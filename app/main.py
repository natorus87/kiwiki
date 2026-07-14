import html
import logging
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import markdown as md_lib
import nh3
import yaml
from fastapi import FastAPI, Depends, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest

from . import session_store, user_store
from .auth import (
    ROLE_HIERARCHY,
    _lookup_api_key,
    current_role_for_username,
    get_current_user,
    parse_users,
    require_role,
)
from .mcp_server import router as mcp_router
from .constants import APP_VERSION, NH3_ATTRS, NH3_TAGS
from .models import (
    AppendFileRequest,
    CreateFolderRequest,
    CreateNoteRequest,
    CreateUserRequest,
    MoveRequest,
    SearchRequest,
    UpdateFrontmatterRequest,
    User,
    WriteFileRequest,
    MAX_CONTENT_LENGTH,
)
from .rate_limiter import RateLimitMiddleware
from .search import deindex_file, index_file, init_db, reindex_all, reindex_changed, search as search_files
from .storage import (
    _read_frontmatter_only,
    append_file,
    create_folder,
    create_note,
    delete_file,
    delete_folder,
    list_all_files,
    list_files,
    move_file,
    move_folder,
    read_file,
    update_frontmatter,
    validate_content_folder_path,
    validate_markdown_content_path,
    write_file,
)
from .tenancy import (
    CURRENT_USER_NS,
    base_data_dir,
    ensure_user_workspace,
    is_valid_username,
    migrate_legacy_data_dir,
    set_user_ns,
    user_root,
)

