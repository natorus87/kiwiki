"""
MCP (Model Context Protocol) server for kiwiki.

Supports two transports:
  1. Streamable HTTP  — POST /mcp          (MCP spec 2025-03-26, default for Claude Code/Desktop)
  2. HTTP + SSE       — GET  /mcp/sse      (MCP spec 2024-11-05, older Cursor versions etc.)
                        POST /mcp/messages

Both transports expose identical tools.
Auth: Authorization: Bearer <api-key> header.
"""
import asyncio
import base64
import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import threading
import time
import uuid
from typing import AsyncGenerator
from urllib.parse import urlencode, urlparse

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse


from .auth import _lookup_api_key, parse_users
from .constants import CSP_BASE_DIRECTIVES
from .models import User
from .storage import (
    read_file,
)
from .tenancy import ensure_user_workspace, is_valid_username, set_user_ns, user_root

from .constants import APP_VERSION
from .mcp_tools import admin, files, knowledge, links, search, tags  # noqa: F401 (Registrierung via @tool)
from .mcp_tools.common import HANDLERS as _HANDLERS, McpContext
from .mcp_tools.fileutil import _configured_base_url, _initialize_cache

router = APIRouter()
logger = logging.getLogger("kiwiki.mcp")

SUPPORTED_PROTOCOL_VERSIONS = {"2025-06-18", "2025-03-26", "2024-11-05"}
# 2025-06-18 ist die Revision, die outputSchema und structuredContent definiert —
# beides liefert kiwiki. Aeltere Clients bekommen weiterhin die von ihnen
# angefragte Revision zurueck.
MCP_PROTOCOL_VERSION = "2025-06-18"

# Base URL for constructing SSE callback URLs — must match the public address.
# Falls back to the request's own base_url if not set.

# In-memory sessions for HTTP+SSE transport: session_id -> (queue, user)
# User is captured at GET /mcp/sse so POST /mcp/messages works without re-sending auth.
_sse_sessions: dict[str, tuple[asyncio.Queue, "User | None"]] = {}
_SSE_MAX_SESSIONS = int(os.getenv("KIWIKI_MCP_MAX_SSE_SESSIONS", "128"))
_SSE_QUEUE_MAX_MESSAGES = int(os.getenv("KIWIKI_MCP_SSE_QUEUE_MAX_MESSAGES", "100"))

# Minimal dynamic client registration and authorization-code storage.
# This keeps the legacy API-key-as-bearer-token behavior, but no longer exposes
# the API key in the front-channel OAuth redirect.
_oauth_clients: dict[str, dict] = {}
_oauth_codes: dict[str, dict] = {}
# Verwendete Refresh-Token (jti -> exp): Rotation mit Wiederverwendungs-Erkennung.
# Ohne Denylist bliebe ein geleakter Refresh-Token 30 Tage nutzbar. Stateless
# widerrufen geht nicht — ein bereits rotierter Token wird als invalid_grant
# abgelehnt (RFC 6819 §5.2.2.3). RAM-only wie Codes/Clients: Neustart leert die
# Liste, und jede Replica hat ihre eigene (Grenze dokumentiert in SECURITY.md).
_oauth_refresh_seen: dict[str, float] = {}
_OAUTH_REFRESH_SEEN_MAX = int(os.getenv("KIWIKI_OAUTH_MAX_REFRESH_SEEN", "10000"))
# DCR-Spam-Schranke pro IP (Fenster 1h): offene Registrierung + 128 Slots +
# 24h-TTL = sonst füllt eine Quelle alle Slots und legitime Clients kriegen 503.
_oauth_register_hits: dict[str, list[float]] = {}
_OAUTH_REGISTER_PER_IP_MAX = int(os.getenv("KIWIKI_OAUTH_MAX_REGISTER_PER_IP", "16"))
_OAUTH_REGISTER_PER_IP_WINDOW = 3600
_OAUTH_REGISTER_SOURCES_MAX = int(os.getenv("KIWIKI_OAUTH_MAX_REGISTER_SOURCES", "4096"))
_OAUTH_CODE_TTL_SECONDS = 300
_OAUTH_MAX_CODES = int(os.getenv("KIWIKI_OAUTH_MAX_CODES", "256"))
_OAUTH_TOKEN_TTL_SECONDS = int(os.getenv("KIWIKI_OAUTH_TOKEN_TTL_SECONDS", "86400"))
_OAUTH_REFRESH_TOKEN_TTL_SECONDS = int(os.getenv("KIWIKI_OAUTH_REFRESH_TOKEN_TTL_SECONDS", "2592000"))
_OAUTH_FORMAT_PREFIX = "kiwiki1"
_OAUTH_BEARER_VALUE = "bearer"
_OAUTH_NO_CLIENT_AUTH_VALUE = "none"
_OAUTH_WEAK_SECRETS = {"change-me-to-a-random-secret", "changeme", "change-me", "secret", "password"}
_OAUTH_MAX_CLIENTS = int(os.getenv("KIWIKI_OAUTH_MAX_CLIENTS", "128"))
_OAUTH_CLIENT_TTL_SECONDS = int(os.getenv("KIWIKI_OAUTH_CLIENT_TTL_SECONDS", "86400"))
_OAUTH_MAX_REDIRECT_URIS = int(os.getenv("KIWIKI_OAUTH_MAX_REDIRECT_URIS", "10"))
_OAUTH_MAX_REDIRECT_URI_LENGTH = 2048
_DEFAULT_ALLOWED_REDIRECT_HOSTS = "chatgpt.com,chat.openai.com"

# Cache for initialize instructions (AGENTS.md + index.md content per namespace)
# Avoids re-reading these files on every MCP connection.
_INITIALIZE_CACHE_TTL = 60  # seconds


# B6: Agent tracker — logs MCP tool calls to a JSONL file per namespace.
_agent_log_lock = threading.Lock()
_AGENT_LOG_MAX_BYTES = 5 * 1024 * 1024  # 5 MB max, then rotate
_AGENT_LOG_FILE = ".kiwiki/agent_log.jsonl"
_AGENT_LOG_SAFE_ARG_KEYS = {
    "path", "paths", "src", "dst", "folder", "scope", "mode", "format",
    "limit", "offset", "chunk_index", "total_chunks", "finalize", "template_type",
}

# E2: Async grep jobs — background grep with polling.
# Starke Referenzen auf laufende Hintergrund-Greps (siehe asyncio-Doku zu
# create_task: der Loop haelt Tasks nur schwach).
_grep_job_counter = 0
_MCP_MAX_JSONRPC_BATCH = 25


