# Architecture

kiwiki is a FastAPI application with a server-rendered Jinja2 front end. There is no SPA framework by design — the UI stays cheap to operate on small devices and friendly to incremental improvement. This document describes how a request flows, where code lives, and the conventions helpers follow. Read this before changing anything in `app/templates/`, `app/static/`, or the UI endpoints in `app/main.py`.

## High-Level Layout

```
Browser ──HTTP──► FastAPI (app/main.py)
                    │
                    ├── Middleware: rate limiter, security headers, CORS
                    ├── Auth: cookie session OR Bearer API key  (app/auth.py)
                    ├── Tenancy: ContextVar sets per-user namespace (app/tenancy.py)
                    │
                    ├── UI routes (/ui/*) → Jinja2 templates  (app/templates/)
                    ├── REST routes (/api/*) → JSON
                    ├── MCP routes (/mcp, /mcp/sse, /oauth/*)  (app/mcp_server.py)
                    └── Static files (/static/*)              (app/static/)
```

## Tenancy

Every request that touches storage runs inside a user namespace. `app/auth.py` calls `tenancy.set_user_ns(username)` after authenticating, which sets a `ContextVar` consumed by `app/storage.py` and `app/search.py`. This is why handlers can call `read_file(path)` without qualifying the path — the path is resolved against `/data/<username>/`.

Two users can never read each other's files, search each other's index, or share MCP state.

## Request Flow Example: Note View