logging.basicConfig(
    level=os.getenv("KIWIKI_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

def _render_markdown_safe(content: str) -> str:
    rendered = md_lib.markdown(
        content,
        extensions=["fenced_code", "tables", "nl2br"],
    )
    return nh3.clean(
        rendered,
        tags=NH3_TAGS,
        attributes=NH3_ATTRS,
        url_schemes={"http", "https", "mailto"},
        link_rel=None,
    )


def _reindex_moved_folder(src: str, dst: str, old_paths: list[str]) -> None:
    for old_path in old_paths:
        deindex_file(old_path)
        suffix = old_path[len(src):].lstrip("/")
        new_path = f"{dst.rstrip('/')}/{suffix}" if suffix else dst
        index_file(new_path)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    from .mcp_server import validate_oauth_config

    validate_oauth_config()
    users_map = parse_users()  # Validierungs-Log gleich beim Boot ausgeben
    migrate_legacy_data_dir()
    # Für jeden konfigurierten User: Workspace anlegen, DB initialisieren, Reindex
    for username, _role in users_map.values():
        if not is_valid_username(username):
            continue
        set_user_ns(username)
        ensure_user_workspace(username)
        init_db()
        # A6: Lazy reindex — nur geänderte Dateien neu indexieren
        count = reindex_changed()
        if count > 0:
            logging.getLogger("kiwiki.startup").info(
                "Lazy reindexed %d file(s) for user %s", count, username
            )
    yield
    from .search import close_pool
    close_pool()


app = FastAPI(title="kiwiki", version=APP_VERSION, lifespan=_lifespan)
app.include_router(mcp_router)

# ---------------------------------------------------------------------------
# CORS — optional: KIWIKI_CORS_ORIGINS = "https://wiki.example,https://api.example"
# Default leer: Cross-Origin-Zugriff bleibt deaktiviert, bis Origins explizit gesetzt sind.
# ---------------------------------------------------------------------------
_cors_origins_raw = os.getenv("KIWIKI_CORS_ORIGINS", "")
if not _cors_origins_raw:
    _cors_origins: list[str] = []
else:
    _cors_origins = [o.strip() for o in _cors_origins_raw.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: StarletteRequest, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "font-src 'self'; "
            "object-src 'none'; base-uri 'self'; form-action 'self'"
        )
        if request.url.scheme == "https":
            response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        return response


app.add_middleware(SecurityHeadersMiddleware)


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Korrelations-ID und kompakte Latenzlogs fuer jeden HTTP-Request."""

    async def dispatch(self, request: StarletteRequest, call_next):
        supplied = request.headers.get("X-Request-ID", "")
        request_id = supplied if re.fullmatch(r"[A-Za-z0-9._-]{1,64}", supplied) else uuid.uuid4().hex
        started = time.perf_counter()
        response = await call_next(request)
        duration_ms = (time.perf_counter() - started) * 1000
        response.headers["X-Request-ID"] = request_id
        logging.getLogger("kiwiki.request").info(
            "%s %s status=%d duration_ms=%.2f request_id=%s",
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
            request_id,
        )
        return response


app.add_middleware(RequestContextMiddleware)


class RequestSizeLimitMiddleware(BaseHTTPMiddleware):
    """Uebergrosse HTTP-Bodies vor JSON-/Form-Parsing ablehnen."""

    max_body_bytes = MAX_CONTENT_LENGTH + 64 * 1024

    async def dispatch(self, request: StarletteRequest, call_next):
        raw_length = request.headers.get("content-length")
        if raw_length:
            try:
                if int(raw_length) > self.max_body_bytes:
                    return JSONResponse(
                        {"detail": "Request body too large"},
                        status_code=413,
                    )
            except ValueError:
                return JSONResponse({"detail": "Invalid Content-Length"}, status_code=400)
        elif request.method in {"POST", "PUT", "PATCH"}:
            chunks: list[bytes] = []
            received = 0
            async for chunk in request.stream():
                received += len(chunk)
                if received > self.max_body_bytes:
                    return JSONResponse(
                        {"detail": "Request body too large"},
                        status_code=413,
                    )
                chunks.append(chunk)
            # Starlette nutzt den Cache bei der nachfolgenden Modell-/Form-Auswertung.
            request._body = b"".join(chunks)
        return await call_next(request)


app.add_middleware(RequestSizeLimitMiddleware)

# ---------------------------------------------------------------------------
# Rate Limiting — Login: 5/min, Write: 30/min, Read: 60/min pro IP
# KIWIKI_RATE_LIMIT_ENABLED="false" deaktiviert komplett
# ---------------------------------------------------------------------------
app.add_middleware(RateLimitMiddleware)

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------------------------------------------------------------------------
# Auth middleware — protects all web UI routes with session cookie
# ---------------------------------------------------------------------------

# Paths that are always accessible without a session
_OPEN_PREFIXES = (
    "/login",
    "/logout",
    "/health",
    "/livez",
    "/readyz",
    "/static/",
    "/api/",
    "/mcp",
    "/.well-known/",
    "/oauth/",
    "/docs",
    "/openapi.json",
    "/redoc",
)


class WebAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if any(path == p or path.startswith(p) for p in _OPEN_PREFIXES):
            return await call_next(request)

        # Requests mit Bearer-Token kommen von API-/MCP-Clients (z. B. ChatGPT,
        # die JSON-RPC direkt an "/" POSTen). Diese haben ihren eigenen
        # Auth-Mechanismus im jeweiligen Endpoint — Cookie-Redirect würde sie
        # nur fälschlich zur Login-Seite schicken.
        if request.headers.get("Authorization", "").startswith("Bearer "):
            return await call_next(request)

        token = request.cookies.get("kiwiki_session", "")
        record = session_store.lookup_session(token) if token else None
        if record is None:
            if request.headers.get("HX-Request"):
                return HTMLResponse(
                    '<div class="error">Sitzung abgelaufen. <a href="/login">Neu anmelden</a></div>',
                    status_code=401,
                )
            return RedirectResponse(url="/login", status_code=302)

        if not is_valid_username(record.username):
            return RedirectResponse(url="/logout", status_code=302)
        current_role = current_role_for_username(record.username)
        if current_role is None or current_role != record.role:
            session_store.revoke_session(token)
            return RedirectResponse(url="/login", status_code=302)
        set_user_ns(record.username)
        return await call_next(request)


app.add_middleware(WebAuthMiddleware)


def _session_user(request: Request) -> User | None:
    """Extract the logged-in user from the session cookie and set the tenant
    namespace as a safety net (the WebAuthMiddleware does this too, but UI
    helpers may be invoked from contexts where the ContextVar isn't set)."""
    token = request.cookies.get("kiwiki_session", "")
    record = session_store.lookup_session(token) if token else None
    if record is None:
        return None
    if not is_valid_username(record.username):
        return None
    current_role = current_role_for_username(record.username)
    if current_role is None or current_role != record.role:
        session_store.revoke_session(token)
        return None
    set_user_ns(record.username)
    return User(username=record.username, role=current_role)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/livez")
async def livez() -> dict:
    """Reiner Prozess-Liveness-Check ohne externe Abhaengigkeiten."""
    return {"status": "alive"}


@app.get("/readyz", response_model=None)
def readyz() -> dict | JSONResponse:
    """Prueft User-Konfiguration, Datenverzeichnis und per-User-SQLite."""
    probe_path: Path | None = None
    try:
        users = parse_users()
        if not users:
            raise RuntimeError("No valid users configured")

        data_dir = base_data_dir()
        data_dir.mkdir(parents=True, exist_ok=True)
        import tempfile

        fd, probe_name = tempfile.mkstemp(prefix=".kiwiki-ready-", dir=str(data_dir))
        os.close(fd)
        probe_path = Path(probe_name)

        from .search import get_db

        for username, _role in users.values():
            namespace_token = CURRENT_USER_NS.set(username)
            try:
                ensure_user_workspace(username)
                init_db()
                with get_db() as conn:
                    conn.execute("SELECT 1").fetchone()
            finally:
                CURRENT_USER_NS.reset(namespace_token)
        return {"status": "ready", "version": app.version, "users": len(users)}
    except Exception:
        logging.getLogger("kiwiki.readiness").exception("Readiness check failed")
        return JSONResponse({"status": "not ready"}, status_code=503)
    finally:
        if probe_path is not None:
            try:
                probe_path.unlink(missing_ok=True)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Login / Logout
# ---------------------------------------------------------------------------

@app.get("/login", response_class=HTMLResponse, response_model=None)
async def login_page(request: Request) -> HTMLResponse | RedirectResponse:
    # Already logged in → go home
    if _session_user(request):
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse(request=request, name="login.html", context={"error": None})


@app.post("/login", response_class=HTMLResponse, response_model=None)
async def login_submit(request: Request, api_key: str = Form(...)) -> HTMLResponse | RedirectResponse:
    if len(api_key) > 256:
        return templates.TemplateResponse(
            request=request, name="login.html",
            context={"error": "API-Key zu lang"},
            status_code=400,
        )
    users_map = parse_users()
    match = _lookup_api_key(users_map, api_key)
    if match is None:
        return templates.TemplateResponse(
            request=request, name="login.html",
            context={"error": "Ungültiger API-Key"},
            status_code=401,
        )
    username, role = match
    if not is_valid_username(username):
        return templates.TemplateResponse(
            request=request, name="login.html",
            context={"error": "Benutzername enthält ungültige Zeichen für Namespace"},
            status_code=400,
        )
    set_user_ns(username)
    ensure_user_workspace(username)
    init_db()
    # Server-seitige Session: das Cookie enthaelt NUR ein zufaelliges Token,
    # NICHT den API-Key. Token-Compromise fuehrt nicht zur API-Key-Leak.
    record = session_store.create_session(username, role, api_key)
    response = RedirectResponse(url="/", status_code=303)
    # Trust-proxy: secure cookie nur hinter HTTPS/Proxy
    _trust_proxy = os.getenv("KIWIKI_TRUST_PROXY", "false").lower() == "true"
    response.set_cookie(
        "kiwiki_session",
        record.token,
        httponly=True,
        secure=_trust_proxy,
        # strict statt lax: kiwiki hat keinen Cross-Site-Einstiegspunkt (kein
        # Login-Link aus E-Mails etc.), daher schliesst strict CSRF ueber die
        # zustandsaendernden /ui/*-POST-Endpunkte, ohne einen legitimen Flow
        # zu brechen — alle internen Requests (HTMX, Formulare) sind same-site.
        samesite="strict",
        max_age=session_store.session_ttl_seconds(),
    )
    return response


@app.get("/logout")
async def logout(request: Request) -> RedirectResponse:
    # Aktuelle Session widerrufen, falls vorhanden
    token = request.cookies.get("kiwiki_session", "")
    if token:
        session_store.revoke_session(token)
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie("kiwiki_session")
    response.delete_cookie("kiwiki_key")
    return response


# ---------------------------------------------------------------------------
# Web UI pages
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    user = _session_user(request)
    return templates.TemplateResponse(request=request, name="index.html", context={"user": user})


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request) -> HTMLResponse:
    user = _session_user(request)
    if not user or ROLE_HIERARCHY.get(user.role, -1) < ROLE_HIERARCHY["admin"]:
        raise HTTPException(status_code=403, detail="Admin role required")
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={"user": user, "users": user_store.list_users()},
    )


@app.get("/editor", response_class=HTMLResponse)
async def editor(request: Request, path: str = "") -> HTMLResponse:
    user = _session_user(request)
    initial_content = ""
    load_error = ""
    if path:
        try:
            fc = read_file(path)
            if fc.frontmatter:
                initial_content += "---\n"
                initial_content += yaml.safe_dump(
                    dict(fc.frontmatter),
                    allow_unicode=True,
                    sort_keys=False,
                    default_flow_style=False,
                )
                initial_content += "---\n\n"
            initial_content += fc.content
        except FileNotFoundError:
            initial_content = ""
        except Exception:
            logging.exception("Failed to load file %r into editor", path)
            load_error = "Datei konnte nicht geladen werden."
    return templates.TemplateResponse(
        request=request, name="editor.html",
        context={
            "file_path": path,
            "initial_content": initial_content,
            "load_error": load_error,
            "user": user,
        },
    )


# ---------------------------------------------------------------------------
# UI fragment endpoints for HTMX (return HTML)
# ---------------------------------------------------------------------------

_SVG_FOLDER = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24"'
    ' fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"'
    ' stroke-linejoin="round" class="icon">'
    '<path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.93a2 2 0 0 1-1.66-.9l-.82-1.2A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2Z"/>'
    '</svg>'
)
_SVG_FILE = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24"'
    ' fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"'
    ' stroke-linejoin="round" class="icon">'
    '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/>'
    '<path d="M14 2v4a2 2 0 0 0 2 2h4"/>'
    '</svg>'
)
# Gemeinsame Attribute für Button-Icons — currentColor wird via CSS color-Eigenschaft gesteuert
_SVG_BTN_ATTRS = 'xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="btn-icon"'

