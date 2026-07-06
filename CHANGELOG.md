# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- **Startseite Layout-Reihenfolge** — Dashboard-Panels („Zuletzt bearbeitet" / „Zuletzt erstellt") erscheinen jetzt unter dem kiwiki-Hero-Block statt darüber. Fokus liegt jetzt zuerst auf Branding/Tagline, dann auf recent activity.

## [2.5.0] - 2026-07-03

### Added
- **Dashboard auf der Startseite** — Zwei Panels „Zuletzt bearbeitet" und „Zuletzt erstellt" oben auf der Startseite (je bis zu 8 Dateien, rekursiv durchsucht). Ermöglicht schnellen Zugriff auf letzte Arbeitsschritte. Hero-Sektion mit Info-Panel darunter verschoben.
- **`/ui/recent-edited`** — Neuer HTMX-Endpoint: Dateien sortiert nach Frontmatter `updated` (rekursiv).
- **`/ui/recent-created`** — Neuer HTMX-Endpoint: Dateien sortiert nach Frontmatter `created` (rekursiv).
- **`created`-Feld in `list_all_files`** — `storage.py` liefert jetzt auch das `created`-Frontmatter-Feld für alle Dateien.

### Fixed
- **Touch-Swipe-Geste komplett tot auf iPad** — Zwei kritische Bugs: (1) `kiwiki.js` wurde vor dem DOM geladen (Script in `layout.html` Zeile 32, Sidebar-Element erst Zeile 58), IIFE fand `null` und boundete keine Listener. Fix: `DOMContentLoaded`-Wrapper + `querySelector` inside `bind()`. (2) Breakpoint `max-width: 768px` schloss alle iPads aus (810–1024px). Fix: Alle Breakpoints auf `1024px` erweitert (CSS + JS).
- **Häufige Abmeldung** — Session-Store war rein in-memory; bei jedem Container-Restart gingen alle Sessions verloren. Sessions nutzten `time.monotonic()` (reset bei Neustart) und hatten keine Sliding Expiration (Ablauf nach 12h egal ob aktiv). Fix: JSON-Datei-Persistenz (`/data/sessions.json`), `time.time()`, Sliding Expiration bei jedem Zugriff.

### Changed
- **Touch-Swipe-Geste** für Sidebar: Swipe-Right zum Öffnen funktioniert jetzt vom **gesamten Content-Bereich** (nicht mehr nur von der linken Bildschirmkante). Swipe-Left zum Schließen funktioniert von überall auf der Sidebar. `edgeZone`-Threshold entfernt, `openThreshold` auf 60px gesetzt. Handler wird immer gebunden (kein viewport-Check bei Laden mehr).

## [2.2.0] - 2026-07-01

