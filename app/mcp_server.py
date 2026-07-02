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
import difflib
import fnmatch
import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets
import threading
import time
import uuid
from datetime import datetime
from typing import AsyncGenerator
from urllib.parse import urlencode, urlparse

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

import markdown as md_lib
import nh3

from .auth import ROLE_HIERARCHY, parse_users
from .models import User
from .search import deindex_file, get_db, index_file, init_db, reindex_all, search as fts_search
from .storage import (
    _read_frontmatter_only,
    append_file,
    create_note,
    delete_file,
    edit_file,
    list_all_files,
    list_files,
    move_folder,
    move_file,
    read_file,
    safe_path,
    update_frontmatter,
    validate_markdown_content_path,
    write_file,
)
from .tenancy import ensure_user_workspace, is_valid_username, set_user_ns, user_root

_NH3_TAGS = {
    "a", "abbr", "b", "blockquote", "br", "code", "div", "em", "h1", "h2", "h3",
    "h4", "h5", "h6", "hr", "i", "li", "ol", "p", "pre", "span", "strong",
    "table", "tbody", "td", "th", "thead", "tr", "ul",
}
_NH3_ATTRS = {
    "a": {"href", "title", "rel"},
    "code": {"class"},
    "span": {"class"},
    "div": {"class"},
    "th": {"align"},
    "td": {"align"},
}

router = APIRouter()
logger = logging.getLogger("kiwiki.mcp")

SUPPORTED_PROTOCOL_VERSIONS = {"2025-03-26", "2024-11-05"}
MCP_PROTOCOL_VERSION = "2025-03-26"

# Base URL for constructing SSE callback URLs — must match the public address.
# Falls back to the request's own base_url if not set.
_BASE_URL = os.getenv("KIWIKI_BASE_URL", "").rstrip("/")

# In-memory sessions for HTTP+SSE transport: session_id -> (queue, user)
# User is captured at GET /mcp/sse so POST /mcp/messages works without re-sending auth.
_sse_sessions: dict[str, tuple[asyncio.Queue, "User | None"]] = {}

# Minimal dynamic client registration and authorization-code storage.
# This keeps the legacy API-key-as-bearer-token behavior, but no longer exposes
# the API key in the front-channel OAuth redirect.
_oauth_clients: dict[str, dict] = {}
_oauth_codes: dict[str, dict] = {}
_OAUTH_CODE_TTL_SECONDS = 300
_oauth_tokens: dict[str, dict] = {}
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
_initialize_cache: dict[str, tuple[float, str]] = {}
_INITIALIZE_CACHE_TTL = 60  # seconds

_MCP_UPLOAD_TTL_SECONDS = int(os.getenv("KIWIKI_MCP_UPLOAD_TTL_SECONDS", "3600"))
_MCP_MAX_UPLOAD_BYTES = int(os.getenv("KIWIKI_MCP_MAX_UPLOAD_BYTES", str(10 * 1024 * 1024)))
_MCP_MAX_UPLOAD_CHUNKS = int(os.getenv("KIWIKI_MCP_MAX_UPLOAD_CHUNKS", "1000"))
_chunked_writes: dict[str, dict] = {}

# B6: Agent tracker — logs MCP tool calls to a JSONL file per namespace.
_agent_log_lock = threading.Lock()
_AGENT_LOG_MAX_BYTES = 5 * 1024 * 1024  # 5 MB max, then rotate
_AGENT_LOG_FILE = ".kiwiki/agent_log.jsonl"

# E2: Async grep jobs — background grep with polling.
_grep_jobs: dict[str, dict] = {}
_grep_job_counter = 0
_GREP_JOBS_MAX = 50
_GREP_JOB_TTL = 600  # 10 minutes


def _prune_grep_jobs() -> None:
    """Remove completed grep jobs older than _GREP_JOB_TTL or excess entries."""
    now = time.time()
    expired = [
        jid for jid, job in _grep_jobs.items()
        if job["status"] != "running" and now - job.get("created_at", 0) > _GREP_JOB_TTL
    ]
    for jid in expired:
        _grep_jobs.pop(jid, None)
    # Cap total jobs
    if len(_grep_jobs) > _GREP_JOBS_MAX:
        sorted_jobs = sorted(_grep_jobs.keys(), key=lambda k: _grep_jobs[k].get("created_at", 0))
        for jid in sorted_jobs[: len(_grep_jobs) - _GREP_JOBS_MAX]:
            _grep_jobs.pop(jid, None)


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
            "args": {k: v for k, v in args.items() if k != "content"},  # skip large content
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
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _base_url(request: Request) -> str:
    return _BASE_URL or str(request.base_url).rstrip("/")


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
            return api_key
    return None


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
    if not client:
        return False
    if client.get("expires_at", 0) < time.time():
        _oauth_clients.pop(client_id, None)
        return False
    return redirect_uri in client.get("redirect_uris", [])