_SVG_EDIT = (
    f'<svg {_SVG_BTN_ATTRS}>'
    '<path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/>'
    '<path d="m15 5 4 4"/>'
    '</svg>'
)
_SVG_TRASH = (
    f'<svg {_SVG_BTN_ATTRS}>'
    '<path d="M3 6h18"/>'
    '<path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/>'
    '<path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/>'
    '<line x1="10" y1="11" x2="10" y2="17"/>'
    '<line x1="14" y1="11" x2="14" y2="17"/>'
    '</svg>'
)
_SVG_FILE_PLUS = (
    f'<svg {_SVG_BTN_ATTRS}>'
    '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/>'
    '<path d="M14 2v4a2 2 0 0 0 2 2h4"/>'
    '<path d="M9 15h6"/>'
    '<path d="M12 18v-6"/>'
    '</svg>'
)
_SVG_FOLDER_PLUS = (
    f'<svg {_SVG_BTN_ATTRS}>'
    '<path d="M12 10v6"/>'
    '<path d="M9 13h6"/>'
    '<path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.93a2 2 0 0 1-1.66-.9l-.82-1.2A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2Z"/>'
    '</svg>'
)
_SVG_MOVE = (
    f'<svg {_SVG_BTN_ATTRS}>'
    '<path d="M5 12h14"/>'
    '<path d="m12 5 7 7-7 7"/>'
    '</svg>'
)
_SVG_CHEVRON = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" viewBox="0 0 24 24"'
    ' fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"'
    ' class="chevron">'
    '<polyline points="9 18 15 12 9 6"/>'
    '</svg>'
)


