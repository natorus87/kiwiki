# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
