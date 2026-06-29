import html
import logging
import os
import re
from pathlib import Path

import markdown as md_lib
import nh3
import yaml
from fastapi import FastAPI, Depends, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest

from . import session_store, user_store
from .auth import ROLE_HIERARCHY, get_current_user, parse_users, require_role
from .mcp_server import router as mcp_router
from .models import (
    AppendFileRequest,
    CreateFolderRequest,
    CreateNoteRequest,
    CreateUserRequest,
    MoveRequest,
    SearchRequest,
    User,
    WriteFileRequest,
)
from .rate_limiter import RateLimitMiddleware
from .search import deindex_file, index_file, init_db, reindex_all, search as search_files
from .storage import (
    append_file,
    create_folder,
    create_note,
    delete_file,
    delete_folder,
    list_files,
    move_file,
    move_folder,
    read_file,
    validate_content_folder_path,
    validate_markdown_content_path,
    write_file,
)
from .tenancy import (
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


def _render_markdown_safe(content: str) -> str:
    rendered = md_lib.markdown(
        content,
        extensions=["fenced_code", "tables", "nl2br"],
    )
    return nh3.clean(
        rendered,
        tags=_NH3_TAGS,
        attributes=_NH3_ATTRS,
        url_schemes={"http", "https", "mailto"},
        link_rel=None,
    )


def _reindex_moved_folder(src: str, dst: str, old_paths: list[str]) -> None:
    for old_path in old_paths:
        deindex_file(old_path)
        suffix = old_path[len(src):].lstrip("/")
        new_path = f"{dst.rstrip('/')}/{suffix}" if suffix else dst
        index_file(new_path)

app = FastAPI(title="kiwiki", version="0.1.0")
app.include_router(mcp_router)

# ---------------------------------------------------------------------------
# CORS — optional: KIWIKI_CORS_ORIGINS = "https://wiki.example,https://api.example"
# Default "*" mit WARNS-LOG — in Produktivumgebung auf erlaubte Origins setzen
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
        if request.url.scheme == "https":
            response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        return response


app.add_middleware(SecurityHeadersMiddleware)

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
    set_user_ns(record.username)
    return User(username=record.username, role=record.role)


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup() -> None:
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
        reindex_all()


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Login / Logout
# ---------------------------------------------------------------------------

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    # Already logged in → go home
    if _session_user(request):
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse(request=request, name="login.html", context={"error": None})


@app.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, api_key: str = Form(...)):
    users_map = parse_users()
    if api_key not in users_map:
        return templates.TemplateResponse(
            request=request, name="login.html",
            context={"error": "Ungültiger API-Key"},
            status_code=401,
        )
    username, role = users_map[api_key]
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
        samesite="lax",
        max_age=session_store.session_ttl_seconds(),
    )
    return response


@app.get("/logout")
async def logout(request: Request):
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
async def index(request: Request):
    user = _session_user(request)
    return templates.TemplateResponse(request=request, name="index.html", context={"user": user})


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    user = _session_user(request)
    if not user or ROLE_HIERARCHY.get(user.role, -1) < ROLE_HIERARCHY["admin"]:
        raise HTTPException(status_code=403, detail="Admin role required")
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={"user": user, "users": user_store.list_users()},
    )


@app.get("/editor", response_class=HTMLResponse)
async def editor(request: Request, path: str = ""):
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
        except Exception as exc:
            load_error = str(exc)
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
    '<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>'
    '</svg>'
)
_SVG_FILE = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24"'
    ' fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"'
    ' stroke-linejoin="round" class="icon">'
    '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>'
    '<polyline points="14 2 14 8 20 8"/>'
    '</svg>'
)
# Gemeinsame Attribute für Button-Icons — currentColor wird via CSS color-Eigenschaft gesteuert
_SVG_BTN_ATTRS = 'xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="btn-icon"'