@app.get("/ui/files", response_class=HTMLResponse)
async def ui_files(request: Request, path: str = ".") -> HTMLResponse:
    try:
        items = list_files(path)
        if path == ".":
            items = [i for i in items if i.name != ".kiwiki"]
        view_items = [
            {
                "name": item.name,
                "path": item.path,
                "is_dir": item.is_dir,
                "has_children": item.has_children,
                "tree_id": "tree-" + re.sub(r"[^a-zA-Z0-9_-]", "-", item.path),
            }
            for item in items
        ]
        return templates.TemplateResponse(
            request=request,
            name="partials/file_tree.html",
            context={
                "items": view_items,
                "user": _session_user(request),
                "svg_folder": _SVG_FOLDER,
                "svg_file": _SVG_FILE,
                "svg_edit": _SVG_EDIT,
                "svg_trash": _SVG_TRASH,
                "svg_file_plus": _SVG_FILE_PLUS,
                "svg_folder_plus": _SVG_FOLDER_PLUS,
                "svg_move": _SVG_MOVE,
            },
        )
    except (ValueError, FileNotFoundError) as exc:
        return HTMLResponse(f'<div class="error">{html.escape(str(exc))}</div>')
    except Exception:
        logging.exception("Failed to render file tree for path %r", path)
        return HTMLResponse('<div class="error">Ordner konnte nicht geladen werden.</div>')