### Added — Accessibility
- **`<main>` landmark + Skip-Link** („Zum Inhalt springen") für Tastatur-Screader-Nutzer (`layout.html`)
- **ARIA tree roles**: Dateibaum jetzt als `role="tree"` mit `treeitem`/`group`/`aria-level`/`aria-expanded`
- **Focus-Trap** in `kwDialog`-Modalen — Tab/Shift+Tab bleibt im Dialog
- **Mobile-Sidebar-Escape**: `Esc` schließt Sidebar, Fokus springt zurück an Hamburger
- **`aria-live`** auf Toast-Stack und Search-Ergebnisse; Error-Toasts sind `role="alert"`
- **`aria-label`** auf Mobile Selektions-Buttons (Verschieben/Tags/Export/Löschen)
- **Tags klickbar**: Erzeugen auf Klick eine Suche mit `tag:<value>`-Präfix
- **Tag-Suche** (`tag:<value>`) in FTS5-Suche via LIKE-Fallback auf `tags`-Spalte
- **Reduced-Motion** globaler Schutz bereits am Top des Stylesheets + im Login

### Added — UX
- **`kwNewNote()`** — „Neue Notiz" fragt Dateinamen ab statt `notes/neue-notiz.md` zu überschreiben
- **Editor `beforeunload`-Warnung** bei ungespeicherten Änderungen
- **„Keine Treffer für …"** als `role="status"` in den Suchergebnissen
- **Sidebar-Resizer** jetzt sichtbar (4px Hover-Indikator statt versteckt)

### Changed
- **`.btn-danger`** klar rot abgesetzt (Error-dim Hintergrund, Error-Border) — Löschaktionen wirken nicht mehr harmlos
- **`.file-meta`** nutzt `--md-on-surface-v` statt schwachem `--md-outline` (besserer Kontrast)
- **Breadcrumb** als `<button>` statt `<a href="#">` (funktionierte ohne JS tot)
- **Editor-Save-Toast** via zentralem `kwToast()` statt eigenem `.save-toast`-Markup
- **Tree-Filter** + Select-Toggle auf Mobile jetzt 44×44px / 16px Font (WCAG 2.2, kein iOS-Zoom)
- **Settings-Grid** responsive bis 1024px (2-Spalten, Submit in eigener Zeile)
- **Hint-Text "Doppelklick zum Umbenennen"** statt englischsprachigem „double-click to rename"
- **`#file-tree` `tabindex="0"`** entfernt (keine doppelten Tab-Stops neben inneren Buttons)
- **`role="status"` + `aria-busy`-markiertes Loading-Hint** für Tree/Recent-Reloads

### Fixed — Codebase
- **CSS-Konsolidierung**: Redundantes zweites `:root` aus dem „Professional UI refresh"-Block entfernt — Token-Quelle jetzt eindeutig
- **Leerer `header-right`-Platzhalter** entfernt

### Tests
- `tests/test_ui_file.py` um Regression-Tests für: Tags als klickbare Buttons, Tag-Suche, `<main>`-Landmark + Skip-Link ergänzt
- 136 Tests grün, Ruff clean

### Docs
- **`docs/ui-accessibility.md`** neu: WCAG 2.2 AA-Modell, Tastatur-Shortcuts, ARIA-Tree, Touch-Targets, Fokus-Management, PR-Checkliste
- **`docs/architecture.md`** neu: Template-Hierarchie, Tenancy/Request-Flow, Helper-Konventionen, Cache-Busting, Test-Matrix
- **`README.md`** um v2.2-Features, Keyboard-Shortcuts-Tabelle und Architektur-Verweis ergänzt
- **`CONTRIBUTING.md`** um Frontend-Workflow, UI-PR-Checkliste, Helper-Naming ergänzt
- **In-Code-Kommentare** an `kwDialog` (Focus-Trap), `openSidebar`/`closeSidebar` (Fokus-Management), `kwNewNote`/`kwSearchTag` (Zweck) und `beforeunload`-Guard

## [2.1.1] - 2026-07-01

### Fixed
- **Edit button missing in mobile view**: `ui_file` endpoint did not pass `user` to the template context, so `{% if user and user.role in ['write', 'admin'] %}` was always false — the "Bearbeiten" button was hidden for everyone, most noticeable on mobile/tablet

### Added
- Regression test `tests/test_ui_file.py` covering Edit/Export/Delete button visibility per role

## [2.1.0] - 2026-06-29

### Added
- **12 new MCP tools** (46 total): `git_commit`, `file_history`, `diff`, `statistics`, `template`, `validate_links`, `link_graph`, `rename`, `batch_tag`, `export`, `duplicate_check`, `ai_summarize`
- **Multi-select mode**: Toggle via toolbar button, context menu, or FAB — batch delete, move, tag, export
- **Inline rename**: Double-click file name in tree to rename; also in context menu
- **Breadcrumb navigation**: Path hierarchy in content area with click-to-navigate
- **Copy path button**: Clipboard copy for file paths
- **Sidebar filter**: Live filtering of file tree
- **Recently opened files**: Quick access on home page
- **Markdown export**: Download single files or selections as .md
- **Keyboard shortcuts**: `dd` delete, `mm` move, `ee` edit, `rr` rename, `Esc` clear selection
- **Floating Action Button (FAB)**: Quick actions on mobile (new note, file, folder, multi-select)
- **Touch gestures**: Swipe right to open sidebar, swipe left to close
- **Editor floating save button**: Mobile-friendly save action
- **Security Headers Middleware**: X-Content-Type-Options, X-Frame-Options, Referrer-Policy, HSTS
- **Template system**: Create notes from templates (meeting, decision, adr, review, bug, feature)

### Changed
- **bleach → nh3 migration**: Deprecated bleach replaced with faster Rust-based nh3 sanitizer
- **MCP tool errors**: Now return `isError: true` in result (MCP spec compliant, fixes ChatGPT error reports)
- **SQLite connections**: Refactored to context manager pattern (prevents connection leaks)
- **All datetime calls**: Use `datetime.now(timezone.utc)` — no more naive datetimes
- **CORS default**: Changed from `*` to `""` (disabled = secure default)
- **`KIWIKI_TRUST_PROXY`**: Default unified to `false` (was inconsistent between files)
- **pytest**: Moved from `requirements.txt` to `requirements-dev.txt` (prod image cleanup)
- **SVG icons**: Consistent Lucide-style set with proper stroke-width=2
- **Visual polish**: Gradient glow hero, terminal-style code blocks, improved button hover states, noise texture overlay
- **Step-card icons**: Distinct colors per category (green/teal/warm/purple)
- **Delete button**: Softer default style (gray, red on hover only)
- **Context menu**: Added "Umbenennen" and "Mehrfachauswahl" options
- **pyproject.toml**: Added project metadata and pytest config

### Fixed
- **Rate-limiter typo**: "spatieren" → "später erneut versuchen"
- **MCP error format**: ChatGPT now correctly reports tool errors instead of transport errors
- **Selection bar**: Hidden by default (was overriding `hidden` attribute)
- **nh3 `link_rel`**: Fixed error when rendering links with `rel` attribute

## [2.0.2] - 2026-06-14

### Added
- README: "Usage as AI Memory" section with ChatGPT/Claude personalization setup instructions
- README: "Agent Harness Setup" subsection with MCP connection commands for Claude Code, Codex, OpenCode, Cursor
- Helm chart: secret template for OAuth token secret
- Dockerfile: non-root user, healthcheck

### Changed
- Dockerfile: optimized layer caching, switched to non-root user
- docker-compose.yml: environment alignment, secret support
- app/mcp_server.py: OAuth redirect_uri validation improvements
- app/storage.py: improved error handling
- UI (CSS, settings): polish and responsive fixes
- Charts: deployment and values alignment

## [2.0.1] - 2026-06-14

### Removed
- CLAUDE.md and CLAUDE.local.template.md from repository (project-agent config kept local)

## [2.0.0] - 2026-06-14

### Added
- OAuth 2.1 Authorization Code + PKCE flow for MCP client authentication
- Dynamic Client Registration (RFC 7591) support
- OAuth Discovery endpoints (RFC 8414, RFC 9728)
- Refresh token support for long-lived MCP sessions
- ChatGPT MCP connector OAuth compatibility

### Fixed
- ChatGPT OAuth redirect_uri validation for dynamically generated client IDs

### Added

#### Web UI & Frontend
- Responsive web interface with Jinja2 templating
- HTMX integration for interactive UI without page reloads
- Toast UI Editor for rich Markdown editing
- Search interface with real-time full-text search
- File browser with navigation tree
- Session cookie-based authentication for web clients

#### REST API
- Complete file CRUD operations (Create, Read, Update, Delete)
- Full-text search via SQLite FTS5
- Reindex endpoint for search index regeneration
- Bearer Token API key authentication
- RESTful endpoints with standard HTTP status codes

#### MCP Server (Model Context Protocol)
- 15 specialized tools for file management and search:
  - File CRUD Operations
  - Full-Text Search
  - Index Management
  - Metadata Extraction
  - Batch Operations
- Dual-Transport Support:
  - POST `/mcp` — Streamable HTTP (modern format)
  - GET `/mcp/sse` — HTTP+SSE (fallback for legacy clients)
- Standard MCP Protocol Implementation (v1.0)

#### Data Management
- SQLite FTS5 engine for full-text search
- Markdown files as core data format
- Hierarchical folder structure with frontmatter metadata
- Automatic index management

#### Security & Access Control
- Role-based system with three levels:
  - `read` — Read-only access
  - `write` — Read + write
  - `admin` — Full access including user management
- API key-based authentication for programmatic access
- Session management for web UI
- CORS configuration for secure cross-origin requests

#### Deployment & Infrastructure
- Docker image with Python 3.12 base
- docker-compose configuration for local development
- Helm chart for Kubernetes deployment
- Environment variable configuration
- Health check endpoints

#### Documentation
- README with project overview
- API documentation with Swagger/OpenAPI
- MCP protocol documentation
- Deployment guides (Docker, Kubernetes)
- Installation & setup instructions

### Technical Details

- **Backend:** Python 3.12 + FastAPI + Starlette 1.0
- **Database:** SQLite with FTS5 extension
- **Frontend:** Jinja2 + HTMX + Toast UI
- **API Standard:** OpenAPI 3.0
- **Protocol:** Model Context Protocol (MCP) v1.0
- **Container:** Docker + docker-compose
- **Orchestration:** Helm charts for Kubernetes

[Unreleased]: https://github.com/natorus87/kiwiki/compare/v2.0.2...HEAD
[2.0.2]: https://github.com/natorus87/kiwiki/releases/tag/v2.0.2
[2.0.1]: https://github.com/natorus87/kiwiki/releases/tag/v2.0.1
[2.0.0]: https://github.com/natorus87/kiwiki/releases/tag/v2.0.0
[0.1.0]: https://github.com/natorus87/kiwiki/releases/tag/v0.1.0