_SVG_EDIT = (
    f'<svg {_SVG_BTN_ATTRS}>'
    '<path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>'
    '<path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>'
    '</svg>'
)
_SVG_TRASH = (
    f'<svg {_SVG_BTN_ATTRS}>'
    '<polyline points="3 6 5 6 21 6"/>'
    '<path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>'
    '</svg>'
)
_SVG_FILE_PLUS = (
    f'<svg {_SVG_BTN_ATTRS}>'
    '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>'
    '<polyline points="14 2 14 8 20 8"/>'
    '<line x1="12" y1="18" x2="12" y2="12"/><line x1="9" y1="15" x2="15" y2="15"/>'
    '</svg>'
)
_SVG_FOLDER_PLUS = (
    f'<svg {_SVG_BTN_ATTRS}>'
    '<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>'
    '<line x1="12" y1="11" x2="12" y2="17"/><line x1="9" y1="14" x2="15" y2="14"/>'
    '</svg>'
)
_SVG_MOVE = (
    f'<svg {_SVG_BTN_ATTRS}>'
    '<polyline points="5 9 2 12 5 15"/>'
    '<polyline points="9 5 12 2 15 5"/>'
    '<line x1="2" y1="12" x2="22" y2="12"/>'
    '<line x1="12" y1="2" x2="12" y2="22"/>'
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
async def ui_files(request: Request, path: str = "."):
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
    except Exception as exc:
        return HTMLResponse(f'<div class="error">{html.escape(str(exc))}</div>')


@app.get("/ui/file", response_class=HTMLResponse)
async def ui_file(request: Request, path: str):
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
                "can_delete": can_delete,
                "svg_edit": svg_edit,
                "svg_trash": svg_trash,
            },
        )
    except FileNotFoundError:
        return HTMLResponse('<div class="content-inner"><div class="empty-state error-state"><h2>Datei nicht gefunden</h2><p>Die ausgewählte Datei existiert nicht mehr oder wurde verschoben.</p></div></div>')
    except Exception as exc:
        return HTMLResponse(f'<div class="content-inner"><div class="error">{html.escape(str(exc))}</div></div>')


@app.post("/ui/search", response_class=HTMLResponse)
async def ui_search(request: Request):
    form = await request.form()
    query = form.get("query", "").strip()
    if not query:
        return HTMLResponse("")
    try:
        results = search_files(query)
        return templates.TemplateResponse(
            request=request,
            name="partials/search_results.html",
            context={"results": results},
        )
    except Exception as exc:
        return HTMLResponse(f'<div class="error">{html.escape(str(exc))}</div>')


@app.get("/ui/recent", response_class=HTMLResponse)
async def ui_recent(request: Request):
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


@app.post("/ui/rename", response_class=HTMLResponse)
async def ui_rename(request: Request):
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
    except Exception as exc:
        return HTMLResponse(f'<div class="error">{html.escape(str(exc))}</div>', status_code=400)


@app.post("/ui/export")
async def ui_export(request: Request):
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

@app.get("/api/files")
async def api_list_files(path: str = ".", user: User = Depends(get_current_user)):
    try:
        return list_files(path)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/file")
async def api_read_file(path: str, user: User = Depends(get_current_user)):
    try:
        return read_file(path)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File not found")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.put("/api/file")
async def api_write_file(req: WriteFileRequest, user: User = Depends(require_role("write"))):
    try:
        validate_markdown_content_path(req.path)
        result = write_file(req.path, req.content)
        index_file(req.path)
        return result
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


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
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/folder")
async def api_create_folder(req: CreateFolderRequest, user: User = Depends(require_role("write"))):
    try:
        validate_content_folder_path(req.path)
        create_folder(req.path)
        return {"path": req.path, "status": "created"}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


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
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/search")
async def api_search(req: SearchRequest, user: User = Depends(get_current_user)):
    try:
        return search_files(req.query)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/note")
async def api_create_note(req: CreateNoteRequest, user: User = Depends(require_role("write"))):
    try:
        path = create_note(req.title, req.content, req.tags, user.username)
        index_file(path)
        return {"path": path, "status": "created"}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/reindex")