@app.get("/ui/file", response_class=HTMLResponse)
async def ui_file(request: Request, path: str) -> HTMLResponse:
    user = _session_user(request)
    can_delete = user and ROLE_HIERARCHY.get(user.role, -1) >= ROLE_HIERARCHY["admin"]
    try:
        fc = read_file(path)
        rendered = _render_markdown_safe(fc.content)
        fm = fc.frontmatter
        title = fm.get("title", Path(path).stem.replace("-", " ").replace("_", " "))
        svg_edit = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="13" height="13" viewBox="0 0 24 24"'
            ' fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">'
            '<path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>'
            '<path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>'
        )
        svg_trash = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="13" height="13" viewBox="0 0 24 24"'
            ' fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">'
            '<polyline points="3 6 5 6 21 6"/>'
            '<path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>'
        )
        return templates.TemplateResponse(
            request=request,
            name="partials/file_view.html",
            context={
                "title": title,
                "path": path,
                "updated": fm.get("updated"),
                "owner": fm.get("owner"),
                "tags": fm.get("tags") or [],
                "rendered": rendered,
                "user": user,
                "can_delete": can_delete,
                "svg_edit": svg_edit,
                "svg_trash": svg_trash,
            },
        )
    except FileNotFoundError:
        return HTMLResponse('<div class="content-inner"><div class="empty-state error-state"><h2>Datei nicht gefunden</h2><p>Die ausgewählte Datei existiert nicht mehr oder wurde verschoben.</p></div></div>')
    except ValueError as exc:
        return HTMLResponse(f'<div class="content-inner"><div class="error">{html.escape(str(exc))}</div></div>')
    except Exception:
        logging.exception("Failed to render file view for path %r", path)
        return HTMLResponse('<div class="content-inner"><div class="error">Datei konnte nicht geladen werden.</div></div>')


@app.post("/ui/search", response_class=HTMLResponse)
async def ui_search(request: Request) -> HTMLResponse:
    form = await request.form()
    query = form.get("query", "").strip()
    if not query:
        return HTMLResponse("")
    try:
        results = search_files(query)
        return templates.TemplateResponse(
            request=request,
            name="partials/search_results.html",
            context={"results": results, "query": query},
        )
    except Exception:
        logging.exception("Search failed for query %r", query)
        return HTMLResponse('<div class="error">Suche fehlgeschlagen.</div>')


@app.get("/ui/recent", response_class=HTMLResponse)
async def ui_recent(request: Request) -> HTMLResponse:
    user = _session_user(request)
    if not user:
        return HTMLResponse("")
    try:
        files = list_files(".")
        files = [f for f in files if not f.is_dir and f.name not in ("index.md", "AGENTS.md")]
        files.sort(key=lambda f: f.updated_at or "", reverse=True)
        recent = files[:5]
        return templates.TemplateResponse(
            request=request,
            name="partials/recent_files.html",
            context={"files": recent},
        )
    except Exception:
        return HTMLResponse("")


@app.get("/ui/recent-edited", response_class=HTMLResponse)
async def ui_recent_edited(request: Request) -> HTMLResponse:
    """Recently edited files (sorted by frontmatter 'updated'), recursive."""
    user = _session_user(request)
    if not user:
        return HTMLResponse("")
    try:
        files = list_all_files(".")
        files = [f for f in files if f["path"] not in ("index.md", "AGENTS.md")]
        files.sort(key=lambda f: f.get("updated", ""), reverse=True)
        recent = files[:8]
        return templates.TemplateResponse(
            request=request,
            name="partials/recent_edited.html",
            context={"files": recent},
        )
    except Exception:
        logging.exception("Failed to render recent-edited panel")
        return HTMLResponse("")


@app.get("/ui/recent-created", response_class=HTMLResponse)
async def ui_recent_created(request: Request) -> HTMLResponse:
    """Recently created files (sorted by frontmatter 'created'), recursive."""
    user = _session_user(request)
    if not user:
        return HTMLResponse("")
    try:
        files = list_all_files(".")
        files = [f for f in files if f["path"] not in ("index.md", "AGENTS.md")]
        files = [f for f in files if f.get("created")]
        files.sort(key=lambda f: f.get("created", ""), reverse=True)
        recent = files[:8]
        return templates.TemplateResponse(
            request=request,
            name="partials/recent_created.html",
            context={"files": recent},
        )
    except Exception:
        logging.exception("Failed to render recent-created panel")
        return HTMLResponse("")