def _prune_oauth_clients() -> None:
    now = time.time()
    expired = [client_id for client_id, client in _oauth_clients.items() if client.get("expires_at", 0) < now]
    for client_id in expired:
        _oauth_clients.pop(client_id, None)


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

    return HTMLResponse(
        f"""<!DOCTYPE html>
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
  </style>
</head>
<body>
  <h1>kiwiki</h1>
  <p>Gib deinen API-Key ein, um den MCP-Zugriff zu autorisieren.</p>
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

    users_map = parse_users()
    if not apikey or apikey not in users_map:
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

    if not redirect_uri:
        return HTMLResponse("<p>Autorisiert. Du kannst dieses Fenster schließen.</p>")

    if not _is_registered_redirect(client_id, redirect_uri):
        return JSONResponse({"error": "invalid_redirect_uri"}, status_code=400)
    if not _pkce_request_is_valid(code_challenge, code_challenge_method):
        return JSONResponse({"error": "invalid_request", "error_description": "PKCE S256 is required"}, status_code=400)
    if resource and resource != f"{_base_url(request)}/mcp":
        return JSONResponse({"error": "invalid_target", "error_description": "Unknown resource"}, status_code=400)

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
        api_key = _api_key_from_signed_token(refresh_token, expected_type="refresh")
        if not api_key:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        access_token = _make_oauth_token(api_key, "access", _OAUTH_TOKEN_TTL_SECONDS, client_id=client_id, resource=resource)
        return JSONResponse({
            "access_token": access_token,
            "token_type": _OAUTH_BEARER_VALUE,
            "expires_in": _OAUTH_TOKEN_TTL_SECONDS,
            "scope": "mcp",
        })

    if grant_type != "authorization_code":
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    record = _oauth_codes.pop(code, None) if code else None
    if not record or record["expires_at"] < time.time():
        return JSONResponse({"error": "invalid_grant"}, status_code=400)

    if record["client_id"] and client_id and client_id != record["client_id"]:
        return JSONResponse({"error": "invalid_grant"}, status_code=400)
    if redirect_uri and redirect_uri != record["redirect_uri"]:
        return JSONResponse({"error": "invalid_grant"}, status_code=400)
    if resource and record.get("resource") and resource != record["resource"]:
        return JSONResponse({"error": "invalid_target"}, status_code=400)

    code_challenge = record.get("code_challenge", "")
    if record.get("code_challenge_method") != "S256" or not code_challenge or not code_verifier:
        return JSONResponse({"error": "invalid_grant"}, status_code=400)
    if not secrets.compare_digest(_pkce_s256(code_verifier), code_challenge):
        return JSONResponse({"error": "invalid_grant"}, status_code=400)

    api_key = record["api_key"]
    if api_key not in parse_users():
        return JSONResponse({"error": "invalid_grant"}, status_code=400)

    token_resource = resource or record.get("resource", "")
    access_token = _make_oauth_token(api_key, "access", _OAUTH_TOKEN_TTL_SECONDS, client_id=client_id, resource=token_resource)
    refresh_token = _make_oauth_token(api_key, "refresh", _OAUTH_REFRESH_TOKEN_TTL_SECONDS, client_id=client_id, resource=token_resource)
    return JSONResponse({
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": _OAUTH_BEARER_VALUE,
        "expires_in": _OAUTH_TOKEN_TTL_SECONDS,
        "scope": "mcp",
    })


@router.post("/oauth/register")
async def oauth_register(request: Request):
    """Dynamic Client Registration (RFC 7591) — ChatGPT registers itself before OAuth flow."""
    _prune_oauth_clients()
    if len(_oauth_clients) >= _OAUTH_MAX_CLIENTS:
        return JSONResponse({"error": "server_error", "error_description": "Too many registered OAuth clients"}, status_code=503)
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
            "Fetches one markdown file by path and returns its frontmatter and content. "
            "This is a read-only alias for read_file, named for ChatGPT and Deep Research connector conventions."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path to the .md file"},
                "id": {"type": "string", "description": "Optional alias for path, used by some MCP clients"},
            },
            "required": [],
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
        "description": "Full-text search over all markdown files using SQLite FTS5.",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
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
        "description": "Permanently deletes a markdown file and removes it from the search index. Requires admin role.",
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
        "tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["path", "title", "updated", "tags"],
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

_OUTPUT_SCHEMAS = {
    "read_index": _STRING_MAP_SCHEMA,
    "list_files": {"type": "array", "items": _FILE_INFO_SCHEMA},
    "read_file": _FILE_CONTENT_SCHEMA,
    "fetch": _FILE_CONTENT_SCHEMA,
    "write_file": _STATUS_SCHEMA,
    "append_file": _STATUS_SCHEMA,
    "write_many": _BATCH_WRITE_SCHEMA,
    "chunked_write": _CHUNKED_WRITE_SCHEMA,
    "search": {"type": "array", "items": _SEARCH_RESULT_SCHEMA},
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
    "sort": {"type": "array", "items": _MOVE_RESULT_SCHEMA},
    "list_all_files": {"type": "array", "items": _ALL_FILE_SCHEMA},
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
    "recent_files": {"type": "array", "items": _RECENT_FILE_SCHEMA},
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
    "tag_index": {"type": "array", "items": _TAG_ENTRY_SCHEMA},
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
    "template": _STATUS_SCHEMA,
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
    "search_history": {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "timestamp": {"type": "number"},
                "result_count": {"type": "integer"},
            },
            "required": ["query", "timestamp", "result_count"],
        },
    },
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
}

_DESTRUCTIVE_TOOLS = {"delete_file", "move_file", "move_folder", "sort", "replace_many", "rename"}


def _tool_annotations(name: str) -> dict:
    read_only = name in _READ_ONLY_TOOLS
    destructive = name in _DESTRUCTIVE_TOOLS
    return {
        "readOnlyHint": read_only,
        "destructiveHint": destructive,
        "idempotentHint": name in {
            "read_index", "list_files", "read_file", "fetch", "search", "read_many",
            "list_all_files", "grep", "find", "file_info", "read_lines", "build_index",
            "recent_files", "backlinks", "preview_edit", "validate_wiki", "related_files",
            "tag_index", "reindex_all", "search_status", "whoami",
            "file_history", "diff", "statistics", "validate_links", "link_graph",
            "export", "duplicate_check", "ai_summarize",
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
    token_record = _oauth_tokens.get(bearer)
    if token_record:
        if token_record["expires_at"] < time.time():
            _oauth_tokens.pop(bearer, None)
            return None
        api_key = token_record["api_key"]
    else:
        api_key = _api_key_from_signed_token(bearer, expected_type="access") or bearer
    users_map = parse_users()
    if api_key not in users_map:
        return None
    username, role = users_map[api_key]
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
            "serverInfo": {"name": "kiwiki", "version": "0.1.0"},
            "instructions": (
                "This is kiwiki — a Markdown-based personal wiki. "
                "Follow these rules strictly:\n"
                "1. ALWAYS call read_index at the start of every session to get the current schema and navigation.\n"
                "2. ALWAYS call search before creating new content to avoid duplicates.\n"
                "3. Place notes in topic subfolders: notes/python/, notes/ml/, projects/kiwiki/ etc.\n"
                "4. Create a subfolder once 3+ files share a topic.\n"
                "5. Prefer edit/append_file over write_file for existing files.\n"
                "6. Use write_many for multi-file updates and chunked_write for large files or unreliable clients.\n"
                "7. Do not ask for confirmation for ordinary create, update, append, or index refresh operations.\n"
                "8. Ask before deleting files or running destructive reorganizations.\n"
                "9. Refresh index.md (via edit or build_index) after creating new folders.\n"
                "10. Always set complete frontmatter: title, type, created, updated, tags, owner."
            ) + schema_hint,
        })

    if method in ("notifications/initialized", "initialized"):
        return None if _is_notification(body) else _rpc_ok(req_id, {})

    if method == "tools/list":
        return _rpc_ok(req_id, {"tools": TOOLS})

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
            return _rpc_ok(req_id, {
                "content": [{"type": "text", "text": text}],
                "structuredContent": json.loads(text),
            })
        except PermissionError as exc:
            _log_agent_call(user, tool_name, arguments, success=False, error=str(exc))
            return _rpc_ok(req_id, {
                "content": [{"type": "text", "text": f"Permission denied: {exc}"}],
                "isError": True,
            })
        except (FileNotFoundError, ValueError) as exc:
            _log_agent_call(user, tool_name, arguments, success=False, error=str(exc))
            return _rpc_ok(req_id, {
                "content": [{"type": "text", "text": str(exc)}],
                "isError": True,
            })
        except Exception as exc:
            logger.exception("MCP tool call failed: %s", tool_name)
            _log_agent_call(user, tool_name, arguments, success=False, error=str(exc)[:200])
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
        status = 403 if code == -32001 else (404 if code == -32601 else 400)
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

    session_id = str(uuid.uuid4())
    queue: asyncio.Queue = asyncio.Queue()
    _sse_sessions[session_id] = (queue, user)

    base_url = _BASE_URL or str(request.base_url).rstrip("/")
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

    # Prefer auth from POST header; fall back to user captured at SSE handshake
    user = _user_from_request(request) or session_user
    response = await _handle_payload(body, user)
    if response is not None:
        await queue.put(response)

    return JSONResponse({}, status_code=202)


def _slug(path: str) -> str:
    return re.sub(r'[^a-zA-Z0-9_-]', '-', path).strip('-').lower()


def _markdown_paths(scope: str = ".") -> list:
    root = safe_path(scope)
    if not root.exists():
        raise FileNotFoundError(f"Path not found: {scope!r}")
    files = sorted(root.rglob("*.md")) if root.is_dir() else [root]
    return [path for path in files if path.is_file() and ".kiwiki" not in path.parts]


def _rel_path(path) -> str:
    return str(path.relative_to(user_root()))


def _file_summary(path) -> dict:
    rel = _rel_path(path)
    stat = path.stat()
    try:
        meta = _read_frontmatter_only(rel)
        title = meta.get("title", path.stem)
        updated = meta.get("updated", "")
        tags = meta.get("tags", [])
    except Exception:
        title = path.stem
        updated = ""
        tags = []
    return {
        "path": rel,
        "title": title,
        "updated": updated,
        "tags": tags if isinstance(tags, list) else [],
        "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
        "size_bytes": stat.st_size,
    }


def _local_markdown_links(text: str) -> list[tuple[int, str]]:
    links: list[tuple[int, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for match in re.finditer(r"\[[^\]]+\]\(([^)]+)\)", line):
            links.append((lineno, match.group(1).strip()))
        for match in re.finditer(r"\[\[([^\]]+)\]\]", line):
            links.append((lineno, match.group(1).strip()))
    return links


def _resolve_local_link(source_rel: str, link: str) -> str | None:
    target = link.split("#", 1)[0].split("?", 1)[0].strip()
    if not target or re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target):
        return None
    if not target.endswith(".md"):
        return None
    if target.startswith("/"):
        return target.lstrip("/")
    source_dir = os.path.dirname(source_rel)
    return os.path.normpath(os.path.join(source_dir, target)).replace("\\", "/")


def _frontmatter_title_and_tags(path: str) -> tuple[str, list[str], dict]:
    meta = _read_frontmatter_only(path)
    tags = meta.get("tags", [])
    if not isinstance(tags, list):
        tags = []
    return meta.get("title", os.path.splitext(os.path.basename(path))[0]), tags, meta


def _index_markdown(path: str) -> None:
    init_db()
    index_file(path)


def _deindex_markdown(path: str) -> None:
    init_db()
    deindex_file(path)


def _content_sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _write_markdown_content(path: str, content: str, mode: str = "replace", create_if_missing: bool = True) -> dict:
    mode = mode or "replace"
    if mode not in {"replace", "append"}:
        raise ValueError("mode must be 'replace' or 'append'")
    validate_markdown_content_path(path)

    if mode == "replace":
        fc = write_file(path, content)
        status = "written"
    else:
        filepath = safe_path(path)
        if filepath.exists():
            fc = append_file(path, content)
            status = "appended"
        elif create_if_missing:
            fc = write_file(path, content)
            status = "created"
        else:
            raise FileNotFoundError(f"File not found: {path!r}")
    _index_markdown(path)
    # Invalidate initialize cache when index.md or AGENTS.md change
    if path in ("index.md", "AGENTS.md"):
        _initialize_cache.clear()
    dumped = frontmatter_dump_payload(fc.content, fc.frontmatter)
    return {
        "path": fc.path,
        "status": status,
        "mode": mode,
        "bytes": len(dumped.encode("utf-8")),
        "sha256": _content_sha256(content),
    }


def frontmatter_dump_payload(content: str, metadata: dict) -> str:
    import frontmatter

    return frontmatter.dumps(frontmatter.Post(content, **metadata))


def _chunk_key(user: User | None, upload_id: str) -> str:
    username = user.username if user is not None else "anonymous"
    return f"{username}:{upload_id}"


def _prune_chunked_writes(now: float | None = None) -> None:
    now = now if now is not None else time.time()
    expired = [
        key
        for key, state in _chunked_writes.items()
        if now - float(state.get("updated_at", 0)) > _MCP_UPLOAD_TTL_SECONDS
    ]
    for key in expired:
        _chunked_writes.pop(key, None)


def _stage_chunked_write(args: dict, user: User | None) -> dict:
    path = args["path"]
    validate_markdown_content_path(path)
    mode = args.get("mode", "replace") or "replace"
    if mode not in {"replace", "append"}:
        raise ValueError("mode must be 'replace' or 'append'")

    upload_id = args.get("upload_id") or f"{path}:{mode}"
    chunk = args.get("chunk", "")
    chunk_index = int(args["chunk_index"])
    if chunk_index < 0:
        raise ValueError("chunk_index must be >= 0")
    total_chunks = args.get("total_chunks")
    if total_chunks is not None:
        total_chunks = int(total_chunks)
        if total_chunks <= 0:
            raise ValueError("total_chunks must be > 0")
        if chunk_index >= total_chunks:
            raise ValueError("chunk_index must be less than total_chunks")

    _prune_chunked_writes()
    key = _chunk_key(user, str(upload_id))
    now = time.time()
    state = _chunked_writes.setdefault(
        key,
        {
            "path": path,
            "mode": mode,
            "chunks": {},
            "created_at": now,
            "updated_at": now,
            "total_chunks": total_chunks,
        },
    )
    if state["path"] != path or state["mode"] != mode:
        raise ValueError("upload_id is already used for a different path or mode")

    chunks: dict[int, str] = state["chunks"]
    if len(chunks) >= _MCP_MAX_UPLOAD_CHUNKS and chunk_index not in chunks:
        raise ValueError(f"Too many chunks (max {_MCP_MAX_UPLOAD_CHUNKS})")
    if chunk_index in chunks and chunks[chunk_index] != chunk:
        raise ValueError(f"chunk_index {chunk_index} already contains different content")
    chunks[chunk_index] = chunk
    if total_chunks is not None:
        previous_total = state.get("total_chunks")
        if previous_total is not None and previous_total != total_chunks:
            raise ValueError("total_chunks changed for this upload_id")
        state["total_chunks"] = total_chunks
    state["updated_at"] = now

    received_bytes = sum(len(part.encode("utf-8")) for part in chunks.values())
    if received_bytes > _MCP_MAX_UPLOAD_BYTES:
        _chunked_writes.pop(key, None)
        raise ValueError(f"Staged upload exceeds max size {_MCP_MAX_UPLOAD_BYTES} bytes")

    expected_total = state.get("total_chunks")
    if bool(args.get("finalize")):
        if expected_total is None:
            expected_total = max(chunks) + 1 if chunks else 0
        missing = [idx for idx in range(expected_total) if idx not in chunks]
        if missing:
            return {
                "path": path,
                "upload_id": upload_id,
                "status": "missing_chunks",
                "mode": mode,
                "received_chunks": len(chunks),
                "total_chunks": expected_total,
                "received_bytes": received_bytes,
                "missing_chunks": missing,
            }

        content = "".join(chunks[idx] for idx in range(expected_total))
        expected_sha = args.get("expected_sha256")
        actual_sha = _content_sha256(content)
        if expected_sha and not hmac.compare_digest(str(expected_sha).lower(), actual_sha):
            raise ValueError(f"sha256 mismatch: expected {expected_sha}, got {actual_sha}")

        result = _write_markdown_content(
            path,
            content,
            mode=mode,
            create_if_missing=bool(args.get("create_if_missing", True)),
        )
        _chunked_writes.pop(key, None)
        return {
            "path": path,
            "upload_id": upload_id,
            "status": result["status"],
            "mode": mode,
            "received_chunks": len(chunks),
            "total_chunks": expected_total,
            "received_bytes": received_bytes,
            "bytes": result["bytes"],
            "sha256": actual_sha,
        }

    return {
        "path": path,
        "upload_id": upload_id,
        "status": "staged",
        "mode": mode,
        "received_chunks": len(chunks),
        "total_chunks": expected_total,
        "received_bytes": received_bytes,
        "missing_chunks": [],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Tool dispatcher
# ─────────────────────────────────────────────────────────────────────────────

async def _dispatch(name: str, args: dict, user: User | None) -> str:
    # Multi-Tenancy: jeder MCP-Aufruf läuft im Namespace des authentifizierten Users.
    if user is not None and is_valid_username(user.username):
        set_user_ns(user.username)
        ensure_user_workspace(user.username)

    def _need_read():
        if user is None:
            raise PermissionError("Authentication required")

    def _need_write():
        if user is None:
            raise PermissionError("Authentication required")
        if ROLE_HIERARCHY.get(user.role, -1) < ROLE_HIERARCHY["write"]:
            raise PermissionError("Write permission required")

    def _need_admin():
        if user is None:
            raise PermissionError("Authentication required")
        if ROLE_HIERARCHY.get(user.role, -1) < ROLE_HIERARCHY["admin"]:
            raise PermissionError("Admin permission required")

    if name == "read_index":
        _need_read()
        out = {}
        for fname in ("index.md", "AGENTS.md"):
            try:
                fc = read_file(fname)
                out[fname] = fc.content
            except Exception as exc:
                out[fname] = f"[Error: {exc}]"
        return json.dumps(out, ensure_ascii=False, indent=2)

    if name == "list_files":
        _need_read()
        items = list_files(args.get("path", "."))
        return json.dumps([i.model_dump() for i in items], ensure_ascii=False, indent=2)

    if name in ("read_file", "fetch"):
        _need_read()
        path = args.get("path") or args.get("id")
        if not path:
            raise ValueError("Missing required argument: path")
        fc = read_file(path)
        return json.dumps(
            {"path": fc.path, "frontmatter": fc.frontmatter, "content": fc.content},
            ensure_ascii=False, indent=2,
        )

    if name == "write_file":
        _need_write()
        path = args["path"]
        result = _write_markdown_content(path, args["content"], mode="replace")
        return json.dumps({"path": result["path"], "status": result["status"]}, ensure_ascii=False)

    if name == "append_file":
        _need_write()
        path = args["path"]
        filepath = safe_path(path)
        if not filepath.exists():
            raise FileNotFoundError(f"File not found: {path}")
        result = _write_markdown_content(path, args["content"], mode="append", create_if_missing=False)
        return json.dumps({"path": result["path"], "status": result["status"]}, ensure_ascii=False)

    if name == "write_many":
        _need_write()
        files = args.get("files", [])
        if not isinstance(files, list) or not files:
            raise ValueError("Missing required argument: files")
        results = []
        for item in files:
            path = str(item.get("path", ""))
            mode = item.get("mode", "replace")
            try:
                result = _write_markdown_content(
                    path,
                    str(item.get("content", "")),
                    mode=mode,
                    create_if_missing=bool(item.get("create_if_missing", True)),
                )
                results.append(result)
            except Exception as exc:
                results.append({"path": path, "status": "error", "mode": mode, "error": str(exc)})
        written = sum(1 for item in results if item["status"] != "error")
        return json.dumps({"results": results, "written": written, "failed": len(results) - written}, ensure_ascii=False, indent=2)

    if name == "chunked_write":
        _need_write()
        return json.dumps(_stage_chunked_write(args, user), ensure_ascii=False, indent=2)

    if name == "search":
        _need_read()
        results = fts_search(args["query"])
        return json.dumps([r.model_dump() for r in results], ensure_ascii=False, indent=2)

    if name == "create_note":
        _need_write()
        owner = user.username if user else "unknown"
        path = create_note(
            title=args["title"],
            content=args.get("content", ""),
            tags=args.get("tags", []),
            owner=owner,
            folder=args.get("folder", "notes"),
        )
        _index_markdown(path)
        return json.dumps({"path": path, "status": "created"}, ensure_ascii=False)

    if name == "delete_file":
        _need_admin()
        path = args["path"]
        delete_file(path)
        _deindex_markdown(path)
        return json.dumps({"path": path, "status": "deleted"}, ensure_ascii=False)

    if name == "move_file":
        _need_write()
        src, dst = args["src"], args["dst"]
        fc = move_file(src, dst)
        _deindex_markdown(src)
        _index_markdown(dst)
        return json.dumps({"src": src, "dst": dst, "status": "moved"}, ensure_ascii=False)

    if name == "edit":
        _need_write()
        fc = edit_file(args["path"], new_str=args["new_str"], old_str=args.get("old_str", ""))
        _index_markdown(args["path"])
        mode = "replaced" if args.get("old_str") else "appended"
        return json.dumps({"path": fc.path, "status": mode}, ensure_ascii=False)

    if name == "update_frontmatter":
        _need_write()
        fc = update_frontmatter(args["path"], args["updates"])
        _index_markdown(args["path"])
        return json.dumps({"path": fc.path, "frontmatter": fc.frontmatter, "status": "updated"}, ensure_ascii=False, indent=2)

    if name == "read_many":
        _need_read()
        result = {}
        for path in args.get("paths", []):
            try:
                fc = read_file(path)
                result[path] = {"frontmatter": fc.frontmatter, "content": fc.content}
            except Exception as exc:
                result[path] = {"error": str(exc)}
        return json.dumps(result, ensure_ascii=False, indent=2)

    if name == "build_index":
        _need_write()
        from datetime import date
        all_files = list_all_files(".")
        # Group by top-level folder (or root for AGENTS.md / index.md)
        groups: dict[str, list[dict]] = {}
        for f in all_files:
            parts = f["path"].split("/")
            folder = parts[0] if len(parts) > 1 else "_root"
            groups.setdefault(folder, []).append(f)
        lines = [
            "---",
            'title: "kiwiki Wissensindex"',
            'type: "index"',
            f'updated: "{date.today().isoformat()}"',
            'owner: "system"',
            "---",
            "",
            "# kiwiki Wissensindex",
            "",
            "Zentrale Navigation. KI-Systeme: lies zuerst `AGENTS.md`.",
            "Automatisch generiert via `build_index`.",
            "",
        ]
        folder_labels = {
            "_root": "Systemdateien",
            "decisions": "Entscheidungen `/decisions/`",
            "notes": "Notizen `/notes/`",
            "projects": "Projekte `/projects/`",
            "shared": "Gemeinsam `/shared/`",
            "users": "Persönlich `/users/`",
        }
        # Sort folders: _root first, then alphabetically
        ordered = ["_root"] + sorted(k for k in groups if k != "_root")
        for folder in ordered:
            if folder not in groups:
                continue
            label = folder_labels.get(folder, f"`/{folder}/`")
            lines.append(f"## {label}")
            lines.append("")
            for f in sorted(groups[folder], key=lambda x: x["path"]):
                if f["path"] in ("index.md",):
                    continue
                title = f["title"] or f["path"]
                lines.append(f'- [{title}]({f["path"]})')
            lines.append("")
        index_content = "\n".join(lines)
        write_file("index.md", index_content)
        _index_markdown("index.md")
        return json.dumps({"status": "rebuilt", "sections": len(groups)}, ensure_ascii=False)

    if name == "sort":
        _need_write()
        results = []
        for m in args.get("moves", []):
            src, dst = m["src"], m["dst"]
            try:
                move_file(src, dst)
                _deindex_markdown(src)
                _index_markdown(dst)
                results.append({"src": src, "dst": dst, "status": "moved"})
            except Exception as exc:
                results.append({"src": src, "dst": dst, "status": "error", "error": str(exc)})
        return json.dumps(results, ensure_ascii=False, indent=2)

    if name == "list_all_files":
        _need_read()
        items = list_all_files(args.get("path", "."))
        return json.dumps(items, ensure_ascii=False, indent=2)

    if name == "grep":
        _need_read()
        pattern = args["pattern"]
        scope = args.get("path", ".")
        context_n = int(args.get("context_lines", 2))
        max_results = int(args.get("max_results", 100))
        flags = 0 if args.get("case_sensitive", False) else re.IGNORECASE

        # --- ReDoS-Hardening ---------------------------------------------
        # 1. Pattern-Sanity: zu lang oder mit gestapelten Quantoren
        #    (a+)+, (a*)*, (.+)+, (a|a)+ etc. — klassische katastrophale
        #    Backtracking-Muster ablehnen, BEVOR wir kompilieren.
        if len(pattern) > 500:
            raise ValueError("Regex pattern too long (max 500 chars)")
        _REDOS_PATTERNS = (
            r"\(\.\*\)\+",
            r"\(\.\+\)\+",
            r"\([^)]*\+\)\+",
            r"\([^)]*\*\)\*",
            r"\([^)]*\+\)\*",
            r"\([^)]*\*\)\+",
            r"\(.+\|.+\)\+",
        )
        for sus in _REDOS_PATTERNS:
            if re.search(sus, pattern):
                raise ValueError(
                    f"Regex pattern rejected (likely ReDoS): nested quantifier {sus!r}"
                )

        try:
            compiled = re.compile(pattern, flags)
        except re.error as exc:
            raise ValueError(f"Invalid regex pattern: {exc}") from exc

        root = safe_path(scope)
        if not root.exists():
            raise FileNotFoundError(f"Path not found: {scope!r}")

        # 2. Globales Timeout: der gesamte grep-Aufruf darf nicht laenger
        #    als GREP_TIMEOUT_S laufen — egal wie viele Dateien / Zeilen.
        GREP_TIMEOUT_S = 30.0
        PER_FILE_TIMEOUT_S = 5.0

        # Snapshot der Dateiliste (sync ist hier ok, ein Aufruf)
        if root.is_dir():
            files = sorted(root.rglob("*.md"))
        else:
            files = [root]

        def _scan_one(filepath, rel, compiled, context_n):
            """Liest eine Datei und sucht — laeuft im Thread, also blockiert
            es nicht den Event-Loop. Re.compile wurde bereits oben gemacht."""
            try:
                text = filepath.read_text(encoding="utf-8")
            except Exception:
                return []
            lines = text.splitlines()
            hits = []
            for i, line in enumerate(lines):
                if compiled.search(line):
                    hits.append({
                        "file": rel,
                        "line": i + 1,
                        "text": line,
                        "context_before": lines[max(0, i - context_n):i],
                        "context_after": lines[i + 1:i + 1 + context_n],
                    })
            return hits

        async def _scan_all() -> list:
            out: list = []

            async def _scan_file(filepath):
                rel = str(filepath.relative_to(user_root()))
                try:
                    return await asyncio.wait_for(
                        asyncio.to_thread(_scan_one, filepath, rel, compiled, context_n),
                        timeout=PER_FILE_TIMEOUT_S,
                    )
                except asyncio.TimeoutError:
                    logger.warning("grep: file %s timed out after %.1fs, skipped", rel, PER_FILE_TIMEOUT_S)
                    return []

            # Scan files in parallel batches for better throughput
            BATCH_SIZE = 8
            for i in range(0, len(files), BATCH_SIZE):
                batch = files[i:i + BATCH_SIZE]
                results = await asyncio.gather(*(_scan_file(fp) for fp in batch))
                for hits in results:
                    out.extend(hits)
                    if len(out) >= max_results:
                        return out[:max_results]
            return out

        try:
            matches = await asyncio.wait_for(_scan_all(), timeout=GREP_TIMEOUT_S)
        except asyncio.TimeoutError:
            return json.dumps(
                {"error": "Grep aborted: exceeded global timeout", "truncated": True},
                ensure_ascii=False, indent=2,
            )

        return json.dumps(
            {
                "matches": matches,
                "truncated": len(matches) >= max_results,
                "total_shown": len(matches),
            },
            ensure_ascii=False, indent=2,
        )

    if name == "find":
        _need_read()
        pattern = args["pattern"]
        scope = args.get("path", ".")
        root = safe_path(scope)
        if not root.exists():
            raise FileNotFoundError(f"Path not found: {scope!r}")

        results = []
        files = sorted(root.rglob("*")) if root.is_dir() else [root]
        for filepath in files:
            if filepath.is_file() and fnmatch.fnmatch(filepath.name, pattern):
                results.append(str(filepath.relative_to(user_root())))
        return json.dumps({"matches": results, "count": len(results)}, ensure_ascii=False, indent=2)

    if name == "file_info":
        _need_read()
        filepath = safe_path(args["path"])
        if not filepath.exists():
            raise FileNotFoundError(f"File not found: {args['path']!r}")
        stat = filepath.stat()
        try:
            text = filepath.read_text(encoding="utf-8")
            line_count = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
        except Exception:
            line_count = None
        import datetime
        return json.dumps({
            "path": args["path"],
            "size_bytes": stat.st_size,
            "line_count": line_count,
            "modified": datetime.datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
        }, ensure_ascii=False, indent=2)

    if name == "read_lines":
        _need_read()
        filepath = safe_path(args["path"])
        if not filepath.exists():
            raise FileNotFoundError(f"File not found: {args['path']!r}")
        lines = filepath.read_text(encoding="utf-8").splitlines()
        total = len(lines)

        if "tail" in args and args["tail"] is not None:
            n = int(args["tail"])
            slice_ = lines[max(0, total - n):]
            offset = max(0, total - n)
        else:
            start = max(1, int(args.get("start", 1))) - 1
            end = min(total, int(args.get("end", total)))
            slice_ = lines[start:end]
            offset = start

        result = [{"line": offset + i + 1, "text": ln} for i, ln in enumerate(slice_)]
        return json.dumps({"path": args["path"], "total_lines": total, "lines": result}, ensure_ascii=False, indent=2)

    if name == "recent_files":
        _need_read()
        limit = max(1, min(int(args.get("limit", 20)), 200))
        include_system = bool(args.get("include_system", False))
        files = _markdown_paths(args.get("path", "."))
        if not include_system:
            files = [p for p in files if _rel_path(p) not in {"index.md", "AGENTS.md"}]
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return json.dumps([_file_summary(p) for p in files[:limit]], ensure_ascii=False, indent=2)

    if name == "backlinks":
        _need_read()
        target = args["path"].strip("/")
        matches = []
        for filepath in _markdown_paths(args.get("scope", ".")):
            rel = _rel_path(filepath)
            if rel == target:
                continue
            try:
                text = filepath.read_text(encoding="utf-8")
            except Exception:
                logger.debug("backlinks: cannot read %s", rel, exc_info=True)
                continue
            title, _, _ = _frontmatter_title_and_tags(rel)
            lines = text.splitlines()
            for lineno, link in _local_markdown_links(text):
                if _resolve_local_link(rel, link) == target:
                    matches.append({"path": rel, "title": title, "line": lineno, "text": lines[lineno - 1]})
            for lineno, line in enumerate(lines, start=1):
                if target in line and not any(m["path"] == rel and m["line"] == lineno for m in matches):
                    matches.append({"path": rel, "title": title, "line": lineno, "text": line})
        return json.dumps({"target": target, "matches": matches, "count": len(matches)}, ensure_ascii=False, indent=2)

    if name == "move_folder":
        _need_write()
        src, dst = args["src"].strip("/"), args["dst"].strip("/")
        moved_before = [_rel_path(p) for p in _markdown_paths(src)]
        move_folder(src, dst)
        for old_path in moved_before:
            new_path = old_path.replace(src.rstrip("/") + "/", dst.rstrip("/") + "/", 1)
            _deindex_markdown(old_path)
            _index_markdown(new_path)
        return json.dumps({"src": src, "dst": dst, "status": "moved", "moved_files": len(moved_before)}, ensure_ascii=False)

    if name == "preview_edit":
        _need_read()
        fc = read_file(args["path"])
        old_str = args.get("old_str", "")
        new_str = args["new_str"]
        if old_str:
            if old_str not in fc.content:
                raise ValueError(f"String not found in {args['path']!r}")
            after = fc.content.replace(old_str, new_str, 1)
            mode = "replace"
        else:
            after = fc.content.rstrip("\n") + "\n\n" + new_str
            mode = "append"
        diff = "".join(difflib.unified_diff(
            fc.content.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"{args['path']} before",
            tofile=f"{args['path']} after",
            n=int(args.get("context_lines", 3)),
        ))
        return json.dumps({"path": args["path"], "mode": mode, "changed": fc.content != after, "diff": diff}, ensure_ascii=False, indent=2)

    if name == "replace_many":
        _need_write()
        paths = args.get("paths") or ([args["path"]] if args.get("path") else [])
        if not paths:
            raise ValueError("Missing required argument: path or paths")
        replacements = args.get("replacements", [])
        if not replacements:
            raise ValueError("Missing required argument: replacements")
        results = []
        total = 0
        for rel in paths:
            filepath = safe_path(rel)
            if not filepath.exists():
                raise FileNotFoundError(f"File not found: {rel!r}")
            if not filepath.is_file() or not rel.endswith(".md"):
                raise ValueError(f"Not a markdown file: {rel!r}")
            text = filepath.read_text(encoding="utf-8")
            changed = text
            count = 0
            for repl in replacements:
                old_str = repl["old_str"]
                new_str = repl["new_str"]
                occurrences = changed.count(old_str)
                if occurrences:
                    changed = changed.replace(old_str, new_str)
                    count += occurrences
            if changed != text:
                filepath.write_text(changed, encoding="utf-8")
                _index_markdown(rel)
            total += count
            results.append({"path": rel, "replacements": count, "changed": changed != text})
        return json.dumps({"results": results, "total_replacements": total}, ensure_ascii=False, indent=2)

    if name == "validate_wiki":
        _need_read()
        required = args.get("required_frontmatter") or ["title", "type", "created", "updated", "tags", "owner"]
        files = _markdown_paths(args.get("path", "."))
        issues = []
        titles: dict[str, list[str]] = {}
        existing = {_rel_path(p) for p in files}
        for filepath in files:
            rel = _rel_path(filepath)
            try:
                # Single read: extract frontmatter from lightweight read, links from full text
                meta = _read_frontmatter_only(rel)
                title = meta.get("title", "")
                if title:
                    titles.setdefault(str(title).lower(), []).append(rel)
                for field in required:
                    if field not in meta or meta.get(field) in ("", None):
                        issues.append({"path": rel, "type": "missing_frontmatter", "message": f"Missing frontmatter field: {field}"})
                text = filepath.read_text(encoding="utf-8")
            except Exception as exc:
                issues.append({"path": rel, "type": "read_error", "message": str(exc)})
                continue
            for lineno, link in _local_markdown_links(text):
                target = _resolve_local_link(rel, link)
                if target and target not in existing and not safe_path(target).exists():
                    issues.append({"path": rel, "type": "broken_link", "line": lineno, "message": f"Broken link: {link}"})
        for title, paths in titles.items():
            if len(paths) > 1:
                for rel in paths:
                    issues.append({"path": rel, "type": "duplicate_title", "message": f"Duplicate title: {title}"})
        return json.dumps({"checked_files": len(files), "issue_count": len(issues), "issues": issues}, ensure_ascii=False, indent=2)

    if name == "upsert_note":
        _need_write()
        folder = args.get("folder", "notes").strip("/") or "notes"
        path = args.get("path", "").strip("/")
        title = args["title"]
        content = args.get("content", "")
        mode = args.get("mode", "append")
        if mode not in {"append", "replace"}:
            raise ValueError("mode must be 'append' or 'replace'")
        existing_path = path if path and safe_path(path).exists() else ""
        if not existing_path:
            for item in list_all_files(folder):
                if item.get("title", "").casefold() == title.casefold():
                    existing_path = item["path"]
                    break
        if existing_path:
            if mode == "replace":
                fc = read_file(existing_path)
                if fc.content:
                    edit_file(existing_path, new_str=content, old_str=fc.content)
                else:
                    edit_file(existing_path, new_str=content)
                status = "replaced"
            else:
                append_file(existing_path, content)
                status = "appended"
            if args.get("tags"):
                update_frontmatter(existing_path, {"tags": args["tags"]})
            _index_markdown(existing_path)
            return json.dumps({"path": existing_path, "status": status}, ensure_ascii=False)
        new_path = create_note(title=title, content=content, tags=args.get("tags", []), owner=user.username if user else "unknown", folder=folder)
        _index_markdown(new_path)
        return json.dumps({"path": new_path, "status": "created"}, ensure_ascii=False)

    if name == "related_files":
        _need_read()
        target_path = args["path"]
        limit = max(1, min(int(args.get("limit", 10)), 100))
        target_title, target_tags, target_fm = _frontmatter_title_and_tags(target_path)
        target_tags_set = set(target_tags)
        explicit_related = set(target_fm.get("related", []) if isinstance(target_fm.get("related", []), list) else [])
        related = []
        backlinks_text = await _dispatch("backlinks", {"path": target_path}, user)
        backlink_paths = {m["path"] for m in json.loads(backlinks_text)["matches"]}
        for item in list_all_files("."):
            rel = item["path"]
            if rel == target_path:
                continue
            title, tags, fm = _frontmatter_title_and_tags(rel)
            reasons = []
            score = 0
            shared = sorted(target_tags_set.intersection(tags))
            if shared:
                score += len(shared) * 3
                reasons.append("shared_tags:" + ",".join(shared))
            if rel in explicit_related or target_path in (fm.get("related", []) if isinstance(fm.get("related", []), list) else []):
                score += 5
                reasons.append("frontmatter_related")
            if rel in backlink_paths:
                score += 4
                reasons.append("backlink")
            if score:
                related.append({"path": rel, "title": title, "score": score, "reasons": reasons, "tags": tags})
        related.sort(key=lambda item: (-item["score"], item["path"]))
        return json.dumps({"path": target_path, "related": related[:limit], "count": min(len(related), limit)}, ensure_ascii=False, indent=2)

    if name == "tag_index":
        _need_read()
        tags: dict[str, list[str]] = {}
        for filepath in _markdown_paths(args.get("path", ".")):
            rel = _rel_path(filepath)
            _, file_tags, _ = _frontmatter_title_and_tags(rel)
            for tag in file_tags:
                tags.setdefault(str(tag), []).append(rel)
        result = [{"tag": tag, "count": len(files), "files": sorted(files)} for tag, files in sorted(tags.items())]
        return json.dumps(result, ensure_ascii=False, indent=2)

    if name == "reindex_all":
        _need_write()
        count = reindex_all()
        return json.dumps({"status": "rebuilt", "indexed_files": count}, ensure_ascii=False)

    if name == "search_status":
        _need_read()
        markdown_count = len(_markdown_paths("."))
        init_db()
        with get_db() as conn:
            indexed_count = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            database = conn.execute("PRAGMA database_list").fetchone()[2]
        return json.dumps({"markdown_files": markdown_count, "indexed_files": indexed_count, "database": database}, ensure_ascii=False, indent=2)

    if name == "whoami":
        _need_read()
        return json.dumps({"username": user.username, "role": user.role, "workspace": str(user_root())}, ensure_ascii=False, indent=2)

    if name == "git_commit":
        _need_write()
        import subprocess
        message = args["message"]
        root = user_root()
        subprocess.run(["git", "add", "-A"], cwd=root, capture_output=True, check=True)
        result = subprocess.run(["git", "commit", "-m", message], cwd=root, capture_output=True, text=True)
        if result.returncode != 0 and "nothing to commit" in result.stdout:
            return json.dumps({"commit_hash": "", "message": "No changes to commit", "files_changed": 0}, ensure_ascii=False)
        hash_result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True)
        commit_hash = hash_result.stdout.strip()
        diff_result = subprocess.run(["git", "diff", "--stat", "HEAD~1..HEAD"], cwd=root, capture_output=True, text=True)
        files_changed = len(diff_result.stdout.strip().splitlines()) if diff_result.stdout.strip() else 0
        return json.dumps({"commit_hash": commit_hash, "message": message, "files_changed": files_changed}, ensure_ascii=False)

    if name == "file_history":
        _need_read()
        import subprocess
        path = args["path"]
        limit = int(args.get("limit", 10))
        root = user_root()
        result = subprocess.run(
            ["git", "log", f"-{limit}", "--pretty=format:%H|%aI|%an|%s", "--", path],
            cwd=root, capture_output=True, text=True,
        )
        history = []
        for line in result.stdout.strip().splitlines():
            if "|" in line:
                parts = line.split("|", 3)
                if len(parts) == 4:
                    history.append({"hash": parts[0], "date": parts[1], "author": parts[2], "message": parts[3]})
        return json.dumps({"path": path, "history": history}, ensure_ascii=False, indent=2)

    if name == "diff":
        _need_read()
        import subprocess
        path = args.get("path")
        from_commit = args.get("from_commit", "HEAD~1")
        to_commit = args.get("to_commit", "HEAD")
        root = user_root()
        cmd = ["git", "diff", f"{from_commit}..{to_commit}"]
        if path:
            cmd.append(path)
        result = subprocess.run(cmd, cwd=root, capture_output=True, text=True)
        stat_result = subprocess.run(
            ["git", "diff", "--stat", f"{from_commit}..{to_commit}"] + ([path] if path else []),
            cwd=root, capture_output=True, text=True,
        )
        files_changed = len(stat_result.stdout.strip().splitlines()) if stat_result.stdout.strip() else 0
        return json.dumps({"diff": result.stdout, "files_changed": files_changed}, ensure_ascii=False, indent=2)

    if name == "statistics":
        _need_read()
        scope = args.get("path", ".")
        all_files = list_all_files(scope)
        total_files = len(all_files)
        files_by_folder: dict[str, int] = {}
        tag_counts: dict[str, int] = {}
        for f in all_files:
            parts = f["path"].split("/")
            folder = parts[0] if len(parts) > 1 else "_root"
            files_by_folder[folder] = files_by_folder.get(folder, 0) + 1
            for tag in f.get("tags", []):
                tag_counts[str(tag)] = tag_counts.get(str(tag), 0) + 1

        def _count_words(f):
            try:
                fc = read_file(f["path"])
                return len(fc.content.split()), len(fc.content)
            except Exception:
                return 0, 0

        # Count words in parallel batches
        total_words = 0
        total_chars = 0
        BATCH_SIZE = 8
        for i in range(0, len(all_files), BATCH_SIZE):
            batch = all_files[i:i + BATCH_SIZE]
            results = await asyncio.gather(*(asyncio.to_thread(_count_words, f) for f in batch))
            for words, chars in results:
                total_words += words
                total_chars += chars

        top_tags = [{"tag": t, "count": c} for t, c in sorted(tag_counts.items(), key=lambda x: -x[1])[:20]]
        dated = [f for f in all_files if f.get("updated")]
        dated.sort(key=lambda x: x.get("updated", ""), reverse=True)
        most_recent = [{"path": f["path"], "title": f["title"], "updated": f["updated"]} for f in dated[:5]]
        oldest = [{"path": f["path"], "title": f["title"], "updated": f["updated"]} for f in dated[-5:]] if dated else []
        return json.dumps({
            "total_files": total_files, "total_words": total_words, "total_chars": total_chars,
            "files_by_folder": files_by_folder, "top_tags": top_tags,
            "most_recent_files": most_recent, "oldest_files": oldest,
        }, ensure_ascii=False, indent=2)

    if name == "template":
        _need_write()
        from datetime import date
        template_type = args["template_type"]
        title = args["title"]
        folder_map = {
            "meeting": "notes/meetings", "decision": "decisions", "adr": "decisions",
            "review": "notes/reviews", "bug": "notes/bugs", "feature": "notes/features",
        }
        folder = args.get("folder") or folder_map.get(template_type, "notes")
        today = date.today().isoformat()
        slug = title.lower().replace(" ", "-").replace("/", "-")
        slug = "".join(c for c in slug if c.isalnum() or c in "-_")[:60]
        path = f"{folder}/{slug}.md"
        i = 2
        while safe_path(path).exists():
            path = f"{folder}/{slug}-{i}.md"
            i += 1
        templates = {
            "meeting": f"---\ntitle: \"{title}\"\ntype: meeting\ncreated: \"{today}\"\nupdated: \"{today}\"\ntags: [meeting]\nowner: \"{user.username}\"\n---\n\n## Agenda\n\n- \n\n## Teilnehmer\n\n- \n\n## Beschlüsse\n\n- \n\n## Action Items\n\n| Wer | Was | Bis |\n|-----|-----|-----|\n|  |  |  |",
            "decision": f"---\ntitle: \"{title}\"\ntype: decision\ncreated: \"{today}\"\nupdated: \"{today}\"\ntags: [decision, adr]\nowner: \"{user.username}\"\n---\n\n## Context\n\nWas ist die Situation?\n\n## Decision\n\nWas wurde entschieden?\n\n## Consequences\n\n### Positiv\n\n- \n\n### Negativ\n\n- \n\n## Alternatives considered\n\n- ",
            "adr": None,
            "review": f"---\ntitle: \"{title}\"\ntype: review\ncreated: \"{today}\"\nupdated: \"{today}\"\ntags: [review]\nowner: \"{user.username}\"\n---\n\n## Summary\n\nKurze Zusammenfassung.\n\n## Findings\n\n### Positive\n\n- \n\n### Issues\n\n| Severity | File | Line | Description |\n|----------|------|------|-------------|\n|  |  |  |  |\n\n## Approval\n\n- [ ] Approved\n- [ ] Changes requested",
            "bug": f"---\ntitle: \"{title}\"\ntype: bug\ncreated: \"{today}\"\nupdated: \"{today}\"\ntags: [bug]\nowner: \"{user.username}\"\n---\n\n## Steps to reproduce\n\n1. \n\n## Expected behavior\n\n\n\n## Actual behavior\n\n\n\n## Possible fix\n\n\n\n## Environment\n\n- OS: \n- Version: ",
            "feature": f"---\ntitle: \"{title}\"\ntype: feature\ncreated: \"{today}\"\nupdated: \"{today}\"\ntags: [feature]\nowner: \"{user.username}\"\n---\n\n## User Story\n\nAls ... möchte ich ... damit ...\n\n## Acceptance Criteria\n\n- [ ] \n\n## Implementation\n\n### Approach\n\n\n\n### Tasks\n\n- [ ] \n\n## Testing\n\n\n",
        }
        if template_type == "adr":
            template_type = "decision"
        content = templates.get(template_type, "")
        write_file(path, content)
        index_file(path)
        return json.dumps({"path": path, "status": "created", "template_type": template_type}, ensure_ascii=False)

    if name == "validate_links":
        _need_read()
        scope = args.get("path", ".")
        root = user_root()
        broken = []
        valid = 0
        checked = 0

        md_files = _markdown_paths(scope)

        def _check_file(md_file):
            rel = _rel_path(md_file)
            try:
                text = md_file.read_text(encoding="utf-8")
            except Exception:
                return [], 0
            file_broken = []
            file_valid = 0
            for lineno, link in _local_markdown_links(text):
                if link.startswith(("http://", "https://", "mailto:", "#")):
                    file_valid += 1
                    continue
                target = _resolve_local_link(rel, link)
                exists = target is not None and safe_path(target).exists() if target else False
                if exists:
                    file_valid += 1
                else:
                    file_broken.append({"source": rel, "line": lineno, "link": link, "target_exists": False})
            return file_broken, file_valid

        # Process in parallel batches
        BATCH_SIZE = 8
        for i in range(0, len(md_files), BATCH_SIZE):
            batch = md_files[i:i + BATCH_SIZE]
            results = await asyncio.gather(*(asyncio.to_thread(_check_file, fp) for fp in batch))
            for file_broken, file_valid in results:
                broken.extend(file_broken)
                valid += file_valid
                checked += 1
        return json.dumps({"checked_files": checked, "broken_links": broken, "valid_count": valid, "broken_count": len(broken)}, ensure_ascii=False, indent=2)

    if name == "link_graph":
        _need_read()
        scope = args.get("path", ".")
        all_files_list = list_all_files(scope)
        nodes = [{"id": f["path"], "title": f["title"], "tags": f.get("tags", [])} for f in all_files_list]
        path_set = {f["path"] for f in all_files_list}
        edges = []
        incoming: dict[str, int] = {}
        outgoing: dict[str, int] = {}

        def _extract_links(f):
            try:
                md_file = (user_root() / f["path"]).resolve()
                text = md_file.read_text(encoding="utf-8")
            except Exception:
                return []
            return [(f["path"], link) for _, link in _local_markdown_links(text)]

        # Read files in parallel batches
        BATCH_SIZE = 8
        all_links = []
        for i in range(0, len(all_files_list), BATCH_SIZE):
            batch = all_files_list[i:i + BATCH_SIZE]
            results = await asyncio.gather(*(asyncio.to_thread(_extract_links, f) for f in batch))
            for file_links in results:
                all_links.extend(file_links)

        for source, link in all_links:
            target = _resolve_local_link(source, link)
            if target and target in path_set:
                edges.append({"source": source, "target": target, "link_text": link})
                outgoing[source] = outgoing.get(source, 0) + 1
                incoming[target] = incoming.get(target, 0) + 1
        linked = set(incoming.keys()) | set(outgoing.keys())
        orphaned = [f["path"] for f in all_files_list if f["path"] not in linked and f["path"] not in ("index.md", "AGENTS.md")]
        most_linked = [{"path": p, "count": c} for p, c in sorted(incoming.items(), key=lambda x: -x[1])[:10]]
        most_linking = [{"path": p, "count": c} for p, c in sorted(outgoing.items(), key=lambda x: -x[1])[:10]]
        return json.dumps({"nodes": nodes, "edges": edges, "orphaned": orphaned, "most_linked": most_linked, "most_linking": most_linking}, ensure_ascii=False, indent=2)

    if name == "rename":
        _need_write()
        old_path = args["old_path"]
        new_path = args["new_path"]
        fc = move_file(old_path, new_path)
        _deindex_markdown(old_path)
        _index_markdown(new_path)
        links_updated = 0
        for md_file in _markdown_paths("."):
            rel = _rel_path(md_file)
            try:
                text = md_file.read_text(encoding="utf-8")
            except Exception:
                continue
            new_text = text
            for _, link in _local_markdown_links(text):
                target = _resolve_local_link(rel, link)
                if target == old_path:
                    new_link = link.replace(old_path.rsplit("/", 1)[-1].replace(".md", ""), new_path.rsplit("/", 1)[-1].replace(".md", ""))
                    new_text = new_text.replace(link, new_link, 1)
            if new_text != text:
                md_file.write_text(new_text, encoding="utf-8")
                _index_markdown(rel)
                links_updated += 1
        return json.dumps({"old_path": old_path, "new_path": new_path, "links_updated": links_updated, "status": "renamed"}, ensure_ascii=False)

    if name == "batch_tag":
        _need_write()
        files = args["files"]
        tags = args["tags"]
        mode = args.get("mode", "merge")
        updated = []
        for path in files:
            meta = _read_frontmatter_only(path)
            existing_tags = list(meta.get("tags", []))
            if mode == "replace":
                new_tags = tags
            else:
                new_tags = list(dict.fromkeys(existing_tags + tags))
            update_frontmatter(path, {"tags": new_tags})
            _index_markdown(path)
            updated.append({"path": path, "tags": new_tags})
        return json.dumps({"updated": updated, "count": len(updated)}, ensure_ascii=False, indent=2)

    if name == "export":
        _need_read()
        scope = args.get("path", ".")
        fmt = args.get("format", "html")
        all_files_list = list_all_files(scope)
        if fmt == "markdown":
            parts = []
            for f in all_files_list:
                try:
                    fc = read_file(f["path"])
                    parts.append(f"# {f['title']}\n\n{fc.content}\n\n---\n")
                except Exception:
                    continue
            content = "\n".join(parts)
            return json.dumps({"content": content, "file_count": len(parts), "total_size": len(content)}, ensure_ascii=False, indent=2)
        else:
            parts = []
            nav_items = []
            for f in all_files_list:
                nav_items.append(f'<li><a href="#{_slug(f["path"])}">{html.escape(f["title"])}</a></li>')
            for f in all_files_list:
                try:
                    fc = read_file(f["path"])
                    rendered = nh3.clean(md_lib.markdown(fc.content, extensions=["fenced_code", "tables", "nl2br"]), tags=_NH3_TAGS, attributes=_NH3_ATTRS, url_schemes={"http", "https", "mailto"})
                    parts.append(f'<section id="{_slug(f["path"])}"><h2>{html.escape(f["title"])}</h2>{rendered}</section>')
                except Exception:
                    continue
            content = f"""<!DOCTYPE html>
<html lang="de"><head><meta charset="utf-8"><title>kiwiki Export</title>
<style>body{{font-family:sans-serif;max-width:800px;margin:0 auto;padding:2rem}}
nav{{margin-bottom:2rem}}section{{margin-bottom:3rem;border-bottom:1px solid #eee;padding-bottom:1rem}}</style>
</head><body><h1>kiwiki Export</h1><nav><ul>{"".join(nav_items)}</ul></nav>{"".join(parts)}</body></html>"""
            return json.dumps({"content": content, "file_count": len(parts), "total_size": len(content)}, ensure_ascii=False, indent=2)

    if name == "duplicate_check":
        _need_read()
        scope = args.get("path", ".")
        threshold = float(args.get("threshold", 0.7))
        all_files_list = list_all_files(scope)
        pairs = []
        for i, a in enumerate(all_files_list):
            for b in all_files_list[i + 1:]:
                a_tags = set(str(t) for t in a.get("tags", []))
                b_tags = set(str(t) for t in b.get("tags", []))
                tag_sim = len(a_tags & b_tags) / max(len(a_tags | b_tags), 1)
                title_a = a["title"].lower().split()
                title_b = b["title"].lower().split()
                title_sim = len(set(title_a) & set(title_b)) / max(len(set(title_a) | set(title_b)), 1)
                combined = (tag_sim * 0.4 + title_sim * 0.6)
                if combined >= threshold:
                    reason = []
                    if tag_sim > 0.5:
                        reason.append(f"shared tags: {', '.join(a_tags & b_tags)}")
                    if title_sim > 0.5:
                        reason.append("similar titles")
                    pairs.append({"file_a": a["path"], "file_b": b["path"], "similarity": round(combined, 2), "reason": "; ".join(reason) or "high overlap"})
        pairs.sort(key=lambda x: -x["similarity"])
        return json.dumps({"pairs": pairs, "total_checked": len(all_files_list)}, ensure_ascii=False, indent=2)

    if name == "ai_summarize":
        _need_read()
        path = args["path"]
        max_length = int(args.get("max_length", 500))
        fc = read_file(path)
        content = fc.content
        lines = content.split("\n")
        headings = [line.lstrip("#").strip() for line in lines if line.startswith("#")]
        words = content.split()
        word_count = len(words)
        sentences = [s.strip() for s in re.split(r'[.!?]+', content) if s.strip() and len(s.strip()) > 10]
        summary_parts = []
        if headings:
            summary_parts.append("Headings: " + ", ".join(headings[:5]))
        if sentences:
            summary_parts.append(sentences[0])
            if len(sentences) > 1:
                summary_parts.append(sentences[-1])
        key_facts = []
        for sentence in sentences[:10]:
            if any(kw in sentence.lower() for kw in ["todo", "fixme", "wichtig", "achtung", "note", "warnung"]):
                key_facts.append(sentence[:200])
        if not key_facts:
            key_facts = [s[:200] for s in sentences[:3]]
        summary = " ".join(summary_parts)[:max_length * 5]
        return json.dumps({"path": path, "summary": summary, "word_count": word_count, "headings": headings, "key_facts": key_facts}, ensure_ascii=False, indent=2)

    # ── E3: Search History ────────────────────────────────────────────────────
    if name == "search_history":
        _need_read()
        from .search import get_search_history
        limit = max(1, min(int(args.get("limit", 10)), 100))
        history = get_search_history(limit)
        return json.dumps(history, ensure_ascii=False, indent=2)

    # ── E5: Dead Link Check ──────────────────────────────────────────────────
    if name == "dead_link_check":
        _need_read()
        scope = args.get("path", ".")
        broken = []
        valid = 0
        checked = 0
        md_files = _markdown_paths(scope)
        for md_file in md_files:
            rel = _rel_path(md_file)
            checked += 1
            try:
                text = md_file.read_text(encoding="utf-8")
            except Exception:
                continue
            for lineno, link in _local_markdown_links(text):
                if link.startswith(("http://", "https://", "mailto:", "#")):
                    valid += 1
                    continue
                target = _resolve_local_link(rel, link)
                exists = target is not None and safe_path(target).exists() if target else False
                if exists:
                    valid += 1
                else:
                    broken.append({"source": rel, "line": lineno, "link": link, "target_exists": False})
        return json.dumps({
            "checked_files": checked,
            "broken_links": broken,
            "valid_count": valid,
            "broken_count": len(broken),
        }, ensure_ascii=False, indent=2)

    # ── E2: Grep Status ──────────────────────────────────────────────────────
    if name == "grep_status":
        _need_read()
        job_id = args.get("job_id", "")
        job = _grep_jobs.get(job_id)
        if job is None:
            return json.dumps({"status": "not_found", "job_id": job_id, "result": None}, ensure_ascii=False)
        if job["status"] == "running":
            return json.dumps({"status": "running", "job_id": job_id, "result": None}, ensure_ascii=False)
        return json.dumps({"status": "completed", "job_id": job_id, "result": job["result"]}, ensure_ascii=False, indent=2)

    raise ValueError(f"Unknown tool: {name!r}")