1. Browser issues GET `/ui/file?path=notes/demo.md`
2. `_session_user(request)` (in `app/main.py`) reads the `kiwiki_session` cookie and returns a `User`
3. `set_user_ns(user.username)` is applied via the auth dependency
4. `read_file("notes/demo.md")` resolves to `/data/<user>/notes/demo.md` via `tenancy.user_root()`
5. The Markdown is sanitized through `nh3` and rendered to HTML via `markdown`
6. `templates.TemplateResponse` renders `partials/file_view.html` with the context:
   - `title`, `path`, `updated`, `owner`, `tags`
   - `rendered` — sanitized HTML
   - `user` — the authenticated user (used by the template's role guard)
   - `can_delete` — admin flag for the delete button
   - `svg_edit`, `svg_trash` — pre-rendered icon strings
7. The partial is swapped into `#main-content` by HTMX (`loadFile()` in `kiwiki.js`)

The `user` key is the historically lost piece that caused the v2.1.1 "edit button missing" bug — the guard `{% if user and user.role in ['write','admin'] %}` evaluated to `Undefined -> False`. Always include `user` in templates that perform role-based rendering.

## Template Hierarchy

```
layout.html                   Base: <head>, header, sidebar slot, <main>, motion bundle
├── index.html                Home dashboard (hero, step grid, recent, FAB, file tree)
├── editor.html               /editor — Toast UI editor (own extra styles, own save flow)
├── settings.html             /settings — admin-only user management
└── login.html                /login — standalone (does NOT extend layout.html)
```

`layout.html` defines the `{% block sidebar %}`, `{% block content %}`, and `{% block extra_styles %}` slots used by index/editor/settings. `login.html` keeps its own CSS because it loads before any session exists.

## Partials (HTMX swaps)

| Partial | Rendered by | Swapped into |
|---|---|---|
| `partials/file_tree.html` | `GET /ui/files?path=…` | `#file-tree` |
| `partials/file_view.html` | `GET /ui/file?path=…` | `#main-content` |
| `partials/search_results.html` | `POST /ui/search` | `#search-results` |
| `partials/sidebar_account.html` | included by `index.html` and `editor.html` | `.sidebar-account` |

Partials must be self-contained — they cannot rely on `<script>` tags or external `<style>` from their parent page. All interactivity for partials lives in `kiwiki.js`, which is loaded once in `layout.html`.

## Static Assets

| File | Role |
|---|---|
| `app/static/kiwiki.css` | Single stylesheet, one `:root` token source, mobile breakpoints |
| `app/static/kiwiki.js` | All UI logic: sidebar, tree state, dialogs, toasts, swipe, FAB |
| `app/static/kiwiki-motion.bundle.js` | Built by `npm run build:motion` (Animate API entrance animations) |

The cache-busting query strings in `layout.html` (`?v=20260701-a11y`) must be bumped whenever `kiwiki.css` or `kiwiki.js` change semantically. Otherwise users still get the old version from cache and report "works after hard refresh" bugs.

## Search

`app/search.py` stores one FTS5 database per user under `/data/<username>/.kiwiki/index.sqlite`. The `search()` function accepts plain queries and the special prefix `tag:<value>`:

```python
# Plain full-text search
search("markdown rendering")

# Prefix-triggered tag search (LIKE on the tags column)
search("tag:python")
```

The prefix path sidesteps FTS5 column filters, which are brittle in SQLite's FTS5. Use it whenever you need structured tag filtering from the UI.

## JS Helper Conventions

All global helpers in `kiwiki.js` are namespaced with the `kw` prefix (`kwToast`, `kwDialog`, `kwNewNote`, `kwSearchTag`, `kwToggleSelect`, …). Legacy helpers (`loadFile`, `openEditor`, `toggleFolder`, `deleteFile`) keep their original names for backward compatibility with templates but should not be extended — prefer `kw*` for new helpers.

Tree state (open folders, active file, scroll position) is persisted in `localStorage` under these keys:

| Key | Purpose |
|---|---|
| `kiwiki:openFolders` | Array of open folder paths |
| `kiwiki:activeFile` | Last opened file path |
| `kiwiki:treeScroll` | Tree last scroll position |
| `kiwiki_sidebar_w` | Desktop sidebar width (resize handle) |

## Roles & Visibility

Role checks happen in three layers and must align — a missing layer causes silent UI bugs:

1. **Server**: `Depends(require_role("admin"))` on the FastAPI route (HTTP 403)
2. **Template**: `{% if user and user.role in ['write','admin'] %}` (HTML visibility)
3. **Client**: `kwCanWrite()` / `kwCanAdmin()` read from `window.KIWIKI.roleLevel`, set in `layout.html` from the session cookie

If any layer is skipped, users see UI they cannot use (or vice versa). The `user` key in the template context is the bridge between server and template — always pass it.

## Testing

| Area | Tests | Runner |
|---|---|---|
| Auth, storage, search, MCP | `tests/test_*.py` (per module) | `pytest` |
| UI rendering regression | `tests/test_ui_file.py` | `pytest` (uses `TestClient`) |
| Lint | All `app/` and `tests/` | `ruff check app tests` |
| Frontend bundle | `frontend/motion/` | `npm run build:motion` |
| Container | `Dockerfile` | `docker build -t kiwiki:test .` |

UI regression tests render a partial with a logged-in `TestClient` and assert on substrings in the HTML (`openEditor(`, `kwSearchTag(`, `<main`, …). They don't yet drive a browser — add Playwright coverage if you need DOM-level guarantees.

## Adding a New Helper

1. **Name**: `kw<Action>` in `app/static/kiwiki.js`
2. **Role check**: `if (!kwCanWrite()) return;` at the top if the action needs write/admin
3. **Feedback**: Use `kwToast(msg)` or `kwToast(msg, {type: 'error'})` for all outcomes (never `alert()`)
4. **Localization**: German UI strings, English code identifiers
5. **Test**: Add a regression test in `tests/test_*.py` — render the partial and assert on the rendered HTML
6. **Cache-bust**: Bump `?v=…` in `layout.html` for both `kiwiki.css` and `kiwiki.js`
7. **Docs**: Update [docs/ui-accessibility.md](ui-accessibility.md) if the change affects keyboard, ARIA, or touch behavior

## Deployment

- Local: `docker compose up -d` (builds the image, mounts `./data`, exposes `:8082`)
- Helm: `charts/kiwiki/` for Kubernetes; review `values.yaml` before production
- Docker Hub: `natorus87/kiwiki:<tag>` — follow the `docker-push` skill workflow

For releases, the `bereitstellung` skill defines the full release flow: clean workspace → tests → build → changelog → tag → push → GitHub release.

## Related Docs

- [ui-accessibility.md](ui-accessibility.md) — WCAG, keyboard model, touch targets, verification checklist
- [../CHANGELOG.md](../CHANGELOG.md) — version history
- [../CONTRIBUTING.md](../CONTRIBUTING.md) — workflow, commit format, UI checklist for PRs
- [../README.md](../README.md) — user-facing overview, configuration, MCP tools