@app.get("/ui/tags", response_class=HTMLResponse)
async def ui_tags(request: Request) -> HTMLResponse:
    """E4: Global tag overview page."""
    user = _session_user(request)
    try:
        all_files = list_files(".")
        tags: dict[str, list[str]] = {}
        for f in all_files:
            if f.is_dir:
                continue
            try:
                meta = _read_frontmatter_only(f.path)
                for tag in meta.get("tags", []):
                    tags.setdefault(str(tag), []).append(f.path)
            except Exception:
                continue
        tag_items = [
            {"tag": tag, "count": len(files), "files": sorted(files)}
            for tag, files in sorted(tags.items(), key=lambda x: (-len(x[1]), x[0]))
        ]
        return templates.TemplateResponse(
            request=request,
            name="partials/tags_overview.html",
            context={"tags": tag_items, "user": user},
        )
    except Exception:
        return HTMLResponse('<div class="error">Fehler beim Laden der Tags</div>')


@app.get("/ui/search-history", response_class=HTMLResponse)
async def ui_search_history(request: Request) -> HTMLResponse:
    """E3: Search history endpoint."""
    user = _session_user(request)
    try:
        from .search import get_search_history
        history = get_search_history(20)
        return templates.TemplateResponse(
            request=request,
            name="partials/search_history.html",
            context={"history": history, "user": user},
        )
    except Exception:
        return HTMLResponse("")


@app.get("/ui/history", response_class=HTMLResponse)
async def ui_file_history(request: Request, path: str = "") -> HTMLResponse:
    """B7: Git file history page."""
    user = _session_user(request)
    if not path:
        return HTMLResponse('<div class="error">Kein Dateipfad angegeben</div>')
    try:
        import subprocess
        root = user_root()
        result = subprocess.run(
            ["git", "log", "-20", "--pretty=format:%H|%aI|%an|%s", "--", path],
            cwd=root, capture_output=True, text=True, timeout=10,
        )
        history = []
        for line in result.stdout.strip().splitlines():
            if "|" in line:
                parts = line.split("|", 3)
                if len(parts) == 4:
                    history.append({
                        "hash": parts[0],
                        "date": parts[1],
                        "author": parts[2],
                        "message": parts[3],
                    })
        return templates.TemplateResponse(
            request=request,
            name="partials/file_history.html",
            context={"path": path, "history": history, "user": user},
        )
    except Exception:
        logging.exception("Failed to load git history for path %r", path)
        return HTMLResponse('<div class="error">Historie konnte nicht geladen werden.</div>')


@app.post("/ui/rename", response_class=HTMLResponse)
async def ui_rename(
    request: Request,
    user: User = Depends(require_role("write")),
) -> HTMLResponse:
    form = await request.form()
    old_path = form.get("old_path", "").strip()
    new_path = form.get("new_path", "").strip()
    if not old_path or not new_path:
        return HTMLResponse('<div class="error">Ungültige Parameter</div>', status_code=400)
    try:
        from .storage import move_file as _move_file
        validate_markdown_content_path(new_path)
        _move_file(old_path, new_path)
        deindex_file(old_path)
        index_file(new_path)
        return HTMLResponse(f'<span class="item-name">{html.escape(new_path.rsplit("/", 1)[-1])}</span>')
    except (ValueError, FileNotFoundError) as exc:
        return HTMLResponse(f'<div class="error">{html.escape(str(exc))}</div>', status_code=400)
    except Exception:
        logging.exception("Failed to rename %r to %r", old_path, new_path)
        return HTMLResponse('<div class="error">Umbenennen fehlgeschlagen.</div>', status_code=400)


@app.post("/ui/export")
async def ui_export(request: Request) -> HTMLResponse:
    form = await request.form()
    paths_raw = form.get("paths", "")
    paths = [p.strip() for p in paths_raw.split(",") if p.strip()]
    if not paths:
        raise HTTPException(status_code=400, detail="No paths provided")
    parts = []
    for p in paths:
        try:
            fc = read_file(p)
            parts.append(fc.content)
        except Exception:
            continue
    combined = "\n\n---\n\n".join(parts)
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(combined, media_type="text/markdown; charset=utf-8",
                             headers={"Content-Disposition": "attachment; filename=kiwiki-export.md"})