def _log_agent_call(user: "User | None", tool: str, args: dict, success: bool, error: str = "") -> None:
    """B6: Append a tool call entry to the agent log file (JSONL format)."""
    try:
        ns = user.username if user else "anonymous"
        root = user_root() if user else None
        if root is None:
            return
        log_path = root / _AGENT_LOG_FILE
        log_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": time.time(),
            "user": ns,
            "tool": tool,
            # Audit only structural metadata. Wiki/search/replacement content can
            # contain credentials or personal information and must not be copied.
            "args": {k: v for k, v in args.items() if k in _AGENT_LOG_SAFE_ARG_KEYS},
            "success": success,
        }
        if error:
            entry["error"] = error[:200]
        with _agent_log_lock:
            # Rotate if file is too large
            if log_path.exists() and log_path.stat().st_size > _AGENT_LOG_MAX_BYTES:
                rotated = log_path.with_suffix(".jsonl.1")
                if rotated.exists():
                    rotated.unlink()
                log_path.rename(rotated)
                rotated.chmod(0o600)
            fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            os.chmod(log_path, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        # Bewusst still: der Audit-Log darf nie einen MCP-Aufruf abbrechen.
        # Kosten ist ein fehlender Eintrag — der naechste erfolgreiche
        # schreibt wieder, und der Log rotiert nach _AGENT_LOG_MAX_BYTES
        # ohnehin, statt zu eskalieren. Wer den Verlust bemerken will, prueft
        # die Groesse der Datei.
        pass


def _base_url(request: Request) -> str:
    return _configured_base_url() or str(request.base_url).rstrip("/")


def validate_oauth_config() -> None:
    configured = os.getenv("KIWIKI_OAUTH_TOKEN_SECRET", "")
    if configured and configured.strip().lower() in _OAUTH_WEAK_SECRETS:
        raise RuntimeError(
            "KIWIKI_OAUTH_TOKEN_SECRET uses a known placeholder value. "
            "Set a strong random secret or omit it to derive tokens from each API key."
        )


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _api_key_hash(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def _token_secret(api_key: str) -> bytes:
    configured = os.getenv("KIWIKI_OAUTH_TOKEN_SECRET", "")
    if configured:
        validate_oauth_config()
        return configured.encode("utf-8")
    return hashlib.sha256(f"kiwiki-oauth-token:{api_key}".encode("utf-8")).digest()


def _sign_token(payload_b64: str, api_key: str) -> str:
    return _b64url_encode(hmac.new(_token_secret(api_key), payload_b64.encode("ascii"), hashlib.sha256).digest())


def _make_oauth_token(api_key: str, token_type: str, ttl_seconds: int, client_id: str = "", resource: str = "") -> str:
    payload = {
        "typ": token_type,
        "akh": _api_key_hash(api_key),
        "cid": client_id,
        "res": resource,
        "exp": int(time.time()) + ttl_seconds,
        "iat": int(time.time()),
        "jti": secrets.token_urlsafe(16),
    }
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return f"{_OAUTH_FORMAT_PREFIX}.{payload_b64}.{_sign_token(payload_b64, api_key)}"


def _api_key_from_signed_token(token: str, expected_type: str = "access") -> str | None:
    record = _signed_token_record(token, expected_type)
    return record[0] if record is not None else None


def _signed_token_record(token: str, expected_type: str) -> tuple[str, dict] | None:
    try:
        prefix, payload_b64, signature = token.split(".", 2)
        if prefix != _OAUTH_FORMAT_PREFIX:
            return None
        payload = json.loads(_b64url_decode(payload_b64))
    except Exception:
        return None

    if payload.get("typ") != expected_type or int(payload.get("exp", 0)) < int(time.time()):
        return None

    users_map = parse_users()
    for api_key in users_map:
        if not hmac.compare_digest(payload.get("akh", ""), _api_key_hash(api_key)):
            continue
        expected_sig = _sign_token(payload_b64, api_key)
        if hmac.compare_digest(signature, expected_sig):
            return api_key, payload
    return None


_OAUTH_NO_STORE_HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache"}


def _unauthorized(request: Request):
    """Return 401 with WWW-Authenticate pointing to OAuth discovery (RFC 9728).
    Points to /.well-known/oauth-protected-resource/mcp per RFC 9728 §3
    (well-known path = base + /.well-known/oauth-protected-resource + resource-path).
    """
    base = _base_url(request)
    resource_metadata = f"{base}/.well-known/oauth-protected-resource/mcp"
    return JSONResponse(
        {"error": "unauthorized", "error_description": "Bearer token required"},
        status_code=401,
        headers={"WWW-Authenticate": f'Bearer resource_metadata="{resource_metadata}", scope="mcp"'},
    )


def _protected_resource_payload(base: str) -> dict:
    return {
        "resource": f"{base}/mcp",
        "authorization_servers": [base],
        "bearer_methods_supported": ["header"],
        "scopes_supported": ["mcp"],
        "resource_documentation": f"{base}/docs",
    }


def _authorization_server_payload(base: str) -> dict:
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "client_id_metadata_document_supported": True,
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": ["mcp"],
    }


def _is_valid_redirect_uri(redirect_uri: str) -> bool:
    try:
        parsed = urlparse(redirect_uri)
    except Exception:
        return False
    if parsed.scheme == "https" and parsed.netloc:
        return True
    if parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"} and parsed.port:
        return True
    return False


def _authorize_page_csp(redirect_uri: str) -> str:
    """CSP fuer die Consent-Seite: form-action muss die konkrete, bereits
    validierte redirect_uri-Origin zulassen, sonst blockt der Browser den
    Redirect nach dem Submit (form-action wirkt auch auf den Redirect-Ziel-
    Origin, nicht nur auf das initiale Submit-Ziel). Nur diese eine Origin,
    nie ein pauschales Whitelist-Statement — die konkrete redirect_uri wurde
    bereits gegen _is_registered_redirect geprueft."""
    form_action = "'self'"
    if redirect_uri:
        parsed = urlparse(redirect_uri)
        if parsed.scheme and parsed.netloc:
            form_action = f"'self' {parsed.scheme}://{parsed.netloc}"
    return f"{CSP_BASE_DIRECTIVES}; form-action {form_action}"


def _allowed_redirect_hosts() -> set[str]:
    raw = os.getenv("KIWIKI_OAUTH_ALLOWED_REDIRECT_HOSTS", _DEFAULT_ALLOWED_REDIRECT_HOSTS)
    return {host.strip().lower() for host in raw.split(",") if host.strip()}


def _redirect_host_is_allowed(redirect_uri: str) -> bool:
    try:
        parsed = urlparse(redirect_uri)
    except Exception:
        return False
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme == "http" and hostname in {"localhost", "127.0.0.1"} and parsed.port:
        return True
    for allowed in _allowed_redirect_hosts():
        if hostname == allowed or hostname.endswith("." + allowed):
            return True
    return False


def _is_registered_redirect(client_id: str, redirect_uri: str) -> bool:
    if not _is_valid_redirect_uri(redirect_uri):
        return False
    if not client_id:
        # Keep static/no-DCR clients usable, but only for loopback URLs.
        return _redirect_host_is_allowed(redirect_uri)
    parsed_client = urlparse(client_id)
    if parsed_client.scheme == "https" and parsed_client.netloc:
        # ChatGPT can use Client ID Metadata Documents where client_id itself is
        # an HTTPS metadata URL instead of a DCR-generated local identifier.
        return _redirect_host_is_allowed(redirect_uri)
    client = _oauth_clients.get(client_id)
    if client and client.get("expires_at", 0) < time.time():
        _oauth_clients.pop(client_id, None)
        client = None
    if not client:
        # DCR-Registrierungen leben nur im RAM: Nach einem Neustart oder TTL-Ablauf
        # nutzen Connectors (z. B. ChatGPT) ihre alte client_id weiter. Für Hosts
        # auf der Redirect-Whitelist bleibt der Flow deshalb auch ohne bekannte
        # Registrierung gültig — sonst endet jeder Re-Auth in invalid_redirect_uri.
        return _redirect_host_is_allowed(redirect_uri)
    return redirect_uri in client.get("redirect_uris", [])


def _prune_oauth_clients() -> None:
    now = time.time()
    expired = [client_id for client_id, client in _oauth_clients.items() if client.get("expires_at", 0) < now]
    for client_id in expired:
        _oauth_clients.pop(client_id, None)


def _prune_oauth_codes() -> None:
    now = time.time()
    expired = [code for code, record in _oauth_codes.items() if record.get("expires_at", 0) < now]
    for code in expired:
        _oauth_codes.pop(code, None)


def _prune_oauth_refresh_seen(now: float | None = None) -> None:
    now = now if now is not None else time.time()
    expired = [jti for jti, exp in _oauth_refresh_seen.items() if exp < now]
    for jti in expired:
        _oauth_refresh_seen.pop(jti, None)
    # Harte Obergrenze als Notbremse, falls Uhren/Pruning je versagen.
    if len(_oauth_refresh_seen) > _OAUTH_REFRESH_SEEN_MAX:
        oldest = sorted(_oauth_refresh_seen.items(), key=lambda item: item[1])
        for jti, _exp in oldest[: len(_oauth_refresh_seen) - _OAUTH_REFRESH_SEEN_MAX]:
            _oauth_refresh_seen.pop(jti, None)


def _prune_oauth_register_hits(now: float | None = None) -> None:
    """Abgelaufene Zeitstempel aller Quellen entfernen, nicht nur der aktuellen.

    Frueher wurde nur die gerade anfragende IP bereinigt; jede einmal
    gesehene Quelle blieb fuer immer im Dict. Die harte Obergrenze greift,
    falls innerhalb eines Fensters sehr viele verschiedene Quellen auftauchen.
    """
    now = now if now is not None else time.time()
    cutoff = now - _OAUTH_REGISTER_PER_IP_WINDOW
    for source in list(_oauth_register_hits):
        fresh = [ts for ts in _oauth_register_hits[source] if ts > cutoff]
        if fresh:
            _oauth_register_hits[source] = fresh
        else:
            del _oauth_register_hits[source]
    overflow = len(_oauth_register_hits) - _OAUTH_REGISTER_SOURCES_MAX
    if overflow > 0:
        oldest = sorted(_oauth_register_hits, key=lambda src: max(_oauth_register_hits[src]))
        for source in oldest[:overflow]:
            del _oauth_register_hits[source]


def _pkce_s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _pkce_request_is_valid(code_challenge: str, code_challenge_method: str) -> bool:
    return bool(code_challenge) and code_challenge_method == "S256"


# ─────────────────────────────────────────────────────────────────────────────
# OAuth 2.1 Discovery endpoints (RFC 9728 + RFC 8414)
# RFC 9728 §3: for resource at /mcp, well-known path is
#   /.well-known/oauth-protected-resource/mcp
# Serve both paths so older clients that omit the suffix also work.
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/.well-known/oauth-protected-resource/mcp")
@router.get("/.well-known/oauth-protected-resource")
async def oauth_protected_resource(request: Request):
    return JSONResponse(_protected_resource_payload(_base_url(request)))


@router.get("/.well-known/oauth-authorization-server/mcp")
@router.get("/.well-known/oauth-authorization-server")
async def oauth_authorization_server(request: Request):
    return JSONResponse(_authorization_server_payload(_base_url(request)))


@router.get("/oauth/authorize")
async def oauth_authorize(request: Request):
    """Authorization page — user enters their kiwiki API key to complete OAuth flow."""
    redirect_uri = request.query_params.get("redirect_uri", "")
    client_id = request.query_params.get("client_id", "")
    state = request.query_params.get("state", "")
    code_challenge = request.query_params.get("code_challenge", "")
    code_challenge_method = request.query_params.get("code_challenge_method", "")
    resource = request.query_params.get("resource", "")
    error = request.query_params.get("error", "")

    if redirect_uri and not _is_registered_redirect(client_id, redirect_uri):
        return JSONResponse({"error": "invalid_redirect_uri"}, status_code=400)
    if redirect_uri and not _pkce_request_is_valid(code_challenge, code_challenge_method):
        return JSONResponse({"error": "invalid_request", "error_description": "PKCE S256 is required"}, status_code=400)
    if resource and resource != f"{_base_url(request)}/mcp":
        return JSONResponse({"error": "invalid_target", "error_description": "Unknown resource"}, status_code=400)

    registered_client = _oauth_clients.get(client_id, {})
    consent_client = str(registered_client.get("client_name") or client_id or "Unbekannter MCP-Client")
    consent_resource = resource or f"{_base_url(request)}/mcp"

    return HTMLResponse(
        headers={"Content-Security-Policy": _authorize_page_csp(redirect_uri)},
        content=f"""<!DOCTYPE html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>kiwiki – Anmelden</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 400px; margin: 80px auto; padding: 0 1rem; }}
    h1 {{ font-size: 1.4rem; margin-bottom: 0.25rem; }}
    p {{ color: #555; font-size: 0.9rem; margin-bottom: 1.5rem; }}
    label {{ display: block; font-size: 0.85rem; font-weight: 600; margin-bottom: 0.4rem; }}
    input {{ width: 100%; padding: 0.6rem 0.75rem; font-size: 1rem; border: 1px solid #ccc; border-radius: 6px; box-sizing: border-box; }}
    button {{ margin-top: 1rem; width: 100%; padding: 0.7rem; background: #1a1a1a; color: #fff; border: none; border-radius: 6px; font-size: 1rem; cursor: pointer; }}
    button:hover {{ background: #333; }}
    .error {{ color: #c00; font-size: 0.85rem; margin-top: 0.5rem; }}
    .consent {{ background: #f4f4f4; border-radius: 6px; padding: 0.75rem; overflow-wrap: anywhere; }}
  </style>
</head>
<body>
  <h1>kiwiki</h1>
  <p>Gib deinen API-Key ein, um den MCP-Zugriff zu autorisieren.</p>
  <p class="consent"><strong>{html.escape(consent_client)}</strong> erhält Zugriff auf
    <code>{html.escape(consent_resource)}</code> und darf dein Wiki lesen und entsprechend deiner Rolle ändern.
    Mit „Autorisieren“ stimmst du diesem Zugriff zu.</p>
  {"<p class='error'>Ungültiger API-Key. Bitte erneut versuchen.</p>" if error else ""}
  <form method="POST" action="/oauth/authorize">
    <input type="hidden" name="redirect_uri" value="{html.escape(redirect_uri, quote=True)}">
    <input type="hidden" name="client_id" value="{html.escape(client_id, quote=True)}">
    <input type="hidden" name="state" value="{html.escape(state, quote=True)}">
    <input type="hidden" name="code_challenge" value="{html.escape(code_challenge, quote=True)}">
    <input type="hidden" name="code_challenge_method" value="{html.escape(code_challenge_method, quote=True)}">
    <input type="hidden" name="resource" value="{html.escape(resource, quote=True)}">
    <label for="apikey">API-Key</label>
    <input type="password" id="apikey" name="apikey" placeholder="dein-api-key" autofocus required>
    <button type="submit">Autorisieren</button>
  </form>
</body>
</html>"""
    )


@router.post("/oauth/authorize")
async def oauth_authorize_submit(request: Request):
    """Process the authorize form — validate API key and redirect with code."""
    form = await request.form()
    apikey = form.get("apikey", "").strip()
    redirect_uri = form.get("redirect_uri", "")
    client_id = form.get("client_id", "")
    state = form.get("state", "")
    code_challenge = form.get("code_challenge", "")
    code_challenge_method = form.get("code_challenge_method", "")
    resource = form.get("resource", "")

    from .rate_limiter import register_failed_key_attempt, reset_failed_key_attempts

    users_map = parse_users()
    if not apikey or _lookup_api_key(users_map, apikey) is None:
        # Dieses Formular prueft denselben API-Key wie /login. Fehlversuche
        # zaehlen daher gegen ein eigenes Budget, sonst waere der Umweg ueber
        # OAuth der bequemere Weg zum Durchprobieren von Keys.
        if register_failed_key_attempt(request):
            return JSONResponse(
                {"error": "too_many_requests", "error_description": "Zu viele fehlgeschlagene Versuche."},
                status_code=429,
                headers={"Retry-After": "60", **_OAUTH_NO_STORE_HEADERS},
            )
        # Redirect back to form with error
        params = urlencode({
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": code_challenge_method,
            "resource": resource,
            "error": "invalid_key",
        })
        return JSONResponse(None, status_code=302, headers={"Location": f"/oauth/authorize?{params}"})

    reset_failed_key_attempts(request)

    if not redirect_uri:
        return HTMLResponse("<p>Autorisiert. Du kannst dieses Fenster schließen.</p>")

    if not _is_registered_redirect(client_id, redirect_uri):
        return JSONResponse({"error": "invalid_redirect_uri"}, status_code=400)
    if not _pkce_request_is_valid(code_challenge, code_challenge_method):
        return JSONResponse({"error": "invalid_request", "error_description": "PKCE S256 is required"}, status_code=400)
    if resource and resource != f"{_base_url(request)}/mcp":
        return JSONResponse({"error": "invalid_target", "error_description": "Unknown resource"}, status_code=400)

    _prune_oauth_codes()
    if len(_oauth_codes) >= _OAUTH_MAX_CODES:
        return JSONResponse(
            {"error": "temporarily_unavailable", "error_description": "Too many pending authorization requests"},
            status_code=503,
            headers=_OAUTH_NO_STORE_HEADERS,
        )
    code = secrets.token_urlsafe(32)
    _oauth_codes[code] = {
        "api_key": apikey,
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge,
        "code_challenge_method": code_challenge_method,
        "resource": resource,
        "expires_at": time.time() + _OAUTH_CODE_TTL_SECONDS,
    }

    sep = "&" if "?" in redirect_uri else "?"
    params = {"code": code}
    if state:
        params["state"] = state
    location = f"{redirect_uri}{sep}{urlencode(params)}"
    return JSONResponse(None, status_code=302, headers={"Location": location})


@router.post("/oauth/token")
async def oauth_token(request: Request):
    """Exchange OAuth grants for signed bearer tokens that survive app restarts."""
    try:
        form = await request.form()
        grant_type = form.get("grant_type", "authorization_code")
        code = form.get("code", "")
        client_id = form.get("client_id", "")
        redirect_uri = form.get("redirect_uri", "")
        code_verifier = form.get("code_verifier", "")
        resource = form.get("resource", "")
        refresh_token = form.get("refresh_token", "")
    except Exception:
        body = await request.json()
        grant_type = body.get("grant_type", "authorization_code")
        code = body.get("code", "")
        client_id = body.get("client_id", "")
        redirect_uri = body.get("redirect_uri", "")
        code_verifier = body.get("code_verifier", "")
        resource = body.get("resource", "")
        refresh_token = body.get("refresh_token", "")

    if grant_type == "refresh_token":
        token_record = _signed_token_record(refresh_token, expected_type="refresh")
        if token_record is None:
            return JSONResponse({"error": "invalid_grant"}, status_code=400, headers=_OAUTH_NO_STORE_HEADERS)
        api_key, token_payload = token_record
        bound_client = str(token_payload.get("cid", ""))
        bound_resource = str(token_payload.get("res", ""))
        if bound_client and client_id != bound_client:
            return JSONResponse({"error": "invalid_grant"}, status_code=400, headers=_OAUTH_NO_STORE_HEADERS)
        if bound_resource and resource != bound_resource:
            return JSONResponse({"error": "invalid_target"}, status_code=400, headers=_OAUTH_NO_STORE_HEADERS)
        # Rotation mit Wiederverwendungs-Erkennung: ein bereits eingelöster
        # Refresh-Token ist verbraucht. Fehlgeschlagene Bindungsprüfungen oben
        # verbrauchen ihn bewusst NICHT — nur der erfolgreiche Pfad rotiert.
        _prune_oauth_refresh_seen()
        presented_jti = str(token_payload.get("jti", ""))
        if not presented_jti or presented_jti in _oauth_refresh_seen:
            return JSONResponse({"error": "invalid_grant"}, status_code=400, headers=_OAUTH_NO_STORE_HEADERS)
        _oauth_refresh_seen[presented_jti] = float(token_payload.get("exp", time.time()))
        access_token = _make_oauth_token(
            api_key,
            "access",
            _OAUTH_TOKEN_TTL_SECONDS,
            client_id=bound_client,
            resource=bound_resource,
        )
        rotated_refresh_token = _make_oauth_token(
            api_key,
            "refresh",
            _OAUTH_REFRESH_TOKEN_TTL_SECONDS,
            client_id=bound_client,
            resource=bound_resource,
        )
        return JSONResponse({
            "access_token": access_token,
            "refresh_token": rotated_refresh_token,
            "token_type": _OAUTH_BEARER_VALUE,
            "expires_in": _OAUTH_TOKEN_TTL_SECONDS,
            "scope": "mcp",
        }, headers=_OAUTH_NO_STORE_HEADERS)

    if grant_type != "authorization_code":
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400, headers=_OAUTH_NO_STORE_HEADERS)

    record = _oauth_codes.pop(code, None) if code else None
    if not record or record["expires_at"] < time.time():
        return JSONResponse({"error": "invalid_grant"}, status_code=400, headers=_OAUTH_NO_STORE_HEADERS)

    if record["client_id"] and client_id and client_id != record["client_id"]:
        return JSONResponse({"error": "invalid_grant"}, status_code=400, headers=_OAUTH_NO_STORE_HEADERS)
    if redirect_uri and redirect_uri != record["redirect_uri"]:
        return JSONResponse({"error": "invalid_grant"}, status_code=400, headers=_OAUTH_NO_STORE_HEADERS)
    if resource and record.get("resource") and resource != record["resource"]:
        return JSONResponse({"error": "invalid_target"}, status_code=400, headers=_OAUTH_NO_STORE_HEADERS)

    code_challenge = record.get("code_challenge", "")
    if record.get("code_challenge_method") != "S256" or not code_challenge or not code_verifier:
        return JSONResponse({"error": "invalid_grant"}, status_code=400, headers=_OAUTH_NO_STORE_HEADERS)
    if not secrets.compare_digest(_pkce_s256(code_verifier), code_challenge):
        return JSONResponse({"error": "invalid_grant"}, status_code=400, headers=_OAUTH_NO_STORE_HEADERS)

    api_key = record["api_key"]
    if api_key not in parse_users():
        return JSONResponse({"error": "invalid_grant"}, status_code=400, headers=_OAUTH_NO_STORE_HEADERS)

    token_resource = resource or record.get("resource", "")
    access_token = _make_oauth_token(api_key, "access", _OAUTH_TOKEN_TTL_SECONDS, client_id=client_id, resource=token_resource)
    refresh_token = _make_oauth_token(api_key, "refresh", _OAUTH_REFRESH_TOKEN_TTL_SECONDS, client_id=client_id, resource=token_resource)
    return JSONResponse({
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": _OAUTH_BEARER_VALUE,
        "expires_in": _OAUTH_TOKEN_TTL_SECONDS,
        "scope": "mcp",
    }, headers=_OAUTH_NO_STORE_HEADERS)


