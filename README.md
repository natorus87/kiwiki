# kiwiki

[![Website](https://img.shields.io/badge/web-kiwiki.xyz-blue)](https://kiwiki.xyz)
![Docker Hub](https://img.shields.io/docker/v/natorus87/kiwiki?label=Docker%20Hub&logo=docker)
![Python](https://img.shields.io/badge/python-3.12-blue)
![License](https://img.shields.io/badge/license-MIT-green)

kiwiki is a self-hosted **Agent Harness** and Markdown knowledge base for humans and AI agents. Notes stay as regular files on disk, the web UI gives you a searchable wiki, and the built-in MCP server lets tools such as Claude Code, Codex, OpenCode, Cursor, ChatGPT, OpenClaw, Hermes, and any MCP client read and write the same knowledge base in parallel.

No hosted account is required. Your Markdown files are the source of truth.

Visit **[kiwiki.xyz](https://kiwiki.xyz)** for the project website, FAQ, agent integration guides, and hosting options.

## Screenshots

![kiwiki dashboard](docs/assets/screenshots/kiwiki-dashboard.png)

![kiwiki note view](docs/assets/screenshots/kiwiki-note-view.png)

![kiwiki search](docs/assets/screenshots/kiwiki-search.png)

<p align="center">
  <img src="docs/assets/screenshots/kiwiki-mobile.png" alt="kiwiki mobile layout" width="360">
</p>

## Features

- **Multi-Agent Harness** — Central knowledge base for Claude Code, Codex, OpenCode, Cursor, ChatGPT, OpenClaw, Hermes, and any MCP-compatible agent. All agents read and write the same wiki in parallel.
- **Markdown files with YAML frontmatter** — Your file system is the source of truth. No proprietary database, no vendor lock-in, simple to back up.
- **100 % privacy** — Self-hosted on your infrastructure. No cloud, no telemetry, no vendor lock-in.
- **Per-user isolated wiki folders** under `/data/<username>/` with role-based access (`read` / `write` / `admin`).
- **SQLite FTS5 full-text search** — Search thousands of Markdown files in milliseconds, including via MCP from your AI. Special prefix `tag:<value>` filters by tag.
- **Responsive web UI** — FastAPI with Jinja2, HTMX, and Toast UI Editor. Works from 4K desktop to mobile, with WCAG 2.2 AA-oriented accessibility (see [docs/ui-accessibility.md](docs/ui-accessibility.md)).
- **Streamable HTTP MCP endpoint** at `/mcp` (legacy HTTP/SSE at `/mcp/sse`) with OAuth 2.1 authorization-code flow for ChatGPT-style connectors.
- **Docker Compose and Helm** — One command to start. Kubernetes-ready.

### Highlights in v3.0

- **Hardened authentication and sessions**: API keys are compared in constant time and are never persisted in browser sessions; stored session tokens are hashed and revoked after user or role changes.
- **Conflict-safe storage**: Atomic writes, revision checks, protected internal paths and per-user file/byte quotas protect Markdown data under concurrent agent access.
- **Hardened MCP and OAuth**: PKCE-bound signed tokens, authenticated SSE sessions, bounded queues/uploads/batches, redacted audit logs and safe Git operations.
- **Accessible responsive UI**: Native navigation semantics, stable note deep links and titles, inert mobile sidebar, visible keyboard focus, pinch zoom and a non-overlapping mobile editor.
- **Self-hosted frontend**: HTMX, Toast UI Editor and fonts are vendored with the application; pages no longer depend on third-party CDNs.
- **Production diagnostics**: `/livez` and dependency-aware `/readyz`, request IDs, latency logs and version reporting are wired into Docker, Compose and Helm.

## Quick Start

```bash
git clone https://github.com/natorus87/kiwiki.git
cd kiwiki
cp .env.example .env
# Replace every <...> placeholder in .env with a random secret first.
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
| `KIWIKI_BASE_URL` | request URL | Public base URL used in MCP and OAuth metadata; derived from the request when empty |
| `KIWIKI_LOG_LEVEL` | `INFO` | Python log level |
| `KIWIKI_TRUST_PROXY` | `false` | Trust forwarding headers and use secure cookies behind a TLS reverse proxy |
| `KIWIKI_TRUSTED_PROXY_CIDRS` | empty | Trusted proxy networks allowed to supply `X-Forwarded-For`; required when proxy trust is enabled |
| `KIWIKI_CORS_ORIGINS` | empty | Comma-separated list of allowed CORS origins; empty disables cross-origin access |
| `KIWIKI_RATE_LIMIT_ENABLED` | `true` | Enables login, OAuth, UI, read, and write rate limits |
| `KIWIKI_LOGIN_LIMIT` | `5` | `/login` brute-force attempts per minute and client IP |
| `KIWIKI_OAUTH_LIMIT` | `20` | OAuth handshake requests (authorize/token/register) per minute and client IP |
| `KIWIKI_WRITE_LIMIT` | `30` | Authenticated write requests per minute and client IP |
| `KIWIKI_UI_LIMIT` | `240` | Web UI fragment requests per minute and client IP |
| `KIWIKI_READ_LIMIT` | `60` | Authenticated read requests per minute and client IP |
| `KIWIKI_SESSION_TTL_SECONDS` | `43200` | Web-session lifetime; sessions are stored hashed and revoked after user/role changes |
| `KIWIKI_MAX_TENANT_FILES` | `10000` | Maximum Markdown files per user workspace |
| `KIWIKI_MAX_TENANT_BYTES` | `1073741824` | Maximum total Markdown bytes per user workspace |
| `KIWIKI_MAX_LIST_ITEMS` | `1000` | Maximum entries returned by one directory listing |
| `KIWIKI_MAX_RECURSIVE_LIST_ITEMS` | `10000` | Maximum entries returned by recursive listings |
| `KIWIKI_OAUTH_TOKEN_SECRET` | derived by app | Stable OAuth signing secret; explicitly required by the bundled Compose and Helm deployments |
| `KIWIKI_OAUTH_TOKEN_TTL_SECONDS` | `86400` | OAuth access-token lifetime |
| `KIWIKI_OAUTH_REFRESH_TOKEN_TTL_SECONDS` | `2592000` | OAuth refresh-token lifetime |
| `KIWIKI_OAUTH_ALLOWED_REDIRECT_HOSTS` | ChatGPT hosts | Additional comma-separated OAuth redirect hosts; HTTPS or loopback only |
| `KIWIKI_OAUTH_MAX_CODES` | `256` | Maximum pending OAuth authorization codes per process |
| `KIWIKI_OAUTH_MAX_CLIENTS` | `128` | Maximum dynamically registered OAuth clients per process |
| `KIWIKI_OAUTH_CLIENT_TTL_SECONDS` | `86400` | Inactive dynamic-client lifetime |
| `KIWIKI_OAUTH_MAX_REDIRECT_URIS` | `10` | Maximum redirect URIs per dynamic client |
| `KIWIKI_MCP_MAX_SSE_SESSIONS` | `128` | Maximum simultaneous legacy SSE sessions |
| `KIWIKI_MCP_SSE_QUEUE_MAX_MESSAGES` | `100` | Maximum queued messages per legacy SSE session |
| `KIWIKI_MCP_UPLOAD_TTL_SECONDS` | `3600` | Lifetime of incomplete staged uploads |
| `KIWIKI_MCP_MAX_UPLOAD_BYTES` | `10485760` | Maximum assembled bytes per staged upload |
| `KIWIKI_MCP_MAX_UPLOAD_CHUNKS` | `1000` | Maximum chunks per staged upload |
| `KIWIKI_MCP_MAX_STAGED_UPLOADS` | `32` | Maximum staged chunked uploads per process |
| `KIWIKI_MCP_MAX_STAGED_BYTES` | `52428800` | Maximum aggregate bytes held by staged uploads |

Example:

```env
KIWIKI_DATA_DIR=/data
KIWIKI_USERS=admin:<admin-api-key>:admin,writer:<writer-api-key>:write,reader:<reader-api-key>:read
KIWIKI_BASE_URL=https://kiwiki.example.com
KIWIKI_TRUST_PROXY=true
KIWIKI_TRUSTED_PROXY_CIDRS=172.16.0.0/12
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

## Usage as AI Memory

Configure your AI tools to use kiwiki as persistent memory. The following instruction works for ChatGPT (Custom Instructions → Personalization), Claude (Personalization), and coding agents:

> Use kiwiki as my persistent memory. When asked about projects, decisions, recurring topics, personal preferences, or work context, first briefly check kiwiki. Use existing notes as context.
>
> Save new important information in kiwiki when it might be useful later: preferences, decisions, project knowledge, workflows, important facts, and open items. Prefer to update existing files rather than creating new ones. Organize according to the existing structure: `/projects`, `/decisions`, `/notes`, `/shared` and `/user`. Write short Markdown notes with frontmatter. Do not delete anything without explicit instruction. Also ask in longer chats whether you should save something.

For coding agents (Claude Code, Codex, OpenCode, Cursor), add this instruction to your project's `AGENTS.md`, `CLAUDE.md`, or equivalent configuration file.

### Agent Harness Setup

When using kiwiki as the Agent Harness for your project, instruct coding agents to connect to kiwiki via MCP. Add the following block to your project's `AGENTS.md` or `CLAUDE.md`:

> This project uses **kiwiki** as its Agent Harness and persistent memory.
>
> **MCP connection** — Connect to the kiwiki MCP server using the appropriate command for your tool:
> - Claude Code: `claude mcp add kiwiki http://localhost:8082/mcp --header "Authorization: Bearer <api-key>"`
> - Codex: `codex mcp add kiwiki http://localhost:8082/mcp --header "Authorization: Bearer <api-key>"`
> - OpenCode: configure MCP server in opencode.json
> - Cursor: configure MCP server in `.cursor/mcp.json`
>
> Once connected, use kiwiki as persistent memory (see usage instruction above).

This ensures every coding agent working on the project automatically connects to the shared knowledge base.

## MCP Tools

The MCP server exposes tools for common wiki workflows, grouped by required role:

### Read (any role)

`read_index` · `list_files` · `read_file` · `fetch` · `search` · `read_many` · `list_all_files` · `grep` · `find` · `file_info` · `read_lines` · `recent_files` · `backlinks` · `preview_edit` · `validate_wiki` · `related_files` · `tag_index` · `search_status` · `whoami` · `file_history` · `diff` · `statistics` · `validate_links` · `link_graph` · `export` · `duplicate_check` · `ai_summarize` · `search_history` · `dead_link_check` · `grep_status`

### Write (write role or admin)

`write_file` · `append_file` · `write_many` · `chunked_write` · `create_note` · `move_file` · `edit` · `update_frontmatter` · `build_index` · `sort` · `move_folder` · `replace_many` · `upsert_note` · `reindex_all` · `git_commit` · `template` · `rename` · `batch_tag`

For autonomous agents, prefer `write_many` when updating several files and `chunked_write` when a large file or flaky client payload limit makes a single `write_file` / `append_file` call unreliable. Ordinary create, update, append, and index-refresh operations do not require confirmation; clients should ask before deleting files or running destructive reorganizations. `chunked_write` keeps temporary chunks in process memory for `KIWIKI_MCP_UPLOAD_TTL_SECONDS` (default `3600`) and limits each upload with `KIWIKI_MCP_MAX_UPLOAD_BYTES` (default `10485760`) and `KIWIKI_MCP_MAX_UPLOAD_CHUNKS` (default `1000`); aggregate staging is additionally capped by `KIWIKI_MCP_MAX_STAGED_UPLOADS` and `KIWIKI_MCP_MAX_STAGED_BYTES`.

### Admin (admin role only)

`delete_file`

The server exposes 49 tools. JSON-RPC batches are limited to 25 requests,
list-valued tool arguments to 50 entries, and OAuth authorization codes expire
after five minutes.

## Keyboard Shortcuts

| Shortcut | Action | Notes |
|---|---|---|
| `Tab` | First focus reveals the **Skip-Link**, then header → sidebar → content | Available on every page |
| `Esc` | Close mobile sidebar, search dropdown, account menu, modal, or context menu | Auto-detects open layer |
| `Ctrl/Cmd + S` | Save current note in Editor | Also works from the FAB on mobile |
| `dd` | Delete active file (admin) / folder | Pressed consecutively within 600 ms |
| `mm` | Move active item | Works on files and folders |
| `ee` | Edit active file in editor | Requires `write` role |
| `rr` | Rename active item (inline) | Works on files and folders |
| `F10 + Shift` | Open context menu for focused navigation item | Alternative to the `ContextMenu` key |
| `Enter` / `Space` on navigation item | Toggle folder or open file | Same as click |

See [docs/ui-accessibility.md](docs/ui-accessibility.md) for the full accessibility model.

## Architecture

The kiwiki frontend is intentionally framework-free — server-rendered Jinja2 templates plus HTMX for partial swaps, plus a small vanilla-JS layer (`app/static/kiwiki.js`) for interactive widgets. Application styles live in `app/static/kiwiki.css` with one `:root` token source; fonts and editor dependencies are vendored under `app/static/`. See [docs/architecture.md](docs/architecture.md) for the layout, request flow, namespaces, and helper conventions before contributing frontend changes.

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

Create a local file named `kiwiki-secrets.env` containing only
`KIWIKI_USERS` and `KIWIKI_OAUTH_TOKEN_SECRET`, then install with a
pre-created Secret:

```bash
kubectl create secret generic kiwiki-runtime-secrets \
  --from-env-file=kiwiki-secrets.env \
  --dry-run=client -o yaml | kubectl apply -f -

helm upgrade --install kiwiki ./charts/kiwiki \
  --set existingSecret=kiwiki-runtime-secrets \
  --set-string env.KIWIKI_BASE_URL="https://kiwiki.example.com"
```

The Secret must contain `KIWIKI_USERS` and `KIWIKI_OAUTH_TOKEN_SECRET`.
`secretEnv` remains available for development, but its values are stored in
Helm release metadata. An existing PVC can be reused with
`--set persistence.existingClaim=kiwiki-data`. Review `charts/kiwiki/values.yaml`
before deploying.

### Upgrading to v3.0

Existing data, user names and API keys do not need conversion. The Helm values
schema does change: `KIWIKI_USERS` and `KIWIKI_OAUTH_TOKEN_SECRET` must no
longer come from the ConfigMap-backed `env` block. Preserve the existing users,
API keys and explicit OAuth signing secret, then create a Kubernetes Secret
before the upgrade:

```bash
kubectl create secret generic kiwiki-runtime-secrets \
  --from-env-file=kiwiki-secrets.env \
  --dry-run=client -o yaml | kubectl apply -f -

helm upgrade --install kiwiki ./charts/kiwiki \
  --set existingSecret=kiwiki-runtime-secrets \
  --set persistence.existingClaim=kiwiki-data
```

Remove the old sensitive `env.KIWIKI_USERS` and
`env.KIWIKI_OAUTH_TOKEN_SECRET` values from the values file used for the
upgrade, and avoid `--reuse-values` until they have been removed. Do not pass
secrets through `--set` in production because Helm stores release values. If
the old deployment had no explicit OAuth secret, create a stable one now;
existing OAuth access and refresh tokens will be invalidated once during that
change. Existing pods keep working until replaced, but the v3 chart will reject
an upgrade that supplies neither `existingSecret` nor `secretEnv`. Previous
Helm release revisions may still contain the former ConfigMap values and should
be handled according to your cluster's secret-retention policy.

## Repository Hygiene

The repository includes:

- GitHub Actions CI for Ruff, branch coverage (minimum 60%), dependency audits,
  Chromium smoke tests and a container readiness check
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
- Configure `KIWIKI_TRUSTED_PROXY_CIDRS`; forwarding headers from all other peers are ignored.
- Restrict `KIWIKI_CORS_ORIGINS` in production.
- Set `KIWIKI_OAUTH_TOKEN_SECRET` for public MCP/OAuth deployments.
- Do not publish local wiki data or deployment secrets.

## License

MIT. See [LICENSE](LICENSE).