# ---------------------------------------------------------------------------
# REST API (Bearer token auth)
# ---------------------------------------------------------------------------

def _api_bad_request(exc: Exception) -> HTTPException:
    """Nur kontrollierte Validierungsfehler an API-Clients weitergeben."""
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc)[:300])
    logging.getLogger("kiwiki.api").exception("Unexpected API request failure")
    return HTTPException(status_code=400, detail="Request could not be processed")

@app.get("/api/files")
async def api_list_files(path: str = ".", user: User = Depends(get_current_user)):
    try:
        from starlette.responses import JSONResponse
        resp = JSONResponse([item.model_dump() for item in list_files(path)])
        # A7: Short cache for read-only listing
        resp.headers["Cache-Control"] = "private, max-age=5"
        return resp
    except Exception as exc:
        raise _api_bad_request(exc)


@app.get("/api/file")
async def api_read_file(path: str, user: User = Depends(get_current_user)):
    try:
        from starlette.responses import JSONResponse
        resp = JSONResponse(read_file(path).model_dump())
        # A7: Short cache for read-only file content
        resp.headers["Cache-Control"] = "private, max-age=5"
        return resp
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File not found")
    except Exception as exc:
        raise _api_bad_request(exc)


@app.put("/api/file")
async def api_write_file(req: WriteFileRequest, user: User = Depends(require_role("write"))):
    try:
        validate_markdown_content_path(req.path)
        result = write_file(req.path, req.content, expected_revision=req.expected_revision)
        index_file(req.path)
        return result
    except Exception as exc:
        raise _api_bad_request(exc)


@app.patch("/api/file/frontmatter")
async def api_update_frontmatter(req: UpdateFrontmatterRequest, user: User = Depends(require_role("write"))):
    try:
        validate_markdown_content_path(req.path)
        result = update_frontmatter(req.path, req.updates)
        index_file(req.path)
        return result
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File not found")
    except Exception as exc:
        raise _api_bad_request(exc)


@app.post("/api/file/append")
async def api_append_file(req: AppendFileRequest, user: User = Depends(require_role("write"))):
    try:
        validate_markdown_content_path(req.path)
        result = append_file(req.path, req.content)
        index_file(req.path)
        return result
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File not found")
    except Exception as exc:
        raise _api_bad_request(exc)


@app.post("/api/folder")
async def api_create_folder(req: CreateFolderRequest, user: User = Depends(require_role("write"))):
    try:
        validate_content_folder_path(req.path)
        create_folder(req.path)
        return {"path": req.path, "status": "created"}
    except Exception as exc:
        raise _api_bad_request(exc)


@app.post("/api/move")
async def api_move(req: MoveRequest, user: User = Depends(require_role("write"))):
    try:
        from .storage import safe_path
        validate_content_folder_path(req.src)
        validate_content_folder_path(req.dst)
        src_path = safe_path(req.src)
        if src_path.is_dir():
            old_paths = [
                str(item.relative_to(user_root()))
                for item in sorted(src_path.rglob("*.md"))
            ]
            move_folder(req.src, req.dst)
            _reindex_moved_folder(req.src, req.dst, old_paths)
        else:
            move_file(req.src, req.dst)
            deindex_file(req.src)
            index_file(req.dst)
        return {"src": req.src, "dst": req.dst, "status": "moved"}
    except Exception as exc:
        raise _api_bad_request(exc)


@app.post("/api/search")
async def api_search(req: SearchRequest, user: User = Depends(get_current_user)):
    try:
        return search_files(req.query)
    except Exception as exc:
        raise _api_bad_request(exc)


@app.post("/api/note")
async def api_create_note(req: CreateNoteRequest, user: User = Depends(require_role("write"))):
    try:
        path = create_note(req.title, req.content, req.tags, user.username)
        index_file(path)
        return {"path": path, "status": "created"}
    except Exception as exc:
        raise _api_bad_request(exc)


@app.post("/api/reindex")
async def api_reindex(user: User = Depends(require_role("admin"))):
    try:
        count = reindex_all()
        return {"status": "reindexed", "count": count}
    except Exception as exc:
        raise _api_bad_request(exc)


@app.get("/api/users")
async def api_list_users(user: User = Depends(require_role("admin"))):
    return [
        {
            "username": record.username,
            "role": record.role,
            "source": record.source,
            "builtin": record.source == "builtin",
        }
        for record in user_store.list_users()
    ]