@router.post("/oauth/register")
async def oauth_register(request: Request):
    """Dynamic Client Registration (RFC 7591) — ChatGPT registers itself before OAuth flow."""
    from .rate_limiter import client_ip as _client_ip_for_register

    _prune_oauth_clients()
    if len(_oauth_clients) >= _OAUTH_MAX_CLIENTS:
        return JSONResponse({"error": "server_error", "error_description": "Too many registered OAuth clients"}, status_code=503)
    # Pro-IP-Schranke (1h-Fenster): verhindert, dass eine Quelle alle Slots
    # füllt und legitime Clients in 503 laufen. Nur erfolgreiche
    # Registrierungen zählen — fehlgeschlagene Validierung kostet nichts.
    now = time.time()
    source = _client_ip_for_register(request)
    _prune_oauth_register_hits(now)
    hits = _oauth_register_hits.get(source, [])
    if len(hits) >= _OAUTH_REGISTER_PER_IP_MAX:
        return JSONResponse(
            {"error": "temporarily_unavailable", "error_description": "Too many registrations from this source"},
            status_code=429,
            headers={"Retry-After": str(_OAUTH_REGISTER_PER_IP_WINDOW)},
        )
    try:
        body = await request.json()
    except Exception:
        body = {}
    client_id = str(uuid.uuid4())
    redirect_uris = body.get("redirect_uris", [])
    if not isinstance(redirect_uris, list) or not redirect_uris:
        return JSONResponse({"error": "invalid_redirect_uris"}, status_code=400)
    if len(redirect_uris) > _OAUTH_MAX_REDIRECT_URIS:
        return JSONResponse({"error": "invalid_redirect_uris"}, status_code=400)
    if not all(isinstance(uri, str) and len(uri) <= _OAUTH_MAX_REDIRECT_URI_LENGTH for uri in redirect_uris):
        return JSONResponse({"error": "invalid_redirect_uris"}, status_code=400)
    if not all(isinstance(uri, str) and _is_valid_redirect_uri(uri) for uri in redirect_uris):
        return JSONResponse({"error": "invalid_redirect_uris"}, status_code=400)
    _oauth_clients[client_id] = {
        "client_name": str(body.get("client_name", "mcp-client"))[:128],
        "redirect_uris": redirect_uris,
        "expires_at": time.time() + _OAUTH_CLIENT_TTL_SECONDS,
    }
    _oauth_register_hits[source] = hits + [now]
    return JSONResponse({
        "client_id": client_id,
        "client_name": body.get("client_name", "mcp-client"),
        "redirect_uris": redirect_uris,
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
        "token_endpoint_auth_method": _OAUTH_NO_CLIENT_AUTH_VALUE,
    }, status_code=201)

