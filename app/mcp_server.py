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
import time
import uuid
from datetime import datetime
from typing import AsyncGenerator
from urllib.parse import urlencode, urlparse

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from .auth import ROLE_HIERARCHY, parse_users
from .models import User
from .search import deindex_file, get_db, index_file, init_db, reindex_all, search as fts_search
from .storage import (
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
    write_file,
)
from .tenancy import ensure_user_workspace, is_valid_username, set_user_ns, user_root

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
    "related_files",
    "tag_index",
    "search_status",
    "whoami",
}

_DESTRUCTIVE_TOOLS = {"delete_file", "move_file", "move_folder", "sort", "replace_many"}


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
        try:
            agents = read_file("AGENTS.md").content
            index  = read_file("index.md").content
            schema_hint = f"\n\n--- AGENTS.md ---\n{agents}\n\n--- index.md ---\n{index}"
        except Exception:
            schema_hint = ""
        return _rpc_ok(req_id, {
            "protocolVersion": negotiated,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "kiwiki", "version": "0.1.0"},
            "instructions": (
                "This is kiwiki — a Markdown-based personal wiki. "
                "Follow these rules strictly:\n"
                "1. ALWAYS call read_index at the start of every session to get the current schema and navigation.\n"
                "2. ALWAYS call search before creating new content to avoid duplicates.\n"
                "3. Place notes in topic subfolders: notes/python/, notes/ml/, projects/kiwiki/ etc.\n"
                "4. Create a subfolder once 3+ files share a topic.\n"
                "5. Prefer edit/append_file over write_file for existing files.\n"
                "6. Refresh index.md (via edit or build_index) after creating new folders.\n"
                "7. Always set complete frontmatter: title, type, created, updated, tags, owner."
            ) + schema_hint,
        })

    if method in ("notifications/initialized", "initialized"):
        return None if _is_notification(body) else _rpc_ok(req_id, {})

    if method == "tools/list":
        return _rpc_ok(req_id, {"tools": TOOLS})

    if method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {}) or {}
        try:
            text = await _dispatch(tool_name, arguments, user)
            return _rpc_ok(req_id, {
                "content": [{"type": "text", "text": text}],
                "structuredContent": json.loads(text),
            })
        except PermissionError as exc:
            return _rpc_err(req_id, -32001, str(exc))
        except (FileNotFoundError, ValueError) as exc:
            return _rpc_err(req_id, -32000, str(exc))
        except Exception as exc:
            return _rpc_err(req_id, -32000, f"Internal error: {exc}")

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
        fc = read_file(rel)
        title = fc.frontmatter.get("title", path.stem)
        updated = fc.frontmatter.get("updated", "")
        tags = fc.frontmatter.get("tags", [])
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
    fc = read_file(path)
    tags = fc.frontmatter.get("tags", [])
    if not isinstance(tags, list):
        tags = []
    return fc.frontmatter.get("title", os.path.splitext(os.path.basename(path))[0]), tags, fc.frontmatter


def _index_markdown(path: str) -> None:
    init_db()
    index_file(path)


def _deindex_markdown(path: str) -> None:
    init_db()
    deindex_file(path)


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
        if not path.endswith(".md"):
            raise ValueError("Only .md files may be written")
        fc = write_file(path, args["content"])
        _index_markdown(path)
        return json.dumps({"path": fc.path, "status": "written"}, ensure_ascii=False)

    if name == "append_file":
        _need_write()
        path = args["path"]
        fc = append_file(path, args["content"])
        _index_markdown(path)
        return json.dumps({"path": fc.path, "status": "appended"}, ensure_ascii=False)

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
            for filepath in files:
                rel = str(filepath.relative_to(user_root()))
                # Per-File-Timeout schuetzt vor einem hengenden File.
                try:
                    hits = await asyncio.wait_for(
                        asyncio.to_thread(_scan_one, filepath, rel, compiled, context_n),
                        timeout=PER_FILE_TIMEOUT_S,
                    )
                except asyncio.TimeoutError:
                    logger.warning("grep: file %s timed out after %.1fs, skipped", rel, PER_FILE_TIMEOUT_S)
                    continue
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
                fc = read_file(rel)
                title = fc.frontmatter.get("title", "")
                if title:
                    titles.setdefault(str(title).lower(), []).append(rel)
                for field in required:
                    if field not in fc.frontmatter or fc.frontmatter.get(field) in ("", None):
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
        conn = get_db()
        try:
            indexed_count = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            database = conn.execute("PRAGMA database_list").fetchone()[2]
        finally:
            conn.close()
        return json.dumps({"markdown_files": markdown_count, "indexed_files": indexed_count, "database": database}, ensure_ascii=False, indent=2)

    if name == "whoami":
        _need_read()
        return json.dumps({"username": user.username, "role": user.role, "workspace": str(user_root())}, ensure_ascii=False, indent=2)

    raise ValueError(f"Unknown tool: {name!r}")