@app.post("/api/users/generate-key")
async def api_generate_user_key(user: User = Depends(require_role("admin"))):
    try:
        return {"key": user_store.generate_api_key()}
    except Exception as exc:
        raise _api_bad_request(exc)


@app.post("/api/users")
async def api_create_user(req: CreateUserRequest, user: User = Depends(require_role("admin"))):
    # Atomare User-Erstellung: erst Workspace vollstaendig initialisieren,
    # dann erst den User-Eintrag persistieren. Bricht die Persistierung ab,
    # wird der bereits angelegte Workspace wieder entfernt — der User
    # existiert dann weder im Store noch auf der Platte.
    username = req.username.strip()
    key = req.key.strip()
    role = req.role.strip()

    _validate_create_user_input(username, key, role)
    _check_user_collisions(username, key)

    workspace_created = _init_user_workspace(username, user.username)
    try:
        record = _persist_new_user(username, key, role)
    except Exception:
        if workspace_created:
            _rollback_workspace(username)
        raise

    return {
        "username": record.username,
        "role": record.role,
        "source": record.source,
        "status": "created",
    }


def _validate_create_user_input(username: str, key: str, role: str) -> None:
    from .tenancy import is_valid_username as _valid_user
    if not _valid_user(username):
        raise HTTPException(status_code=400, detail="Benutzername muss [a-zA-Z0-9_-]{1,64} entsprechen")
    if not key or ":" in key or "," in key:
        raise HTTPException(status_code=400, detail="API-Key ungueltig")
    if role not in ROLE_HIERARCHY:
        raise HTTPException(status_code=400, detail="Unbekannte Rolle")


def _check_user_collisions(username: str, key: str) -> None:
    existing = user_store.users_by_key()
    if key in existing:
        raise HTTPException(status_code=400, detail="API-Key wird bereits verwendet")
    if any(u.username == username for u in existing.values()):
        raise HTTPException(status_code=400, detail="Benutzername existiert bereits")


def _init_user_workspace(username: str, fallback_ns: str) -> bool:
    """Phase 1 aufbauen; liefert True nur fuer neu erzeugte Workspaces."""
    from .tenancy import current_user_ns as _cur_ns
    from .tenancy import base_data_dir

    workspace_created = not (base_data_dir() / username).exists()
    prev_ns = None
    try:
        prev_ns = _cur_ns()
        set_user_ns(username)
        ensure_user_workspace(username)
        init_db()
        reindex_all()
    except Exception as exc:
        if workspace_created:
            _rollback_workspace(username)
        raise _api_bad_request(exc)
    finally:
        if prev_ns is not None:
            set_user_ns(prev_ns or fallback_ns)
    return workspace_created


def _persist_new_user(username: str, key: str, role: str) -> object:
    """Phase 2: User-Eintrag persistieren. Bricht ab, wenn es scheitert."""
    try:
        return user_store.create_local_user(username, key, role)
    except Exception as exc:
        raise _api_bad_request(exc)


def _rollback_workspace(username: str) -> None:
    try:
        user_store.remove_workspace_for_user(username)
    except Exception:
        logging.exception("Failed to remove workspace for user %r", username)


@app.delete("/api/users/{username}")
async def api_delete_user(username: str, user: User = Depends(require_role("admin"))):
    if username == user.username:
        raise HTTPException(status_code=400, detail="Der aktuell angemeldete Benutzer kann nicht gelöscht werden")
    try:
        user_store.delete_local_user(username)
        session_store.revoke_all_for_user(username)
        return {"username": username, "status": "deleted"}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="User not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.delete("/api/folder")
async def api_delete_folder(path: str, user: User = Depends(require_role("admin"))):
    try:
        validate_content_folder_path(path)
        from .storage import safe_path

        folder_path = safe_path(path)
        indexed_paths = [
            str(item.relative_to(user_root()))
            for item in folder_path.rglob("*.md")
        ] if folder_path.exists() else []
        delete_folder(path)
        for indexed_path in indexed_paths:
            deindex_file(indexed_path)
        return {"path": path, "status": "deleted"}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Folder not found")
    except Exception as exc:
        raise _api_bad_request(exc)


@app.delete("/api/file")
async def api_delete_file(path: str, user: User = Depends(require_role("admin"))):
    try:
        validate_markdown_content_path(path)
        delete_file(path)
        deindex_file(path)
        return {"path": path, "status": "deleted"}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File not found")
    except Exception as exc:
        raise _api_bad_request(exc)