# ─────────────────────────────────────────────────────────────────────────────
# Tool definitions
# ─────────────────────────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "read_index",
        "description": "Reads /data/index.md and /data/AGENTS.md and returns both.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "list_files",
        "description": "Lists files and folders at the given relative path inside /data.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path to list (default: '.')"}
            },
            "required": [],
        },
    },
    {
        "name": "read_file",
        "description": "Reads a markdown file and returns its frontmatter and content.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path to the .md file"}
            },
            "required": ["path"],
        },
    },
    {
        "name": "fetch",
        "description": (
            "Fetches one markdown file and returns it as id, title, text, url and metadata. "
            "Follows the ChatGPT and Deep Research connector contract; the id is the note path "
            "returned by search."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Note id as returned by search (the relative path)"},
                "path": {"type": "string", "description": "Alias for id, accepted for direct callers"},
            },
            # Server nimmt id oder path; der OpenAI-Kontrakt sendet immer id.
            # required: ["id"] allein bräche path-Aufrufer unter strikter
            # Client-Validierung — deshalb anyOf statt einem Pflichtfeld.
            "anyOf": [{"required": ["id"]}, {"required": ["path"]}],
        },
    },
    {
        "name": "write_file",
        "description": "Writes (creates or overwrites) a markdown file. Only .md files allowed.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string", "description": "Full file content including frontmatter"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "append_file",
        "description": "Appends content to an existing markdown file and updates its search index.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "write_many",
        "description": (
            "Writes or appends multiple markdown files in one call. "
            "Use this for autonomous batch updates; each file returns its own status so one failure does not abort the whole batch."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "files": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "content": {"type": "string"},
                            "mode": {"type": "string", "enum": ["replace", "append"], "description": "default: replace"},
                            "create_if_missing": {"type": "boolean", "description": "For append mode, create the file if missing (default: true)."},
                        },
                        "required": ["path", "content"],
                    },
                }
            },
            "required": ["files"],
        },
    },
    {
        "name": "chunked_write",
        "description": (
            "Stages and finalizes a large markdown write over multiple calls. "
            "Send chunks with the same upload_id, increasing chunk_index from 0, and set finalize=true on the last call. "
            "Use this when write_file or append_file would exceed client payload limits."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "upload_id": {"type": "string", "description": "Stable id for this upload; defaults to path:mode."},
                "chunk": {"type": "string", "description": "Next content chunk. May be empty on a finalize-only call."},
                "chunk_index": {"type": "integer", "description": "Zero-based chunk index."},
                "total_chunks": {"type": "integer", "description": "Expected number of chunks. Required for deterministic finalization."},
                "mode": {"type": "string", "enum": ["replace", "append"], "description": "default: replace"},
                "finalize": {"type": "boolean", "description": "When true, validates all chunks and writes/appends the assembled content."},
                "expected_sha256": {"type": "string", "description": "Optional sha256 of assembled content for verification."},
                "create_if_missing": {"type": "boolean", "description": "For append mode, create the file if missing (default: true)."},
            },
            "required": ["path", "chunk", "chunk_index"],
        },
    },
    {
        "name": "search",
        "description": (
            "Full-text search over all markdown files using SQLite FTS5. Returns results with "
            "id, title, text and url; pass an id to fetch to read the full note."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string", "maxLength": 512}},
            "required": ["query"],
        },
    },
    {
        "name": "create_note",
        "description": (
            "Creates a new markdown note with frontmatter. "
            "Use 'folder' to place the note in the correct topic subfolder "
            "(e.g. folder='notes/python', folder='projects/kiwiki', folder='decisions'). "
            "Creates the subfolder automatically if it doesn't exist."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title":   {"type": "string", "description": "Human-readable title"},
                "content": {"type": "string", "description": "Markdown body (without frontmatter)"},
                "folder":  {"type": "string", "description": "Target folder path, e.g. 'notes/python' or 'decisions' (default: 'notes')"},
                "tags":    {"type": "array", "items": {"type": "string"}},
            },
            "required": ["title", "content"],
        },
    },
    {
        "name": "delete_file",
        "description": "Permanently deletes a markdown file from the authenticated user's workspace and removes it from the search index. Requires write role.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path to the .md file to delete"}
            },
            "required": ["path"],
        },
    },
    {
        "name": "move_file",
        "description": (
            "Moves or renames a markdown file within the wiki. "
            "Use this to reorganize notes into topic subfolders (e.g. move 'notes/python-asyncio.md' to 'notes/python/asyncio.md'). "
            "Creates destination directories automatically. Requires write role."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "src": {"type": "string", "description": "Current relative path of the file"},
                "dst": {"type": "string", "description": "New relative path (including subfolders)"},
            },
            "required": ["src", "dst"],
        },
    },
    {
        "name": "edit",
        "description": (
            "Edit a file's content without touching frontmatter. Two modes: "
            "(1) Find-and-replace: provide old_str + new_str — replaces first occurrence; "
            "(2) Append: omit old_str or leave it empty — appends new_str to the end. "
            "Raises an error if old_str is given but not found."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path":    {"type": "string", "description": "Relative path to the .md file"},
                "new_str": {"type": "string", "description": "Replacement or content to append"},
                "old_str": {"type": "string", "description": "Exact string to find (omit to append)"},
            },
            "required": ["path", "new_str"],
        },
    },
    {
        "name": "update_frontmatter",
        "description": (
            "Updates frontmatter fields of an existing file without touching its content. "
            "Use this to add/change tags, set 'related' links, update 'type', or fix any metadata. "
            "Pass only the fields you want to change; existing fields are preserved."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path":    {"type": "string", "description": "Relative path to the .md file"},
                "updates": {
                    "type": "object",
                    "description": "Frontmatter fields to set, e.g. {\"tags\": [\"python\"], \"related\": [\"notes/python/async.md\"]}",
                },
            },
            "required": ["path", "updates"],
        },
    },
    {
        "name": "read_many",
        "description": (
            "Reads multiple files in a single call. "
            "Use this when you need context from several related files before writing "
            "(e.g. read index.md + 3 related notes at once). "
            "Returns a map of path → {frontmatter, content}."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of relative file paths to read",
                }
            },
            "required": ["paths"],
        },
    },
    {
        "name": "build_index",
        "description": (
            "Rebuilds index.md from the current wiki structure. "
            "Scans all files, groups them by top-level folder, and writes a fresh index.md "
            "with links and titles. Call this after major reorganizations or when index.md is stale."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "sort",
        "description": (
            "Batch-moves multiple files at once to reorganize the wiki. "
            "Typical workflow: call list_all_files → plan topic subfolders → call sort with all moves. "
            "Each move creates destination directories automatically. "
            "Returns per-move status; failed moves are skipped, others continue."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "moves": {
                    "type": "array",
                    "description": "List of file moves to execute",
                    "items": {
                        "type": "object",
                        "properties": {
                            "src": {"type": "string", "description": "Current path"},
                            "dst": {"type": "string", "description": "New path"},
                        },
                        "required": ["src", "dst"],
                    },
                }
            },
            "required": ["moves"],
        },
    },
    {
        "name": "list_all_files",
        "description": (
            "Recursively lists ALL markdown files in the wiki (or a subtree) with their titles and tags. "
            "Use this to get a full overview of the wiki structure before writing, "
            "to find orphaned pages, or to decide where to place a new note."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Root path to list from (default: '.' = entire wiki)"}
            },
            "required": [],
        },
    },
    {
        "name": "grep",
        "description": (
            "Search for a regex pattern inside markdown files, with line numbers and optional context lines. "
            "Use this when you need to find exact text, TODOs, a specific heading, or any pattern — "
            "similar to `grep -n`. Returns matches with file path, line number, matched line, "
            "and surrounding context. Scope the search with 'path' to a subfolder."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern":        {"type": "string", "description": "Regex pattern to search for"},
                "path":           {"type": "string", "description": "Subtree to search in (default: '.' = entire wiki)"},
                "context_lines":  {"type": "integer", "description": "Lines of context before/after each match (default: 2)"},
                "case_sensitive": {"type": "boolean", "description": "Case-sensitive matching (default: false)"},
                "max_results":    {"type": "integer", "description": "Maximum number of matches to return (default: 100)"},
                "background":     {"type": "boolean", "description": "Run asynchronously and return a job_id (default: false)"},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "find",
        "description": (
            "Find files by filename glob pattern (e.g. 'python*.md', '*asyncio*'). "
            "Searches recursively in the wiki or a given subtree. "
            "Use this when you know part of a filename but not its folder — similar to `find -name`."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern to match against filenames, e.g. 'python*.md' or '*todo*'"},
                "path":    {"type": "string", "description": "Subtree to search in (default: '.' = entire wiki)"},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "file_info",
        "description": (
            "Returns metadata for a file: size in bytes, line count, last-modified timestamp, "
            "and a frontmatter summary. Use this to check when a file was last updated "
            "or how large it is before reading — similar to `ls -la` + `wc -l`."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path to the .md file"}
            },
            "required": ["path"],
        },
    },
    {
        "name": "read_lines",
        "description": (
            "Reads a specific line range from a file with line numbers. "
            "Use 'start'+'end' for a range (like `sed -n '10,20p'`), "
            "or 'tail' for the last N lines (like `tail -n 20`). "
            "Useful for large files when you only need a specific section."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path":  {"type": "string", "description": "Relative path to the .md file"},
                "start": {"type": "integer", "description": "First line to read, 1-indexed (default: 1)"},
                "end":   {"type": "integer", "description": "Last line to read, inclusive (default: last line)"},
                "tail":  {"type": "integer", "description": "Return only the last N lines (overrides start/end)"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "recent_files",
        "description": "Lists recently modified markdown files, sorted newest first.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Subtree to inspect (default: '.' = entire wiki)"},
                "limit": {"type": "integer", "description": "Maximum number of files to return (default: 20)"},
                "include_system": {"type": "boolean", "description": "Include index.md and AGENTS.md (default: false)"},
            },
            "required": [],
        },
    },
    {
        "name": "backlinks",
        "description": "Finds markdown links and plain references that point to a target file.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Target markdown file path"},
                "scope": {"type": "string", "description": "Subtree to scan (default: '.' = entire wiki)"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "move_folder",
        "description": (
            "Moves or renames a folder within the wiki and reindexes moved markdown files. "
            "Creates destination parent folders automatically. Requires write role."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "src": {"type": "string", "description": "Current folder path"},
                "dst": {"type": "string", "description": "New folder path"},
            },
            "required": ["src", "dst"],
        },
    },
    {
        "name": "preview_edit",
        "description": (
            "Previews an edit without writing. Provide old_str + new_str for replacement, "
            "or omit old_str to preview appending new_str to the markdown body."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path to the .md file"},
                "new_str": {"type": "string", "description": "Replacement or appended content"},
                "old_str": {"type": "string", "description": "Exact string to replace (omit to append)"},
                "context_lines": {"type": "integer", "description": "Unified diff context lines (default: 3)"},
            },
            "required": ["path", "new_str"],
        },
    },
    {
        "name": "replace_many",
        "description": (
            "Applies multiple exact string replacements to one or more markdown files. "
            "Use after preview_edit for coordinated edits. Requires write role."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Single file path"},
                "paths": {"type": "array", "items": {"type": "string"}, "description": "Multiple file paths"},
                "replacements": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "old_str": {"type": "string"},
                            "new_str": {"type": "string"},
                        },
                        "required": ["old_str", "new_str"],
                    },
                },
            },
            "required": ["replacements"],
        },
    },
    {
        "name": "validate_wiki",
        "description": (
            "Checks markdown files for missing frontmatter, duplicate titles, and broken local links."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Subtree to validate (default: '.' = entire wiki)"},
                "required_frontmatter": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Required frontmatter fields (default: title,type,created,updated,tags,owner)",
                },
            },
            "required": [],
        },
    },
    {
        "name": "upsert_note",
        "description": (
            "Creates a note if it does not exist; otherwise appends to or replaces the existing note body. "
            "Matches by path first, then by title in the target folder. Requires write role."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Human-readable title"},
                "content": {"type": "string", "description": "Markdown body without frontmatter"},
                "folder": {"type": "string", "description": "Target folder path (default: 'notes')"},
                "path": {"type": "string", "description": "Exact path to upsert if known"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "mode": {"type": "string", "enum": ["append", "replace"], "description": "How to update existing notes (default: append)"},
            },
            "required": ["title", "content"],
        },
    },
    {
        "name": "related_files",
        "description": "Finds related markdown files using shared tags, frontmatter links, and backlinks.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Reference markdown file"},
                "limit": {"type": "integer", "description": "Maximum number of related files (default: 10)"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "tag_index",
        "description": "Lists all frontmatter tags with counts and file paths.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Subtree to scan (default: '.' = entire wiki)"},
            },
            "required": [],
        },
    },
    {
        "name": "reindex_all",
        "description": "Rebuilds the full-text search index for the current user's wiki. Requires write role.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "search_status",
        "description": "Returns search index health information for the current user's wiki.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "whoami",
        "description": "Returns the authenticated username, role, and workspace namespace.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "git_commit",
        "description": "Commits all changes in the wiki workspace with a message. Requires write role.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Commit message"},
            },
            "required": ["message"],
        },
    },
    {
        "name": "file_history",
        "description": "Shows git log history for a specific file.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path to the .md file"},
                "limit": {"type": "integer", "description": "Max commits to return (default: 10)"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "diff",
        "description": "Shows git diff for a file or between commits.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path (omit for full diff)"},
                "from_commit": {"type": "string", "description": "Start commit hash (default: HEAD~1)"},
                "to_commit": {"type": "string", "description": "End commit hash (default: HEAD)"},
            },
            "required": [],
        },
    },
    {
        "name": "statistics",
        "description": "Returns wiki statistics: file counts, word counts, tags, folder distribution.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Subtree to analyze (default: '.')"},
            },
            "required": [],
        },
    },
    {
        "name": "template",
        "description": "Creates a note from a predefined template (meeting, decision, adr, review, bug, feature).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "template_type": {"type": "string", "enum": ["meeting", "decision", "adr", "review", "bug", "feature"]},
                "title": {"type": "string", "description": "Note title"},
                "folder": {"type": "string", "description": "Target folder (default: auto based on type)"},
            },
            "required": ["template_type", "title"],
        },
    },
    {
        "name": "validate_links",
        "description": "Checks all internal markdown links for broken references.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Subtree to check (default: '.')"},
            },
            "required": [],
        },
    },
    {
        "name": "link_graph",
        "description": "Returns the internal link structure as a graph with nodes, edges, and orphaned files.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Subtree to analyze (default: '.')"},
            },
            "required": [],
        },
    },
    {
        "name": "rename",
        "description": "Renames a file and updates ALL internal links that reference it. Requires write role.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "old_path": {"type": "string", "description": "Current file path"},
                "new_path": {"type": "string", "description": "New file path"},
            },
            "required": ["old_path", "new_path"],
        },
    },
    {
        "name": "batch_tag",
        "description": "Sets tags on multiple files at once. Mode 'merge' adds to existing tags, 'replace' overwrites.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "files": {"type": "array", "items": {"type": "string"}, "description": "List of file paths"},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "Tags to set"},
                "mode": {"type": "string", "enum": ["merge", "replace"], "description": "default: merge"},
            },
            "required": ["files", "tags"],
        },
    },
    {
        "name": "export",
        "description": "Exports the wiki as HTML or concatenated markdown.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Subtree to export (default: '.')"},
                "format": {"type": "string", "enum": ["html", "markdown"], "description": "default: html"},
            },
            "required": [],
        },
    },
    {
        "name": "duplicate_check",
        "description": "Finds potentially duplicate files based on title similarity and shared tags.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Subtree to check (default: '.')"},
                "threshold": {"type": "number", "description": "Similarity threshold 0-1 (default: 0.7)"},
            },
            "required": [],
        },
    },
    {
        "name": "ai_summarize",
        "description": "Creates an extractive summary of a file: headings, key sentences, and word count.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path to the .md file"},
                "max_length": {"type": "integer", "description": "Max summary length in words (default: 500)"},
            },
            "required": ["path"],
        },
    },
    # ── E3: Search History ────────────────────────────────────────────────────
    {
        "name": "search_history",
        "description": "Returns recent search queries with result counts. Useful for seeing what was searched before.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max number of entries (default: 10)"},
            },
            "required": [],
        },
    },
    # ── E5: Dead Link Check ──────────────────────────────────────────────────
    {
        "name": "dead_link_check",
        "description": "Scans all markdown files for broken internal links. Returns a list of broken links with source file and line number.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Subtree to check (default: '.' = entire wiki)"},
            },
            "required": [],
        },
    },
    # ── E2: Async Grep Status ────────────────────────────────────────────────
    {
        "name": "grep_status",
        "description": "Check the status of a background grep job started by grep. Returns results when complete.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "The job ID returned by a previous grep call"},
            },
            "required": ["job_id"],
        },
    },
    {
        "name": "knowledge_search",
        "description": "Searches the local deterministic knowledge graph with source provenance.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "maxLength": 512},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
            },
            "required": ["query"],
        },
    },
    {
        "name": "entity_details",
        "description": "Returns one local knowledge entity by id.",
        "inputSchema": {"type": "object", "properties": {"entity_id": {"type": "string", "maxLength": 128}}, "required": ["entity_id"]},
    },
    {
        "name": "entity_neighbors",
        "description": "Returns bounded relationships adjacent to one entity.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "entity_id": {"type": "string", "maxLength": 128},
                "depth": {"type": "integer", "minimum": 1, "maximum": 3, "default": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
            },
            "required": ["entity_id"],
        },
    },
    {
        "name": "fact_timeline",
        "description": "Returns bounded facts for an entity with their source documents.",
        "inputSchema": {
            "type": "object",
            "properties": {"entity_id": {"type": "string", "maxLength": 128}, "limit": {"type": "integer", "minimum": 1, "maximum": 100}},
            "required": ["entity_id"],
        },
    },
    {
        "name": "explain_relation",
        "description": "Explains one derived relation and its Markdown provenance.",
        "inputSchema": {"type": "object", "properties": {"relation_id": {"type": "string", "maxLength": 128}}, "required": ["relation_id"]},
    },
    {
        "name": "knowledge_status",
        "description": "Returns local knowledge index state and bounded counters.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "knowledge_reindex",
        "description": "Queues an idempotent knowledge reconcile for the authenticated user's workspace. Requires write role.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
]

