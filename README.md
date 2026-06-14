# kiwiki

![Docker Hub](https://img.shields.io/docker/v/natorus87/kiwiki?label=Docker%20Hub&logo=docker)
![Python](https://img.shields.io/badge/python-3.12-blue)
![License](https://img.shields.io/badge/license-MIT-green)

kiwiki is a self-hosted Markdown knowledge base for humans and AI agents. Notes stay as regular files on disk, the web UI gives you a searchable wiki, and the built-in MCP server lets tools such as ChatGPT, Claude, Cursor, Codex, and other MCP clients read and write the same knowledge base.

No hosted account is required. Your Markdown files are the source of truth.

## Screenshots

![kiwiki dashboard](docs/assets/screenshots/kiwiki-dashboard.png)

![kiwiki note view](docs/assets/screenshots/kiwiki-note-view.png)

![kiwiki search](docs/assets/screenshots/kiwiki-search.png)

<p align="center">
  <img src="docs/assets/screenshots/kiwiki-mobile.png" alt="kiwiki mobile layout" width="360">
</p>

## Features

- Markdown files with YAML frontmatter
- Per-user isolated wiki folders under `/data/<username>/`
- FastAPI web app with Jinja2, HTMX, and Toast UI Editor
- SQLite FTS5 full-text search
- Streamable HTTP MCP endpoint at `/mcp`
- Legacy HTTP/SSE MCP endpoint at `/mcp/sse`
- OAuth 2.1 discovery and authorization-code flow for ChatGPT-style MCP connectors
- Role-based access control: `read`, `write`, `admin`
- Docker Compose and Helm deployment support

## Quick Start

```bash
git clone https://github.com/natorus87/kiwiki.git
cd kiwiki
cp .env.example .env
docker compose up -d
```

Open the web UI:

```text
http://localhost:8082
```

Use the API key configured in `KIWIKI_USERS`.

## Configuration

All runtime configuration is done through environment variables.

| Variable | Default | Description |
|---|---:|---|
| `KIWIKI_DATA_DIR` | `/data` | Data directory for all wiki files |
| `KIWIKI_USERS` | required | Built-in users in `user:key:role` format, comma-separated |
| `KIWIKI_BASE_URL` | `http://localhost:8080` | Public base URL used in MCP and OAuth metadata |
| `KIWIKI_LOG_LEVEL` | `INFO` | Python log level |
| `KIWIKI_TRUST_PROXY` | `true` | Use secure cookies behind a TLS reverse proxy |
| `KIWIKI_CORS_ORIGINS` | `*` | Comma-separated list of allowed CORS origins |
| `KIWIKI_RATE_LIMIT_ENABLED` | `true` | Enables login, read, and write rate limits |
| `KIWIKI_OAUTH_TOKEN_SECRET` | derived | Optional stable secret for signing OAuth MCP tokens |
| `KIWIKI_OAUTH_TOKEN_TTL_SECONDS` | `86400` | OAuth access-token lifetime |
| `KIWIKI_OAUTH_REFRESH_TOKEN_TTL_SECONDS` | `2592000` | OAuth refresh-token lifetime |

Example:

```env
KIWIKI_DATA_DIR=/data
KIWIKI_USERS=admin:<admin-api-key>:admin,writer:<writer-api-key>:write,reader:<reader-api-key>:read
KIWIKI_BASE_URL=https://kiwiki.example.com
KIWIKI_TRUST_PROXY=true
KIWIKI_CORS_ORIGINS=https://kiwiki.example.com
KIWIKI_OAUTH_TOKEN_SECRET=<random-token-signing-secret>
```

Generate strong keys with:

```bash
openssl rand -hex 24
```

Do not commit real API keys, OAuth secrets, `.env` files, or local wiki data.

## User Model

Each user gets a separate wiki root:

```text
/data/admin/
/data/alice/
/data/bob/
```

Users cannot read or search another user's files. Web UI requests, REST API calls, search, and MCP tools all run in the authenticated user's namespace.

Roles:

| Role | Permissions |
|---|---|
| `read` | Read files and search |
| `write` | Read plus create, edit, move, and reindex |
| `admin` | Full access, including delete and user management |

## ChatGPT and MCP

kiwiki exposes MCP over Streamable HTTP:

```text
https://kiwiki.example.com/mcp
```

For ChatGPT custom connectors, configure the MCP endpoint as the connector URL. If OAuth is enabled, ChatGPT discovers:

```text
/.well-known/oauth-protected-resource/mcp
/.well-known/oauth-authorization-server/mcp
```

The OAuth flow uses:

- Authorization code with PKCE
- Signed access tokens
- Refresh tokens
- The `resource` parameter expected by MCP clients
- Client ID Metadata Document style client IDs used by ChatGPT

For public deployments, set a stable `KIWIKI_OAUTH_TOKEN_SECRET`. This keeps connector tokens valid across container restarts while still allowing API-key rotation to revoke access.

Direct bearer-token access is also supported:

```bash
curl https://kiwiki.example.com/mcp \
  -H "Authorization: Bearer <api-key>" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

## MCP Tools

The MCP server exposes tools for common wiki workflows:

- `read_index`
- `list_files`
- `read_file`
- `fetch`
- `write_file`
- `append_file`
- `search`
- `create_note`
- `edit`
- `update_frontmatter`
- `read_many`
- `build_index`
- `list_all_files`
- `grep`
- `find`
- `file_info`
- `read_lines`
- `recent_files`
- `backlinks`
- `preview_edit`
- `replace_many`
- `validate_wiki`
- `upsert_note`
- `related_files`
- `tag_index`
- `reindex_all`
- `search_status`
- `whoami`

## Local Development

Create a virtual environment and install dependencies:

```bash
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
npm ci
```

Run the app:

```bash
KIWIKI_DATA_DIR=./data \
KIWIKI_USERS="admin:dev-key:admin" \
KIWIKI_BASE_URL="http://127.0.0.1:8080" \
KIWIKI_TRUST_PROXY=false \
uvicorn app.main:app --host 127.0.0.1 --port 8080 --reload
```

Build the frontend motion bundle:

```bash
npm run build:motion
```

Run checks:

```bash
ruff check app tests
pytest -q
npm audit --audit-level=high
docker build -t kiwiki:test .
```

## Docker

The default Compose file builds the local image and serves the app on port `8082`:

```bash
docker compose up -d
docker compose logs -f kiwiki
```

Persistent data is mounted at:

```text
./data:/data
```

For public deployments, move real secrets into `.env` or your secret manager.

## Helm

A Helm chart is available under `charts/kiwiki`.

Install example:

```bash
helm upgrade --install kiwiki ./charts/kiwiki \
  --set env.KIWIKI_USERS="admin:<admin-api-key>:admin" \
  --set env.KIWIKI_BASE_URL="https://kiwiki.example.com" \
  --set env.KIWIKI_OAUTH_TOKEN_SECRET="<random-token-signing-secret>"
```

Review `charts/kiwiki/values.yaml` before deploying to production.

## Repository Hygiene

The repository includes:

- GitHub Actions CI for Ruff, Pytest, frontend build, and Docker build
- Dependabot configuration for Python, npm, and GitHub Actions
- Issue and pull request templates
- Security policy
- MIT license

Ignored local artifacts include `.venv/`, `node_modules/`, `data/`, Python caches, test caches, and local agent configuration.

## Security

See [SECURITY.md](SECURITY.md).

Important operational rules:

- Use strong random API keys.
- Set `KIWIKI_TRUST_PROXY=true` behind HTTPS.
- Restrict `KIWIKI_CORS_ORIGINS` in production.
- Set `KIWIKI_OAUTH_TOKEN_SECRET` for public MCP/OAuth deployments.
- Do not publish local wiki data or deployment secrets.

## License

MIT. See [LICENSE](LICENSE).
