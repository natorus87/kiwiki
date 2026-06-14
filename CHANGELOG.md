# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/natorus87/kiwiki/compare/v2.0.0...HEAD
[2.0.0]: https://github.com/natorus87/kiwiki/releases/tag/v2.0.0
[0.1.0]: https://github.com/natorus87/kiwiki/releases/tag/v0.1.0