_STRING_MAP_SCHEMA = {
    "type": "object",
    "additionalProperties": {"type": "string"},
}

_FRONTMATTER_SCHEMA = {
    "type": "object",
    "additionalProperties": True,
}

_FILE_INFO_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "name": {"type": "string"},
        "is_dir": {"type": "boolean"},
        "size": {"type": "integer"},
        "updated_at": {"type": ["string", "null"]},
        "has_children": {"type": "boolean"},
    },
    "required": ["path", "name", "is_dir", "size", "has_children"],
    "additionalProperties": False,
}

_FILE_CONTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "frontmatter": _FRONTMATTER_SCHEMA,
        "content": {"type": "string"},
    },
    "required": ["path", "frontmatter", "content"],
    "additionalProperties": False,
}

_STATUS_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "status": {"type": "string"},
    },
    "required": ["path", "status"],
    "additionalProperties": False,
}

_BATCH_WRITE_RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "status": {"type": "string"},
        "mode": {"type": "string"},
        "bytes": {"type": "integer"},
        "sha256": {"type": "string"},
        "error": {"type": "string"},
    },
    "required": ["path", "status", "mode"],
    "additionalProperties": False,
}

_BATCH_WRITE_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {"type": "array", "items": _BATCH_WRITE_RESULT_SCHEMA},
        "written": {"type": "integer"},
        "failed": {"type": "integer"},
    },
    "required": ["results", "written", "failed"],
    "additionalProperties": False,
}

_CHUNKED_WRITE_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "upload_id": {"type": "string"},
        "status": {"type": "string"},
        "mode": {"type": "string"},
        "received_chunks": {"type": "integer"},
        "total_chunks": {"type": ["integer", "null"]},
        "received_bytes": {"type": "integer"},
        "missing_chunks": {"type": "array", "items": {"type": "integer"}},
        "bytes": {"type": "integer"},
        "sha256": {"type": "string"},
    },
    "required": ["path", "upload_id", "status", "mode", "received_chunks", "received_bytes"],
    "additionalProperties": False,
}

_SEARCH_RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "title": {"type": "string"},
        "snippet": {"type": "string"},
        "score": {"type": "number"},
    },
    "required": ["path", "title", "snippet", "score"],
    "additionalProperties": False,
}

_ALL_FILE_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "title": {"type": "string"},
        "updated": {"type": "string"},
        "created": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["path", "title", "updated", "created", "tags"],
    "additionalProperties": False,
}

_MOVE_RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "src": {"type": "string"},
        "dst": {"type": "string"},
        "status": {"type": "string"},
        "error": {"type": "string"},
    },
    "required": ["src", "dst", "status"],
    "additionalProperties": False,
}

_READ_MANY_FILE_SCHEMA = {
    "type": "object",
    "properties": {
        "frontmatter": _FRONTMATTER_SCHEMA,
        "content": {"type": "string"},
        "error": {"type": "string"},
    },
    "additionalProperties": False,
}

_RECENT_FILE_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "title": {"type": "string"},
        "updated": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "modified": {"type": "string"},
        "size_bytes": {"type": "integer"},
    },
    "required": ["path", "title", "updated", "tags", "modified", "size_bytes"],
    "additionalProperties": False,
}

_REFERENCE_MATCH_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "title": {"type": "string"},
        "line": {"type": "integer"},
        "text": {"type": "string"},
    },
    "required": ["path", "title", "line", "text"],
    "additionalProperties": False,
}

_ISSUE_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "type": {"type": "string"},
        "message": {"type": "string"},
        "line": {"type": "integer"},
    },
    "required": ["path", "type", "message"],
    "additionalProperties": False,
}

_RELATED_FILE_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "title": {"type": "string"},
        "score": {"type": "integer"},
        "reasons": {"type": "array", "items": {"type": "string"}},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["path", "title", "score", "reasons", "tags"],
    "additionalProperties": False,
}