async def api_reindex(user: User = Depends(require_role("admin"))):
    try:
        count = reindex_all()
        return {"status": "reindexed", "count": count}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


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
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/users")
async def api_create_user(req: CreateUserRequest, user: User = Depends(require_role("admin"))):
    # Atomare User-Erstellung: erst Workspace vollstaendig initialisieren,
    # dann erst den User-Eintrag persistieren. Bricht die Persistierung ab,
    # wird der bereits angelegte Workspace wieder entfernt — der User
    # existiert dann weder im Store noch auf der Platte.
    username = req.username.strip()
    key = req.key.strip()
    role = req.role.strip()

    # Validierung frueh (ohne Seiteneffekte), damit kein halb-angelegter
    # Workspace zurueckgerollt werden muss.
    from .tenancy import is_valid_username as _valid_user
    if not _valid_user(username):
        raise HTTPException(status_code=400, detail="Benutzername muss [a-zA-Z0-9_-]{1,64} entsprechen")
    if not key or ":" in key or "," in key:
        raise HTTPException(status_code=400, detail="API-Key ungueltig")
    if role not in ROLE_HIERARCHY:
        raise HTTPException(status_code=400, detail="Unbekannte Rolle")

    try:
        # Kollisionscheck BEVOR wir den Workspace anlegen — so vermeiden
        # wir, bei Duplikaten einen leeren Workspace anlegen zu muessen.
        existing = user_store.users_by_key()
        if key in existing:
            raise HTTPException(status_code=400, detail="API-Key wird bereits verwendet")
        if any(u.username == username for u in existing.values()):
            raise HTTPException(status_code=400, detail="Benutzername existiert bereits")

        # Phase 1: Workspace + DB + Index aufbauen. Wenn etwas scheitert,
        # gibt es noch keinen User-Eintrag — kein Rollback noetig.
        prev_ns = None
        try:
            from .tenancy import current_user_ns as _cur_ns

            prev_ns = _cur_ns()
            set_user_ns(username)
            ensure_user_workspace(username)
            init_db()
            reindex_all()
        except Exception as exc:
            # Phase 1 fehlgeschlagen — halb angelegten Workspace entsorgen,
            # damit der Username wieder sauber ist.
            try:
                user_store.remove_workspace_for_user(username)
            except Exception:
                logging.exception("Failed to remove workspace after initialization failure for user %r", username)
            raise HTTPException(status_code=400, detail=f"Workspace-Init fehlgeschlagen: {exc}")
        finally:
            if prev_ns is not None:
                set_user_ns(prev_ns or user.username)

        # Phase 2: User-Eintrag persistieren. Bei Fehlschlag den in
        # Phase 1 angelegten Workspace wieder entfernen.
        try:
            record = user_store.create_local_user(username, key, role)
        except Exception as exc:
            try:
                user_store.remove_workspace_for_user(username)
            except Exception:
                logging.exception("Failed to remove workspace after local user persistence failure for user %r", username)
            raise HTTPException(status_code=400, detail=str(exc))

        return {
            "username": record.username,
            "role": record.role,
            "source": record.source,
            "status": "created",
        }
    except HTTPException:
        raise
    except Exception as exc:
        # Letzter Auffangnetz — bei unerwarteten Fehlern saubermachen.
        try:
            user_store.remove_workspace_for_user(username)
        except Exception:
            logging.exception("Failed to remove workspace after unexpected user creation failure for user %r", username)
        raise HTTPException(status_code=400, detail=str(exc))


@app.delete("/api/users/{username}")
async def api_delete_user(username: str, user: User = Depends(require_role("admin"))):
    if username == user.username:
        raise HTTPException(status_code=400, detail="Der aktuell angemeldete Benutzer kann nicht gelöscht werden")
    try:
        user_store.delete_local_user(username)
        return {"username": username, "status": "deleted"}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.delete("/api/folder")
async def api_delete_folder(path: str, user: User = Depends(require_role("admin"))):
    try:
        validate_content_folder_path(path)
        delete_folder(path)
        return {"path": path, "status": "deleted"}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Folder not found")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


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
        raise HTTPException(status_code=400, detail=str(exc))