_TAG_ENTRY_SCHEMA = {
    "type": "object",
    "properties": {
        "tag": {"type": "string"},
        "count": {"type": "integer"},
        "files": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["tag", "count", "files"],
    "additionalProperties": False,
}

def _list_output_schema(item_schema: dict) -> dict:
    """Listenergebnisse in ein Objekt-Schema huellen.

    Die MCP-Spezifikation laesst fuer `outputSchema` und `structuredContent` nur
    Objekte zu. Tools, die natuerlicherweise eine Liste liefern, geben sie
    deshalb unter dem Schluessel `items` zurueck.
    """
    return {
        "type": "object",
        "properties": {"items": {"type": "array", "items": item_schema}},
        "required": ["items"],
        "additionalProperties": False,
    }


# Eine Relation aus knowledge.relations. Die Tabelle erzwingt
# CHECK((object_id IS NULL) != (object_value IS NULL)): genau eines der beiden
# Felder traegt das Objekt, das andere ist null. Deshalb sind "entity_id" und
# "value" nullable, aber immer beide vorhanden. Der heutige Extraktor erzeugt
# ausschliesslich Literal-Relationen (tagged_with, related_to), also durchweg
# entity_id=null — die Spalte object_id bleibt fuer Entitaet-zu-Entitaet.
_KNOWLEDGE_FACT_SCHEMA = {
    "type": "object",
    "properties": {
        "predicate": {"type": "string"},
        "entity_id": {"type": ["string", "null"]},
        "value": {"type": ["string", "null"]},
        "source": {"type": "string", "description": "Pfad der Notiz, aus der die Relation stammt"},
    },
    "required": ["predicate", "entity_id", "value", "source"],
    "additionalProperties": False,
}


_OUTPUT_SCHEMAS = {
    "read_index": _STRING_MAP_SCHEMA,
    "list_files": _list_output_schema(_FILE_INFO_SCHEMA),
    "read_file": _FILE_CONTENT_SCHEMA,
    "fetch": {
        # OpenAI-Connector-Kontrakt: id/title/text/url, metadata optional.
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "title": {"type": "string"},
            "text": {"type": "string"},
            "url": {"type": "string"},
            "metadata": {"type": "object", "additionalProperties": True},
        },
        "required": ["id", "title", "text", "url"],
        "additionalProperties": False,
    },
    "write_file": _STATUS_SCHEMA,
    "append_file": _STATUS_SCHEMA,
    "write_many": _BATCH_WRITE_SCHEMA,
    "chunked_write": _CHUNKED_WRITE_SCHEMA,
    "search": {
        # OpenAI-Connector-Kontrakt (developers.openai.com/api/docs/mcp):
        # Top-Level "results", je Treffer id/title/url. "text" ist optional,
        # hilft Deep Research aber bei der Relevanzbewertung.
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "title": {"type": "string"},
                        "text": {"type": "string"},
                        "url": {"type": "string"},
                    },
                    "required": ["id", "title", "url"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["results"],
        "additionalProperties": False,
    },
    "create_note": _STATUS_SCHEMA,
    "delete_file": _STATUS_SCHEMA,
    "move_file": {
        "type": "object",
        "properties": {
            "src": {"type": "string"},
            "dst": {"type": "string"},
            "status": {"type": "string"},
        },
        "required": ["src", "dst", "status"],
        "additionalProperties": False,
    },
    "edit": _STATUS_SCHEMA,
    "update_frontmatter": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "frontmatter": _FRONTMATTER_SCHEMA,
            "status": {"type": "string"},
        },
        "required": ["path", "frontmatter", "status"],
        "additionalProperties": False,
    },
    "read_many": {
        "type": "object",
        "additionalProperties": _READ_MANY_FILE_SCHEMA,
    },
    "build_index": {
        "type": "object",
        "properties": {
            "status": {"type": "string"},
            "sections": {"type": "integer"},
        },
        "required": ["status", "sections"],
        "additionalProperties": False,
    },
    "sort": _list_output_schema(_MOVE_RESULT_SCHEMA),
    "list_all_files": _list_output_schema(_ALL_FILE_SCHEMA),
    "grep": {
        "type": "object",
        "properties": {
            "matches": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "file": {"type": "string"},
                        "line": {"type": "integer"},
                        "text": {"type": "string"},
                        "context_before": {"type": "array", "items": {"type": "string"}},
                        "context_after": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["file", "line", "text", "context_before", "context_after"],
                    "additionalProperties": False,
                },
            },
            "truncated": {"type": "boolean"},
            "total_shown": {"type": "integer"},
            "error": {"type": "string"},
        },
        "additionalProperties": False,
    },
    "find": {
        "type": "object",
        "properties": {
            "matches": {"type": "array", "items": {"type": "string"}},
            "count": {"type": "integer"},
        },
        "required": ["matches", "count"],
        "additionalProperties": False,
    },
    "file_info": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "size_bytes": {"type": "integer"},
            "line_count": {"type": ["integer", "null"]},
            "modified": {"type": "string"},
        },
        "required": ["path", "size_bytes", "line_count", "modified"],
        "additionalProperties": False,
    },
    "read_lines": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "total_lines": {"type": "integer"},
            "lines": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "line": {"type": "integer"},
                        "text": {"type": "string"},
                    },
                    "required": ["line", "text"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["path", "total_lines", "lines"],
        "additionalProperties": False,
    },
    "recent_files": _list_output_schema(_RECENT_FILE_SCHEMA),
    "backlinks": {
        "type": "object",
        "properties": {
            "target": {"type": "string"},
            "matches": {"type": "array", "items": _REFERENCE_MATCH_SCHEMA},
            "count": {"type": "integer"},
        },
        "required": ["target", "matches", "count"],
        "additionalProperties": False,
    },
    "move_folder": {
        "type": "object",
        "properties": {
            "src": {"type": "string"},
            "dst": {"type": "string"},
            "status": {"type": "string"},
            "moved_files": {"type": "integer"},
        },
        "required": ["src", "dst", "status", "moved_files"],
        "additionalProperties": False,
    },
    "preview_edit": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "mode": {"type": "string"},
            "changed": {"type": "boolean"},
            "diff": {"type": "string"},
        },
        "required": ["path", "mode", "changed", "diff"],
        "additionalProperties": False,
    },
    "replace_many": {
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "replacements": {"type": "integer"},
                        "changed": {"type": "boolean"},
                    },
                    "required": ["path", "replacements", "changed"],
                    "additionalProperties": False,
                },
            },
            "total_replacements": {"type": "integer"},
        },
        "required": ["results", "total_replacements"],
        "additionalProperties": False,
    },
    "validate_wiki": {
        "type": "object",
        "properties": {
            "checked_files": {"type": "integer"},
            "issue_count": {"type": "integer"},
            "issues": {"type": "array", "items": _ISSUE_SCHEMA},
        },
        "required": ["checked_files", "issue_count", "issues"],
        "additionalProperties": False,
    },
    "upsert_note": _STATUS_SCHEMA,
    "related_files": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "related": {"type": "array", "items": _RELATED_FILE_SCHEMA},
            "count": {"type": "integer"},
        },
        "required": ["path", "related", "count"],
        "additionalProperties": False,
    },
    "tag_index": _list_output_schema(_TAG_ENTRY_SCHEMA),
    "reindex_all": {
        "type": "object",
        "properties": {
            "status": {"type": "string"},
            "indexed_files": {"type": "integer"},
        },
        "required": ["status", "indexed_files"],
        "additionalProperties": False,
    },
    "search_status": {
        "type": "object",
        "properties": {
            "markdown_files": {"type": "integer"},
            "indexed_files": {"type": "integer"},
            "database": {"type": "string"},
        },
        "required": ["markdown_files", "indexed_files", "database"],
        "additionalProperties": False,
    },
    "whoami": {
        "type": "object",
        "properties": {
            "username": {"type": "string"},
            "role": {"type": "string"},
            "workspace": {"type": "string"},
        },
        "required": ["username", "role", "workspace"],
        "additionalProperties": False,
    },
    "git_commit": {
        "type": "object",
        "properties": {
            "commit_hash": {"type": "string"},
            "message": {"type": "string"},
            "files_changed": {"type": "integer"},
        },
        "required": ["commit_hash", "message", "files_changed"],
        "additionalProperties": False,
    },
    "file_history": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "history": {"type": "array", "items": {
                "type": "object",
                "properties": {"hash": {"type": "string"}, "date": {"type": "string"}, "author": {"type": "string"}, "message": {"type": "string"}},
                "required": ["hash", "date", "author", "message"],
            }},
        },
        "required": ["path", "history"],
        "additionalProperties": False,
    },
    "diff": {
        "type": "object",
        "properties": {
            "diff": {"type": "string"},
            "files_changed": {"type": "integer"},
        },
        "required": ["diff", "files_changed"],
        "additionalProperties": False,
    },
    "statistics": {
        "type": "object",
        "properties": {
            "total_files": {"type": "integer"},
            "total_words": {"type": "integer"},
            "total_chars": {"type": "integer"},
            "files_by_folder": {"type": "object", "additionalProperties": {"type": "integer"}},
            "top_tags": {"type": "array", "items": {"type": "object", "properties": {"tag": {"type": "string"}, "count": {"type": "integer"}}, "required": ["tag", "count"]}},
            "most_recent_files": {"type": "array", "items": {"type": "object", "properties": {"path": {"type": "string"}, "title": {"type": "string"}, "updated": {"type": "string"}}, "required": ["path", "title", "updated"]}},
            "oldest_files": {"type": "array", "items": {"type": "object", "properties": {"path": {"type": "string"}, "title": {"type": "string"}, "updated": {"type": "string"}}, "required": ["path", "title", "updated"]}},
        },
        "required": ["total_files", "total_words", "total_chars", "files_by_folder", "top_tags", "most_recent_files", "oldest_files"],
        "additionalProperties": False,
    },
    "template": {
        # Eigenes Schema statt _STATUS_SCHEMA: template liefert zusaetzlich den
        # tatsaechlich verwendeten Typ zurueck ("adr" wird auf "decision"
        # abgebildet), und _STATUS_SCHEMA verbietet jedes weitere Feld.
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "status": {"type": "string"},
            "template_type": {"type": "string"},
        },
        "required": ["path", "status", "template_type"],
        "additionalProperties": False,
    },
    "validate_links": {
        "type": "object",
        "properties": {
            "checked_files": {"type": "integer"},
            "broken_links": {"type": "array", "items": {
                "type": "object",
                "properties": {"source": {"type": "string"}, "line": {"type": "integer"}, "link": {"type": "string"}, "target_exists": {"type": "boolean"}},
                "required": ["source", "line", "link", "target_exists"],
            }},
            "valid_count": {"type": "integer"},
            "broken_count": {"type": "integer"},
        },
        "required": ["checked_files", "broken_links", "valid_count", "broken_count"],
        "additionalProperties": False,
    },
    "link_graph": {
        "type": "object",
        "properties": {
            "nodes": {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "string"}, "title": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}}}, "required": ["id", "title", "tags"]}},
            "edges": {"type": "array", "items": {"type": "object", "properties": {"source": {"type": "string"}, "target": {"type": "string"}, "link_text": {"type": "string"}}, "required": ["source", "target", "link_text"]}},
            "orphaned": {"type": "array", "items": {"type": "string"}},
            "most_linked": {"type": "array", "items": {"type": "object", "properties": {"path": {"type": "string"}, "count": {"type": "integer"}}, "required": ["path", "count"]}},
            "most_linking": {"type": "array", "items": {"type": "object", "properties": {"path": {"type": "string"}, "count": {"type": "integer"}}, "required": ["path", "count"]}},
        },
        "required": ["nodes", "edges", "orphaned", "most_linked", "most_linking"],
        "additionalProperties": False,
    },
    "rename": {
        "type": "object",
        "properties": {
            "old_path": {"type": "string"},
            "new_path": {"type": "string"},
            "links_updated": {"type": "integer"},
            "status": {"type": "string"},
        },
        "required": ["old_path", "new_path", "links_updated", "status"],
        "additionalProperties": False,
    },
    "batch_tag": {
        "type": "object",
        "properties": {
            "updated": {"type": "array", "items": {"type": "object", "properties": {"path": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}}}, "required": ["path", "tags"]}},
            "count": {"type": "integer"},
        },
        "required": ["updated", "count"],
        "additionalProperties": False,
    },
    "export": {
        "type": "object",
        "properties": {
            "content": {"type": "string"},
            "file_count": {"type": "integer"},
            "total_size": {"type": "integer"},
        },
        "required": ["content", "file_count", "total_size"],
        "additionalProperties": False,
    },
    "duplicate_check": {
        "type": "object",
        "properties": {
            "pairs": {"type": "array", "items": {"type": "object", "properties": {"file_a": {"type": "string"}, "file_b": {"type": "string"}, "similarity": {"type": "number"}, "reason": {"type": "string"}}, "required": ["file_a", "file_b", "similarity", "reason"]}},
            "total_checked": {"type": "integer"},
        },
        "required": ["pairs", "total_checked"],
        "additionalProperties": False,
    },
    "ai_summarize": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "summary": {"type": "string"},
            "word_count": {"type": "integer"},
            "headings": {"type": "array", "items": {"type": "string"}},
            "key_facts": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["path", "summary", "word_count", "headings", "key_facts"],
        "additionalProperties": False,
    },
    "search_history": _list_output_schema({
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "timestamp": {"type": "number"},
            "result_count": {"type": "integer"},
        },
        "required": ["query", "timestamp", "result_count"],
    }),
    "dead_link_check": {
        "type": "object",
        "properties": {
            "checked_files": {"type": "integer"},
            "broken_links": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                    "line": {"type": "integer"},
                    "link": {"type": "string"},
                    "target_exists": {"type": "boolean"},
                },
                "required": ["source", "line", "link", "target_exists"],
            }},
            "valid_count": {"type": "integer"},
            "broken_count": {"type": "integer"},
        },
        "required": ["checked_files", "broken_links", "valid_count", "broken_count"],
        "additionalProperties": False,
    },
    "grep_status": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["running", "completed", "not_found"]},
            "job_id": {"type": "string"},
            "result": {"type": "object"},
        },
        "required": ["status", "job_id"],
        "additionalProperties": False,
    },
    "knowledge_search": {
        "type": "object",
        "properties": {
            "status": {"type": "string"},
            "results": {"type": "array", "items": {"type": "object"}},
        },
        "required": ["status", "results"],
        "additionalProperties": False,
    },
    "entity_details": {
        # "entity" fehlt, solange die Wissensmaschine aus ist, und ist null,
        # wenn die id unbekannt ist — beides unterscheidbar zu halten ist der
        # Sinn der Unterscheidung zwischen "nicht vorhanden" und "nichts gefunden".
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["disabled", "ready"]},
            "entity": {
                "type": ["object", "null"],
                "properties": {
                    "id": {"type": "string"},
                    "kind": {"type": "string"},
                    "name": {"type": "string"},
                },
                "required": ["id", "kind", "name"],
                "additionalProperties": False,
            },
        },
        "required": ["status"],
        "additionalProperties": False,
    },
    "entity_neighbors": {
        # "depth" liefert der Server nur im aktiven Zustand; der Wert ist die
        # auf 1..3 geklemmte Anfrage, nicht die ungepruefte Eingabe.
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["disabled", "ready"]},
            "depth": {"type": "integer", "minimum": 1, "maximum": 3},
            "neighbors": {"type": "array", "items": _KNOWLEDGE_FACT_SCHEMA},
        },
        "required": ["status", "neighbors"],
        "additionalProperties": False,
    },
    "fact_timeline": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["disabled", "ready"]},
            "facts": {"type": "array", "items": _KNOWLEDGE_FACT_SCHEMA},
        },
        "required": ["status", "facts"],
        "additionalProperties": False,
    },
    "explain_relation": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["disabled", "ready"]},
            "relation": {
                # "value" ist null, wenn die Relation auf eine Entitaet statt auf
                # einen Literalwert zeigt (siehe _KNOWLEDGE_FACT_SCHEMA).
                "type": ["object", "null"],
                "properties": {
                    "predicate": {"type": "string"},
                    "value": {"type": ["string", "null"]},
                    "source": {"type": "string"},
                    "revision": {"type": "integer", "minimum": 0},
                    "extraction": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": ["predicate", "value", "source", "revision", "extraction", "confidence"],
                "additionalProperties": False,
            },
        },
        "required": ["status"],
        "additionalProperties": False,
    },
    "knowledge_status": {
        "type": "object",
        "properties": {
            "status": {"type": "string"},
            "enabled": {"type": "boolean"},
            "documents": {"type": "integer"},
            "pending": {"type": "integer"},
            "failed": {"type": "integer"},
        },
        "required": ["status", "enabled", "documents", "pending", "failed"],
        "additionalProperties": False,
    },
    "knowledge_reindex": {
        # "queued" heisst: der Abgleich ist eingereiht, nicht abgeschlossen.
        # Den Fortschritt liefert knowledge_status.
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["disabled", "queued"]},
            "enabled": {"type": "boolean"},
        },
        "required": ["status", "enabled"],
        "additionalProperties": False,
    },
}

_READ_ONLY_TOOLS = {
    "read_index",
    "list_files",
    "read_file",
    "fetch",
    "search",
    "read_many",
    "list_all_files",
    "grep",
    "find",
    "file_info",
    "read_lines",
    "recent_files",
    "backlinks",
    "preview_edit",
    "validate_wiki",
    "search_history",
    "dead_link_check",
    "grep_status",
    "related_files",
    "tag_index",
    "search_status",
    "whoami",
    "file_history",
    "diff",
    "statistics",
    "validate_links",
    "link_graph",
    "export",
    "duplicate_check",
    "ai_summarize",
    "knowledge_search",
    "entity_details",
    "entity_neighbors",
    "fact_timeline",
    "explain_relation",
    "knowledge_status",
}

_DESTRUCTIVE_TOOLS = {"delete_file", "move_file", "move_folder", "sort", "replace_many", "rename"}


def _tool_annotations(name: str) -> dict:
    read_only = name in _READ_ONLY_TOOLS
    destructive = name in _DESTRUCTIVE_TOOLS
    return {
        "readOnlyHint": read_only,
        "destructiveHint": destructive,
        # Regel: idempotent sind Lese-Werkzeuge ohne Schreibseiteneffekt.
        # build_index, reindex_all und knowledge_reindex standen
        # faelschlich drin — teure Schreibvorgaenge, die ein Client sonst
        # frei wiederholen duerfte. grep bleibt drin (Default-Aufruf liest
        # nur; background-Jobs sind opt-in per Argument).
        "idempotentHint": name in {
            "read_index", "list_files", "read_file", "fetch", "search", "read_many",
            "list_all_files", "grep", "find", "file_info", "read_lines",
            "recent_files", "backlinks", "preview_edit", "validate_wiki", "related_files",
            "tag_index", "search_status", "whoami",
            "file_history", "diff", "statistics", "validate_links", "link_graph",
            "export", "duplicate_check", "ai_summarize",
            "knowledge_search", "entity_details", "entity_neighbors", "fact_timeline",
            "explain_relation", "knowledge_status",
            "search_history", "dead_link_check", "grep_status",
        },
        "openWorldHint": False,
    }


for _tool in TOOLS:
    _tool["annotations"] = _tool_annotations(_tool["name"])
    _tool["outputSchema"] = _OUTPUT_SCHEMAS[_tool["name"]]

# ─────────────────────────────────────────────────────────────────────────────
# Auth helpers
# ─────────────────────────────────────────────────────────────────────────────

def _user_from_request(request: Request) -> User | None:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    bearer = auth[7:]
    api_key = _api_key_from_signed_token(bearer, expected_type="access") or bearer
    users_map = parse_users()
    match = _lookup_api_key(users_map, api_key)
    if match is None:
        return None
    username, role = match
    return User(username=username, role=role)


# ─────────────────────────────────────────────────────────────────────────────
# JSON-RPC helpers
# ─────────────────────────────────────────────────────────────────────────────

def _rpc_ok(req_id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _rpc_err(req_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _is_notification(body: dict) -> bool:
    return "id" not in body


async def _handle_message(body: dict, user: User | None) -> dict | None:
    """Core JSON-RPC dispatcher — transport-independent."""
    method = body.get("method", "")
    params = body.get("params", {}) or {}
    req_id = body.get("id")

    if method == "initialize":
        if user is not None and is_valid_username(user.username):
            set_user_ns(user.username)
            ensure_user_workspace(user.username)
        requested = params.get("protocolVersion", MCP_PROTOCOL_VERSION)
        negotiated = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else MCP_PROTOCOL_VERSION

        # Use cached schema hint if available (avoids re-reading AGENTS.md + index.md
        # on every MCP connection — these files rarely change)
        cache_key = user.username if user else "anon"
        now = time.time()
        cached = _initialize_cache.get(cache_key)
        if cached and now - cached[0] < _INITIALIZE_CACHE_TTL:
            schema_hint = cached[1]
        else:
            try:
                agents = read_file("AGENTS.md").content
                index  = read_file("index.md").content
                schema_hint = f"\n\n--- AGENTS.md ---\n{agents}\n\n--- index.md ---\n{index}"
            except Exception:
                schema_hint = ""
            _initialize_cache[cache_key] = (now, schema_hint)

        return _rpc_ok(req_id, {
            "protocolVersion": negotiated,
            "capabilities": {
                "tools": {},
                "resources": {"listChanged": False},  # B1
                "prompts": {},  # B5
            },
            "serverInfo": {"name": "kiwiki", "version": APP_VERSION},
            "instructions": (
                "This is kiwiki — a Markdown-based personal wiki. "
                "Follow these rules strictly:\n"
                "1. ALWAYS call read_index at the start of every session to get the current schema and navigation.\n"
                "2. ALWAYS call search before creating new content to avoid duplicates.\n"
                "3. Place notes in topic subfolders: notes/python/, notes/ml/, projects/kiwiki/ etc.\n"
                "4. Create a subfolder once 3+ files share a topic.\n"
                "5. Prefer edit/append_file over write_file for existing files.\n"
                "6. Use write_many for multi-file updates and chunked_write for large files or unreliable clients.\n"
                "7. Authorization is already enforced by kiwiki from the authenticated user's role. "
                "For requested create, update, append, and index refresh operations, call the tool directly; "
                "do not ask whether you may write to or change kiwiki.\n"
                "8. Ask for confirmation only before a tool annotated destructiveHint=true.\n"
                "9. Refresh index.md (via edit or build_index) after creating new folders.\n"
                "10. Always set complete frontmatter: title, type, created, updated, tags, owner."
            ) + schema_hint,
        })

    if method in ("notifications/initialized", "initialized"):
        return None if _is_notification(body) else _rpc_ok(req_id, {})

    # Ping ist in jeder MCP-Revision Pflicht: der Empfaenger muss umgehend mit
    # einer leeren Antwort reagieren. Ohne Handler lief der Keepalive in
    # "Method not found" (HTTP 404) und Clients verwarfen die Sitzung.
    if method == "ping":
        return None if _is_notification(body) else _rpc_ok(req_id, {})

    if method == "tools/list":
        return _rpc_ok(req_id, {"tools": TOOLS})

    # kiwiki bietet keine URI-Templates an. Clients fragen die Liste im
    # Discovery trotzdem ab; -32601 wird dort als HTTP 404 ausgeliefert und
    # laesst den Server defekt aussehen.
    if method == "resources/templates/list":
        return _rpc_ok(req_id, {"resourceTemplates": []})

    # ── B1: MCP Resources ────────────────────────────────────────────────────
    if method == "resources/list":
        resources = [
            {
                "uri": "kiwiki://index",
                "name": "Wiki Index",
                "description": "The main index.md file with navigation structure",
                "mimeType": "text/markdown",
            },
            {
                "uri": "kiwiki://agents",
                "name": "Agent Instructions",
                "description": "AGENTS.md with instructions for AI agents",
                "mimeType": "text/markdown",
            },
            {
                "uri": "kiwiki://tags",
                "name": "Tag Index",
                "description": "All tags with file counts and paths",
                "mimeType": "application/json",
            },
            {
                "uri": "kiwiki://recent",
                "name": "Recent Files",
                "description": "Recently modified files sorted newest first",
                "mimeType": "application/json",
            },
            {
                "uri": "kiwiki://search_history",
                "name": "Search History",
                "description": "Recent search queries with result counts",
                "mimeType": "application/json",
            },
        ]
        return _rpc_ok(req_id, {"resources": resources})

    if method == "resources/read":
        uri = params.get("uri", "")
        if user is not None and is_valid_username(user.username):
            set_user_ns(user.username)

        if uri == "kiwiki://index":
            try:
                content = read_file("index.md").content
            except Exception:
                content = "# Index\nNo index.md found."
            return _rpc_ok(req_id, {
                "contents": [{"uri": uri, "mimeType": "text/markdown", "text": content}]
            })
        elif uri == "kiwiki://agents":
            try:
                content = read_file("AGENTS.md").content
            except Exception:
                content = "# Agents\nNo AGENTS.md found."
            return _rpc_ok(req_id, {
                "contents": [{"uri": uri, "mimeType": "text/markdown", "text": content}]
            })
        elif uri == "kiwiki://tags":
            tag_data = await _dispatch("tag_index", {}, user)
            return _rpc_ok(req_id, {
                "contents": [{"uri": uri, "mimeType": "application/json", "text": tag_data}]
            })
        elif uri == "kiwiki://recent":
            recent_data = await _dispatch("recent_files", {"limit": 20}, user)
            return _rpc_ok(req_id, {
                "contents": [{"uri": uri, "mimeType": "application/json", "text": recent_data}]
            })
        elif uri == "kiwiki://search_history":
            from .search import get_search_history
            history = get_search_history(20)
            return _rpc_ok(req_id, {
                "contents": [{"uri": uri, "mimeType": "application/json", "text": json.dumps(history)}]
            })
        return _rpc_err(req_id, -32602, f"Unknown resource: {uri}")

    # ── B5: MCP Prompts ──────────────────────────────────────────────────────
    if method == "prompts/list":
        prompts = [
            {
                "name": "meeting_note",
                "description": "Create a structured meeting note with agenda, attendees, and action items",
                "arguments": [
                    {"name": "title", "description": "Meeting title", "required": True},
                    {"name": "date", "description": "Meeting date (YYYY-MM-DD)", "required": False},
                ],
            },
            {
                "name": "decision_record",
                "description": "Record an architectural or project decision with context and consequences",
                "arguments": [
                    {"name": "title", "description": "Decision title", "required": True},
                    {"name": "context", "description": "What situation prompted this decision", "required": False},
                ],
            },
            {
                "name": "bug_report",
                "description": "Structured bug report with steps to reproduce and environment info",
                "arguments": [
                    {"name": "title", "description": "Bug title", "required": True},
                ],
            },
            {
                "name": "feature_spec",
                "description": "Feature specification with user story, acceptance criteria, and tasks",
                "arguments": [
                    {"name": "title", "description": "Feature title", "required": True},
                ],
            },
            {
                "name": "daily_summary",
                "description": "Summarize what was done today based on recent file changes",
                "arguments": [],
            },
        ]
        return _rpc_ok(req_id, {"prompts": prompts})

    if method == "prompts/get":
        prompt_name = params.get("name", "")
        args = params.get("arguments", {})

        if prompt_name == "meeting_note":
            title = args.get("title", "Meeting")
            date = args.get("date", time.strftime("%Y-%m-%d"))
            return _rpc_ok(req_id, {
                "description": f"Create a meeting note for: {title}",
                "messages": [{
                    "role": "user",
                    "content": {
                        "type": "text",
                        "text": (
                            f"Create a meeting note titled '{title}' dated {date}.\n"
                            "Use the template tool with type 'meeting', or create a note in notes/meetings/ with this structure:\n"
                            "- Agenda\n- Participants\n- Decisions\n- Action Items (table: Who | What | Deadline)"
                        ),
                    },
                }],
            })
        elif prompt_name == "decision_record":
            title = args.get("title", "Decision")
            context = args.get("context", "")
            context_line = f"Context: {context}\n" if context else ""
            return _rpc_ok(req_id, {
                "description": f"Record decision: {title}",
                "messages": [{
                    "role": "user",
                    "content": {
                        "type": "text",
                        "text": (
                            f"Record an architectural decision: '{title}'\n"
                            f"{context_line}"
                            "Create a note in decisions/ with:\n"
                            "- Context (situation)\n- Decision (what was decided)\n- Consequences (positive + negative)\n- Alternatives considered"
                        ),
                    },
                }],
            })
        elif prompt_name == "bug_report":
            title = args.get("title", "Bug")
            return _rpc_ok(req_id, {
                "description": f"Report bug: {title}",
                "messages": [{
                    "role": "user",
                    "content": {
                        "type": "text",
                        "text": (
                            f"Create a bug report: '{title}'\n"
                            "Use the template tool with type 'bug', or create in notes/bugs/ with:\n"
                            "- Steps to reproduce\n- Expected behavior\n- Actual behavior\n- Possible fix\n- Environment (OS, version)"
                        ),
                    },
                }],
            })
        elif prompt_name == "feature_spec":
            title = args.get("title", "Feature")
            return _rpc_ok(req_id, {
                "description": f"Specify feature: {title}",
                "messages": [{
                    "role": "user",
                    "content": {
                        "type": "text",
                        "text": (
                            f"Write a feature specification: '{title}'\n"
                            "Use the template tool with type 'feature', or create in notes/features/ with:\n"
                            "- User Story (As a ... I want ... so that ...)\n"
                            "- Acceptance Criteria\n"
                            "- Implementation Approach\n"
                            "- Tasks\n"
                            "- Testing plan"
                        ),
                    },
                }],
            })
        elif prompt_name == "daily_summary":
            return _rpc_ok(req_id, {
                "description": "Summarize today's activity",
                "messages": [{
                    "role": "user",
                    "content": {
                        "type": "text",
                        "text": (
                            "Summarize what was done today in this wiki.\n"
                            "1. Call recent_files to see what changed today\n"
                            "2. Call search_status to check index health\n"
                            "3. Create a summary note in notes/ with today's date"
                        ),
                    },
                }],
            })
        return _rpc_err(req_id, -32602, f"Unknown prompt: {prompt_name}")

    if method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {}) or {}
        try:
            text = await _dispatch(tool_name, arguments, user)
            # B6: Log tool call to agent tracker
            _log_agent_call(user, tool_name, arguments, success=True)
            result: dict = {"content": [{"type": "text", "text": text}]}
            # structuredContent ist laut MCP-Spec ein Objekt. Ein nicht-objekt
            # Ergebnis wird weggelassen statt schemawidrig ausgeliefert; ein
            # Parse-Fehler darf den bereits ausgefuehrten Tool-Aufruf nicht
            # nachtraeglich als Fehlschlag erscheinen lassen.
            try:
                parsed = json.loads(text)
            except ValueError:
                logger.warning("Tool %s returned non-JSON output", tool_name)
                parsed = None
            if isinstance(parsed, dict):
                result["structuredContent"] = parsed
            return _rpc_ok(req_id, result)
        except PermissionError as exc:
            _log_agent_call(user, tool_name, arguments, success=False, error=type(exc).__name__)
            return _rpc_ok(req_id, {
                "content": [{"type": "text", "text": f"Permission denied: {exc}"}],
                "isError": True,
            })
        except (FileNotFoundError, ValueError) as exc:
            _log_agent_call(user, tool_name, arguments, success=False, error=type(exc).__name__)
            return _rpc_ok(req_id, {
                "content": [{"type": "text", "text": str(exc)}],
                "isError": True,
            })
        except Exception as exc:
            logger.exception("MCP tool call failed: %s", tool_name)
            _log_agent_call(user, tool_name, arguments, success=False, error=type(exc).__name__)
            return _rpc_ok(req_id, {
                "content": [{"type": "text", "text": "Internal error. Check server logs for details."}],
                "isError": True,
            })

    return _rpc_err(req_id, -32601, f"Method not found: {method!r}")


async def _handle_payload(body, user: User | None) -> dict | list | None:
    """Handle a JSON-RPC message or batch. Returns None for notification-only payloads."""
    if isinstance(body, list):
        if not body:
            return _rpc_err(None, -32600, "Invalid Request")
        if len(body) > _MCP_MAX_JSONRPC_BATCH:
            return _rpc_err(None, -32600, f"JSON-RPC batch exceeds {_MCP_MAX_JSONRPC_BATCH} messages")
        responses = []
        for item in body:
            if not isinstance(item, dict):
                responses.append(_rpc_err(None, -32600, "Invalid Request"))
                continue
            response = await _handle_message(item, user)
            if response is not None:
                responses.append(response)
        return responses or None

    if not isinstance(body, dict):
        return _rpc_err(None, -32600, "Invalid Request")

    return await _handle_message(body, user)


# ─────────────────────────────────────────────────────────────────────────────
# Transport 1: Streamable HTTP  —  POST /mcp
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/")
@router.post("/mcp")
async def mcp_http(request: Request) -> Response:
    """Streamable HTTP transport (MCP spec 2025-03-26).

    Also accessible at the server root: ChatGPT-style connectors POST their
    JSON-RPC frames to ``/`` directly after the OAuth handshake.
    """
    user = _user_from_request(request)
    if user is None:
        return _unauthorized(request)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(_rpc_err(None, -32700, "Parse error"), status_code=400)

    response = await _handle_payload(body, user)
    if response is None:
        return Response(status_code=202)

    error = response.get("error") if isinstance(response, dict) else None
    if error:
        code = error.get("code", -32000)
        if code == -32601:
            # Method not found ist ein Protokoll-, kein Transportfehler: Die MCP-Spec
            # erwartet den JSON-RPC-Fehler mit HTTP 200. 404 liess den Server
            # defekt aussehen (eigener Regressionstest benennt das explizit).
            return JSONResponse(response)
        status = 403 if code == -32001 else 400
        return JSONResponse(response, status_code=status)

    return JSONResponse(response)


# ─────────────────────────────────────────────────────────────────────────────
# GET /mcp  —  Streamable HTTP server-to-client SSE stream (MCP spec 2025-03-26)
# Claude Desktop opens this first; 405 here causes "Couldn't reach the MCP server".
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/mcp")
async def mcp_http_sse(request: Request) -> StreamingResponse:
    """Server-initiated SSE stream for Streamable HTTP transport (MCP spec 2025-03-26)."""
    user = _user_from_request(request)
    if user is None:
        return _unauthorized(request)

    async def event_stream() -> AsyncGenerator[str, None]:
        try:
            while True:
                if await request.is_disconnected():
                    break
                await asyncio.sleep(20)
                yield ": keepalive\n\n"
        except asyncio.CancelledError:
            pass

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# Transport 2: HTTP + SSE  —  GET /mcp/sse  +  POST /mcp/messages
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/mcp/sse")
async def mcp_sse(request: Request):
    """
    SSE transport entry point (MCP spec 2024-11-05).
    Client connects here, receives an `endpoint` event, then POSTs to /mcp/messages.
    """
    user = _user_from_request(request)
    if user is None:
        return _unauthorized(request)

    if len(_sse_sessions) >= _SSE_MAX_SESSIONS:
        return JSONResponse({"error": "Too many active SSE sessions"}, status_code=503)
    session_id = str(uuid.uuid4())
    queue: asyncio.Queue = asyncio.Queue(maxsize=_SSE_QUEUE_MAX_MESSAGES)
    _sse_sessions[session_id] = (queue, user)

    base_url = _base_url(request)
    endpoint_url = f"{base_url}/mcp/messages?sessionId={session_id}"

    async def event_stream() -> AsyncGenerator[str, None]:
        # Tell the client where to POST its messages
        yield f"event: endpoint\ndata: {endpoint_url}\n\n"
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=25.0)
                    yield f"event: message\ndata: {json.dumps(message, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            _sse_sessions.pop(session_id, None)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/mcp/messages")
async def mcp_messages(request: Request, sessionId: str) -> JSONResponse:
    """
    SSE transport message receiver (MCP spec 2024-11-05).
    Processes the JSON-RPC request and pushes the response into the session queue.
    """
    session = _sse_sessions.get(sessionId)
    if session is None:
        return JSONResponse({"error": "Session not found or expired"}, status_code=404)
    queue, session_user = session

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Parse error"}, status_code=400)

    user = _user_from_request(request)
    if user is None:
        return _unauthorized(request)
    if session_user is None or user.username != session_user.username:
        return JSONResponse({"error": "Session user mismatch"}, status_code=403)
    response = await _handle_payload(body, user)
    if response is not None:
        try:
            queue.put_nowait(response)
        except asyncio.QueueFull:
            return JSONResponse({"error": "SSE response queue is full"}, status_code=429)

    return JSONResponse({}, status_code=202)


# ─────────────────────────────────────────────────────────────────────────────
# Tool dispatcher
# ─────────────────────────────────────────────────────────────────────────────



async def _dispatch(name: str, args: dict, user: User | None) -> str:
    # Multi-Tenancy: jeder MCP-Aufruf läuft im Namespace des authentifizierten Users.
    if user is not None and is_valid_username(user.username):
        set_user_ns(user.username)
        ensure_user_workspace(user.username)

    ctx = McpContext(args, user, dispatcher=_dispatch)
    handler = _HANDLERS.get(name)
    if handler is None:
        raise ValueError(f"Unknown tool: {name!r}")
    return await handler(ctx)
