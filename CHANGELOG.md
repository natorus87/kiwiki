# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
This changelog is written in English only.

## [Unreleased]

### Changed
- **New brand color: honey amber (`#e3a94f`) replaces Kiwi Green.** Primary actions, focus rings,
  links, the wordmark and the Neural Atlas now use amber so kiwiki is visually distinct from the
  similar KiwiFS project. Tag nodes and the admin role pill moved to a cool blue (`--md-tertiary`,
  formerly `--md-warm`) to stay distinguishable from the new primary. Static asset versions were
  bumped so browsers pick up the new theme.

## [4.1.0] - 2026-09-29

### Upgrade notes
- **Refresh tokens are rotated.** `grant_type=refresh_token` now also returns a new
  `refresh_token`; the redeemed one is consumed (`invalid_grant` on reuse). Spec-compliant OAuth
  clients (ChatGPT, Claude) pick up the new token automatically.
- **Bearer headers no longer open UI routes.** Only `POST /` (JSON-RPC) accepts a bearer token
  without a session; `/ui/*`, `/editor` and `/settings` require the browser session. REST (`/api/*`)
  and MCP (`/mcp`) are unchanged.
- **Raw API-key cookies are deprecated.** They keep working but log a warning; please switch to
  `Authorization: Bearer`.

### Security
- **A bearer header bypassed the session check on every UI route** — the WebAuthMiddleware let any
  request carrying `Authorization: Bearer <anything>` through to `/editor`, `/ui/*` and `/settings`
  unchecked; data only stayed private because the namespace was missing. The bypass now applies to
  `POST /` (JSON-RPC connectors) only, and `/ui/export` additionally requires an authenticated user.
- **Note content could set UI control classes** — `class` on `<a>` was allowed freely (`kw-file-link`,
  `btn btn-danger` …): app-styled buttons inside notes and global click handlers firing without data.
  nh3 now filters classes through an allowlist (`<a>`: only `wikilink`/`missing`, `<code>`: only
  `language-*`), including the HTML export.
- **Session cookie without `Secure` on direct TLS** — the flag depended on `KIWIKI_TRUST_PROXY` only;
  it now also follows the `https` request scheme.
- **Legacy cookie auth with a raw API key** logs a deprecation warning (once per user and process);
  the limits of the in-memory refresh-replay protection are documented in `SECURITY.md`.
- **The OAuth registration counter grew without bound** — only the requesting IP was pruned. It is now
  pruned globally and capped by `KIWIKI_OAUTH_MAX_REGISTER_SOURCES` (4096).

### Fixed
- **MCP `list_all_files` violated its outputSchema after a dashboard request** — the dashboard enriched
  the cached entries with `excerpt`; strict clients rejected the whole response for up to 5 s.
  `list_all_files` now returns copies.
- **Wikilinks in `~~~` blocks and inline code** turned into visible, escaped `<a …>` text; both now stay
  literal. The duplicate-title check ignores leading horizontal rules and headings inside code blocks.
- **The editor preview did not resolve wikilinks relative to the source note** — it now matches the
  server (verified by a test against `_resolve_wikilink`).
- **The editor preview was empty apart from wikilinks and code** — the custom text-node renderer
  returned `undefined`, which ToastUI 3 treats as "render nothing". It now returns `context.origin()`;
  the browser smoke test checks heading, paragraph, table cell and task text in the preview.
- **Note view without a visible main heading** — the server (`title_redundant`) and CSS
  (`h1:first-child { display: none }`) together hid both titles. The CSS rule is gone; when the title
  differs, body `h1` elements are demoted to `h2`, so exactly one `h1` remains.
- **Invisible selection checkboxes were tab stops** — every tree entry cost two tabs. Outside select
  mode they now carry `tabindex="-1"`.
- **Escape collapsed the desktop sidebar** instead of closing open search results. Search results take
  precedence; Escape now only closes the mobile overlay.
- **Excerpts showed raw wikilink syntax** (`[[../adr-001-sqliteADR 001]]`) and repeated the title.
  Wikilinks become their label or file stem, and a leading heading equal to the title is dropped — in
  the dashboard and in search results.
- **Task lists** (`- [ ]`/`- [x]`) render as disabled checkboxes in the note view, matching the editor
  preview. The checkbox is inserted after nh3; note content still cannot inject an `<input>`.
- **Knowledge page with the engine turned off** showed "0 nodes / 100 % depth" and a rebuild button that
  could only fail. It now explains how to turn the engine on.
- **Directly opened note URLs showed an unstyled fragment page** — `/ui/file` returns an HTMX partial
  without layout. Browser navigation (deep link, shared link, reload — detected by a missing
  `HX-Request` header plus a `text/html` Accept) is now redirected with 307 to `/?file=`, which loads the
  same note in full. Machine clients without `text/html` still get the fragment, so citation URLs remain
  fetchable.
- **`[[Wikilinks]]` are clickable** — they used to be dead text in the note view and the editor preview.
  Server-side (`_render_markdown_safe`) and in the ToastUI preview (`customHTMLRenderer`) they resolve
  by Obsidian convention (relative to the folder, `.md` appended); missing targets carry the `missing`
  class and a dashed underline, code blocks and inline code stay literal.
- **Frontmatter no longer leaks into the editor preview** — the `---` block, title and tags rendered as a
  horizontal rule, heading and body text. Text nodes inside the frontmatter range are suppressed and empty
  shells (rule, empty leading blocks) are removed by an observer; the source is untouched.
- **"Recently created" reported "no note" although notes existed** — the filter required a frontmatter
  `created`. The fallback is now the filesystem time (birthtime, else ctime, else mtime); `updated`
  without a stamp falls back to mtime.
- **Every timestamp read "Today, 00:00"** — writes stored only the date. `created`/`updated` now include
  the time (UTC, minute precision); the display still converts to local time.
- **Search fields name their scope** — the top bar (`Suchen…`) and the sidebar (`Filtern…`) were two
  magnifying glasses without a promise. Placeholders now carry verb and target ("Search notes…" /
  "Filter file tree…", German equivalents).
- **Dashboard lists show a preview and tags** — previously only title and date, i.e. clicking blind.
  Every row now has an excerpt (first ~120 characters without frontmatter/Markdown syntax, files over
  100 KB excluded) and up to 3 tags.
- **The mobile CTA row fits at 375 px** — three buttons edge to edge are gone: only `+ New note` stays
  full width, Tags/History move into a `···` menu (native `<details>`, closes on selection). Section
  labels use tighter tracking on mobile (0.18em → 0.06em).
- **The start page uses its empty half** — a new tag cloud (`/ui/tags?compact=1`, top 12 chips, no
  breadcrumb or lists); without tags the section stays hidden.
- **Duplicate H1 in the note view** — when the frontmatter title equals the first body H1, the view
  heading is hidden (breadcrumb and metadata remain); different titles are shown as before.
- **Internal `.kiwiki` files were readable through read APIs** — `validate_content_read_path()` existed
  but was only called by `read_lines`/`file_info`. `read_file`, `fetch` and `read_many` (and with them
  `/api/file`, `/ui/file` and the editor) still served `.kiwiki/agent_log.jsonl`. The guard now sits
  centrally in `read_file()` and `_read_frontmatter_only()`; writing was already blocked and stays so.
- **MCP and UI search had no query limit** — only REST capped at 512 characters via `SearchRequest`.
  `search()` now truncates server-side to `MAX_QUERY_LENGTH`, and the `search` tool schema declares
  `maxLength: 512`. An unbounded megabyte query ran through FTS sanitising and the LIKE fallback and
  burned CPU.
- **OAuth refresh tokens rotate** — previously a refresh token stayed reusable for 30 days. Every
  successful redemption returns a new refresh token and consumes the presented one (reuse →
  `invalid_grant`, in memory like codes and DCR clients). Failed client/resource binding checks
  deliberately do not consume the token.
- **Open DCR registration is limited per IP** — 128 slots with a 24 h TTL could be filled from a single
  source, after which legitimate clients got 503. Now at most 16 registrations per source IP and hour
  (`KIWIKI_OAUTH_MAX_REGISTER_PER_IP`); only successful registrations count.
- **Unknown MCP methods return HTTP 200** — `-32601` was delivered as HTTP 404 and made the server look
  broken. Method-not-found is a protocol error, not a transport error, and belongs in the JSON-RPC error
  body with HTTP 200.
- **Stricter MCP schemas for OpenAI clients** — `fetch` requires either `id` (OpenAI contract) or `path`
  (alias for direct callers) via `anyOf` instead of nothing; without `KIWIKI_BASE_URL` the startup logs a
  warning, because citation URLs would be relative and unusable for OpenAI connectors.
- **Dev dependency `httpx2` raised to 2.13.1** — 6 known CVEs in 2.7.0 (WSS over SOCKS without TLS, SSE
  ReDoS, multipart header injection, decompression bomb, request smuggling). Dev scope only; neither
  `app/` nor `tests/` import the package, production was not reachable.
- **Live search never fired** — `hx-trigger` sat on the search form with the `changed` modifier. htmx
  compares `elt.value` of the trigger element for that, and a `<form>` has none: the comparison was
  always `undefined === undefined` and discarded every input and submit event. Typing showed nothing,
  Enter showed nothing, and a previously visible result list stayed up after the query changed — i.e.
  results for a different query.
- **The tag overview only saw the root folder** — `/ui/tags` read the top level only via
  `list_files(".")` and reported "No tags yet" as soon as notes lived in folders. It now uses
  `list_all_files(".")`, which is recursive and already returns the tags.
- **The file header overflowed the screen on narrow devices** — `.file-header` is a column flex with
  `flex-wrap: wrap` there; width is the cross axis and grows to `max-content`. A long path pushed the
  header, including the copy button, out of view.
- **Dashboard panels clipped the timestamp** — grid children have `min-width: auto`; a long note title
  stretched the panel past the edge.
- **The hero stayed at full display size** — `index.html` loads its `<style>` after `kiwiki-polish.css`,
  so the hero rules there lost at equal specificity: the title stayed at 3.8rem and the grid kept the
  empty second column of the removed status panel.
- **Markdown tables had an oversized border** — `display: block` with `width: 100%` drew the border
  around the full column width while the cells only filled their content.
- **`__import__("os")` in the indexer hot path** — `app/knowledge/indexer.py` read the file size limit
  through a dynamic import. It is now a normal `import os`; the key is visible to the documentation
  check as well.

### Performance
- `users.yaml` is only re-parsed when it changes (stat signature) instead of several times per request.

### Changed
- **Start page for populated workspaces** — the introduction (large logo, explanatory text) is reserved
  for empty workspaces; otherwise a compact "Pick up where you left off" header with the actions.
  "Recently edited" and "Recently created" are one list with a toggle instead of two largely identical
  columns; the tag cloud sits next to it in the first viewport.
- **Tags view as a dense list** — one tag per row with its count and the notes visible directly,
  instead of a card grid with a "1 file" disclosure.
- **Heading structure** — Tags, Search history and the editor have an `h1` (visually hidden in the
  editor, where the path field provides the context).
- **Settings** show the file tree instead of an empty sidebar column.
- **Mobile note actions** — Export and Delete live in the `···` menu next to Edit instead of Delete
  wrapping alone onto a second line.
- **The login hint addresses users instead of operators** (no more `user:key:role`).
- Copy icon next to the path with a pointer target of at least 24 px.
- **One measurement system instead of two competing ones** — the stylesheets carried 37 font sizes and
  58 spacing values, 71 % of the spacings fell between steps. Both now run on tokens: seven type roles
  plus display, spacings as multiples of 4 (plus 2 px for hairline gaps). Font weights are limited to
  400/500/600/700 — 620 and 650 were artefacts of a variable font. `tests/test_design_tokens.py` fails on
  raw values from now on.
- **The start page helps on first launch instead of collapsing the help** — a fresh workspace showed two
  empty panels with the same sentence and a collapsed "Getting started". The help block stays open as
  long as no note exists, and each panel has its own empty text.
- **Timestamps are readable** — instead of `2026-09-19T17:42:00`, lists and the file header show "Today,
  09:31", "Yesterday, 18:25", "3 days ago" or "19 Sep"; the full timestamp stays in the `title` of the
  `<time>` element.
- **The file tree starts open and remembers its state** — it is the main navigation and was collapsed
  again after every page change. Open/closed now lives in the `kiwiki_sidebar` cookie so the server
  renders it correctly right away.
- **The start page shows the work first** — the hero is more compact, the status panel with its three
  unchanging facts (`.md`, `FTS5`, role) is gone, and "Getting started" including the MCP access details
  is complete but collapsed below the note lists.
- **Search results lead with the title** — below it the path, below that an excerpt around the match
  instead of the first 200 raw characters of the file. Frontmatter, heading markers, table pipes and link
  targets are stripped.
- **The reading view names the path once instead of three times** — breadcrumb, title and path chip said
  the same thing. The breadcrumb now ends at the folder; the metadata line carries path, time and author.
- **Edit is the primary action** — in the reading view and the editor, Delete was the most prominent
  button (on phones even the widest). It keeps its label, sits apart and only turns red on hover/focus.
- **Tag overview and search history are styled** — both views had no CSS of their own and no visible way
  back; both now share the reading view's header with a breadcrumb.
- **Settings no longer waste an empty column** — the sidebar there only carried a Back button that
  already existed in the page header.
- **`Ctrl+S` sits on the Save button** — previously a loose label at the end of the editor bar.
- **The configuration docs stay in sync with the code** — `tests/test_deployment_config.py` now checks
  that every `KIWIKI_*` variable read by the code appears in the README table, that `.env.example`
  names no invented keys, that `values.yaml` exposes the UI and session limits and that `AGENTS.md`
  distinguishes versioned from local paths. `.env.example` lists the operationally relevant variables
  actively and the rest commented with their default in brackets; the complete list stays in the
  README.
- **`AGENTS.md` names the paths that exist** — the file pointed to `.Codex/rules/`, `.Codex/skills/` and
  `.Codex/agents/`. The real location is `.claude/`. It now also notes that `.claude/` is excluded by
  `.gitignore`, so a fresh clone has no rules, agents or skills, and lists the quality gates that must
  be green before anything is reported as done.
- **`APP_VERSION` is part of the release consistency test** — `test_release_version_is_consistent`
  checked `pyproject.toml`, `Chart.yaml` and `values.yaml`, but not the constant that is shipped as the
  version in FastAPI, `/version` and the MCP `serverInfo`.
- **Documented environment variables** — `KIWIKI_KEY_ATTEMPT_LIMIT` (the brute-force counter kept
  separately for the `/oauth/authorize` form) and `KIWIKI_KNOWLEDGE_MIN_FREE_BYTES` (the free-space check
  before writing the knowledge index) were missing from the README, `.env.example` and Helm values.
  Likewise, `KIWIKI_MCP_MAX_STAGED_UPLOADS` and `KIWIKI_MCP_MAX_STAGED_BYTES` sat after the knowledge
  paragraph instead of in the configuration table, where no tool picked them up.
- **After five failed attempts the correct API key was locked out** — the `login` rate-limit layer
  counted every `POST /login` permanently, successful ones included. From the sixth attempt on the
  middleware answered 429 before `login_submit` could run and release the budget: anyone who had
  mistyped twice could not get in for 60 seconds, even with the right key. The login path now runs
  through and decides by the response — only a redirect after a successful check releases the window,
  a 401 does not. The OAuth form behaves as before (`/oauth/authorize` is in the `oauth` tier and uses
  `KIWIKI_KEY_ATTEMPT_LIMIT`).
- **Search history swallowed its errors** — `record_search` and the prune step at the end of `search()`
  caught every error with `pass`. A user saw an empty history and could not tell whether search was
  broken or had found nothing. Both paths now log; the same applies to closing the connection pools and
  checking the search index schema.
- **Two dashboard panels reported errors as "empty"** — the fragments for "recently edited" and "search
  history" returned an empty response on any error. On the dashboard, empty means "nothing there", so
  an error was indistinguishable from an empty workspace.
- **The browser smoke test was a silent gate** — it printed nothing on success, so CI showed an empty
  line and could not tell "all green" from "nothing ran".
- **The coverage threshold was 19 points below reality** — `pyproject.toml` required 60 %, actual
  coverage was 79 %. A gate that easy to meet does not protect anything. The threshold is now 75 % in
  both files, and a test keeps `pyproject.toml` and `ci.yml` from drifting apart.

## [4.0.0] - 2026-09-18

### Security
- **File history stays inside the user's namespace** — `/ui/history` now validates the `path` parameter
  with the same check as the MCP tools. Previously a path like `../<other-user>/notes/x.md` reached
  `git log` unfiltered; if a repository existed above the user directory, the view exposed other users'
  commit metadata.
- **Background greps are bound to their owner** — `grep_status` only returns results to the user who
  started the job. Foreign job IDs behave like unknown ones.
- **Failed attempts on the OAuth form have their own budget** — `POST /oauth/authorize` checks the same
  API key as `/login` but sits in the more generous `oauth` tier. Failed entries now count against a
  separate limit (`KIWIKI_KEY_ATTEMPT_LIMIT`, default 5/minute) without throttling the connector
  handshake.
- **Internal files are blocked for reading too** — `find`, `read_lines` and `file_info` exclude
  `.kiwiki`, matching the existing write block.

### Changed
- **Knowledge tools declare their response** — `entity_details`, `entity_neighbors`, `fact_timeline`,
  `explain_relation` and `knowledge_reindex` used `{"type": "object", "additionalProperties": true}` and
  thus declared nothing. The schemas now name fields, types, ranges (`depth` 1–3, `confidence` 0–1) and
  allowed `status` values. `entity` and `relation` are explicitly nullable: `null` means "looked, found
  nothing", a missing field means "knowledge engine off".
- **MCP now negotiates revision 2025-06-18** — `outputSchema` and `structuredContent` only belong to the
  specification from this revision on. kiwiki shipped both but announced `2025-03-26` in the handshake;
  a client aligning its tool model with the negotiated revision saw unknown fields. Clients requesting
  `2024-11-05` or `2025-03-26` still get exactly that revision; a newer request (e.g. `2025-11-25`) is
  answered with `2025-06-18`.
- **`fetch` returns list metadata readably** — `tags: [python, mcp]` appears as `"python, mcp"` instead
  of the Python representation `"['python', 'mcp']"`.
- **BREAKING: `search` and `fetch` follow the OpenAI connector contract** — `search` returns
  `{"results": [{"id", "title", "text", "url"}]}`, `fetch` returns `{"id", "title", "text", "url",
  "metadata"}`. The `id` is the note path and can be passed to `fetch` unchanged; `url` cites
  `/ui/file` via `KIWIKI_BASE_URL`. Without a base URL the link stays relative, because no request is
  available in the tool dispatcher. `read_file` keeps its previous format and is not affected.
- **BREAKING: list tools return an object instead of an array** — `list_files`, `sort`,
  `list_all_files`, `recent_files`, `tag_index` and `search_history` now return their results under the
  `items` key (`{"items": [...]}`). The MCP specification only allows objects for `outputSchema` and
  `structuredContent`; strictly validating clients rejected the previous array responses. Integrations
  that parse `content[0].text` directly as an array need to be adjusted.

### Fixed
- **Free-form frontmatter values respect the tool schemas** — `title: 2026` and `tags: python` are valid
  YAML and yield an int and a scalar. `list_all_files` and `recent_files` passed them through unchecked
  and violated their own `outputSchema`; a validating client then rejected the whole response. A scalar
  tag was also silently dropped instead of being read as a one-element list.
- **`batch_tag` no longer splits scalar tags into single letters** — `list("python")` produced six tags
  (`p`, `y`, `t`, …) and wrote them back to the note. Every note with a scalar `tags:` was affected.
- **`template` declares the `template_type` field it returns** — the tool shared `_STATUS_SCHEMA` with
  six others, which forbade any further field via `additionalProperties: false`.
- **`NaN` and `Infinity` in frontmatter no longer break the response** — `score: .nan` ended up as a bare
  JSON literal in the output. RFC 8259 knows neither; strict parsers failed on the entire response.
- **`ping` is answered** — the MCP specification requires an immediate empty response in every
  revision. kiwiki answered `-32601 Method not found` instead, delivered as HTTP 404; clients using
  `ping` as keepalive dropped the session.
- **Unquoted dates in frontmatter no longer break the tools** — YAML reads `created: 2026-01-01` as a
  `datetime.date`. That value reached `json.dumps()` and sorting and made `read_file`, `read_many`,
  `list_files`, `list_all_files`, `recent_files` and `statistics` fail with an internal error. The
  server instructions explicitly ask for `created`/`updated`, and `write_file` wrote the date back
  unquoted — the server produced the unreadable note itself. Frontmatter is now normalised to
  JSON-compatible types during parsing, for the read and the write path alike.
- **`list_all_files` respects its own `outputSchema`** — the response contained `created`, while the
  schema forbade any further field via `additionalProperties: false`. Strictly validating clients
  rejected the result.
- **`grep_status` no longer returns `null` for `result`** — the field is optional and is omitted for
  `not_found` and `running` instead of violating the object type declared in the schema.
- **`resources/templates/list` answers with an empty list** — kiwiki offers no URI templates, but
  clients query them during discovery anyway. The previous `-32601` answer came back as HTTP 404.
- **Notes with a duplicate tag no longer break the knowledge index** — frontmatter lists are
  deduplicated before indexing. Previously `tags: [python, python]` produced two relations with the
  same primary key; the document stayed unindexed after three failed attempts and the tenant status
  reported `degraded`.
- **Saving during indexing is no longer lost** — a job is only completed if it is actually still
  running. If a file was saved again in the meantime, completion used to discard the queued revision,
  and the knowledge index stayed stale until restart.
- **Search recovers from a removed index** — `init_db()` detects a vanished database and recreates
  tables and connections; the LIKE fallback catches SQLite errors just like the FTS path. Previously a
  user's search stayed broken until process restart after a workspace rollback.
- **Sessions follow the configured data directory** — the location of `sessions.json` is resolved at
  runtime instead of being frozen at import, and loading from disk runs entirely under the lock.
- **Export copes with commas in file names** — the selection is sent as one form field per path;
  previously `notes/Meeting, Q4.md` fell apart into two unusable fragments and was silently missing
  from the result.
- **`template` reports invalid input** — an unknown `template_type` and a title without usable
  characters now produce a clear error instead of an empty note or `-.md`.
- **Background greps stay referenced** — the task is held on to, so it is not garbage-collected mid-run
  and the job does not stay at `running` forever.

## [3.2.0] - 2026-08-08

### Added
- **Central German/English product UI** — a shared language resolution honours an explicit choice, a
  persisted cookie and `Accept-Language`. Pages, HTMX fragments, browser dialogs, toasts, ARIA texts,
  the editor and user management use the same complete DE/EN catalogue.
- **Kiwiki UI system** — a lean, self-hosted polish layer bundles spacing, control heights, focus
  states, layers, radii and responsive touch targets, and unifies the dashboard, explorer, editor,
  settings, login and Neural Atlas visually.

### Changed
- **Calmer information hierarchy** — primary actions, form fields, sidebar navigation and content areas
  follow a consistent editorial workspace language with stable states instead of decorative hover
  motion.

### Fixed
- **Write users can fully maintain their own knowledge space** — `delete_file` and `knowledge_reindex`
  now only require the `write` role. The same rule applies to REST endpoints, context menus, batch
  delete, the editor and the Atlas UI; user management stays `admin` only.
- **Neural Atlas action buttons stay aligned on touch devices** — Reset and motion controls no longer move vertically
  when mobile browsers retain a sticky hover state after tapping. Their circular boxes and SVG icons now use explicit,
  identical dimensions so both controls remain level in German and English layouts.
- **Sidebar filter icon stays inside the input** — The search icon is now positioned within the filter field on
  desktop and mobile instead of consuming a separate toolbar column.

### Tests
- **Localisation and UI contracts** — new server-side and static regression tests cover both languages,
  the persisted language choice, identical catalogue keys, localised error fragments, cache busting and
  the focus, touch, viewport and reduced-motion basics. The browser smoke test covers the English core
  flow.

## [3.1.1] - 2026-08-03

### Fixed
- **Desktop sidebar menus remain clickable after restoring a custom width** — A saved resizer width no longer
  overrides the zero-width `collapsed` state while the sidebar is still `inert`. Desktop width restoration now only
  happens after opening, invalid values are bounded, closing also closes the account menu, and pointer loss or window
  blur reliably ends resizing without leaving the interface in a dragging state. Desktop/mobile breakpoint changes
  now synchronize the hamburger, backdrop, accessibility state and any open account or context menu.
- **Mobile Neural Atlas remains visible and supports pinch zoom** — Large graphs no longer expand out of the camera
  after loading. Repulsion scales with the total node count; large layouts use a bounded linear simulation; and spring
  forces are normalized by distance and node degree so highly connected notes or tags cannot corrupt coordinates.
  Velocity and radius guards recover non-finite nodes, reset restores both layout and fitted camera, and Safari resumes
  rendering after back/forward-cache navigation. The canvas tracks two touch pointers for bounded pinch-to-zoom and
  German and English instructions describe both mouse-wheel and touch operation.

### Tests
- **Desktop menu interaction regression coverage** — The real-browser suite restores a custom sidebar width, verifies
  the collapsed/open geometry and accessibility state, repeatedly toggles the account menu, hit-tests its Knowledge
  Graph link, exercises the desktop right-click context menu and verifies menu cleanup across breakpoint changes and
  interrupted resize gestures.
- **Large mobile graph regression coverage** — Contract, Chromium and local WebKit checks load 500 nodes with a
  degree-424 hub, wait through 120 animation frames, enforce a frame-time budget, reject page errors, verify visible
  graph pixels, exercise pinch zoom in both directions, confirm reset to 100 percent and cover Safari canvas resume.

## [3.1.0] - 2026-08-03

### Added
- **Upgrade-safe native Knowledge Engine (opt-in)** — A deterministic per-tenant SQLite index derives entities,
  relationships and Markdown link provenance without rewriting source notes. Schema migrations, persistent
  idempotent jobs, bounded startup backfill, mutation hooks, REST endpoints and seven MCP tools are guarded by
  `KIWIKI_KNOWLEDGE_ENABLED=false` for rollback-safe upgrades of running instances.
- **Interactive neural knowledge atlas** — The authenticated `/knowledge` page renders each tenant's documents,
  tags and relationships as a self-hosted spatial canvas with orbit/zoom controls, node inspection, source-note
  navigation, neighborhood focus, reduced-motion support, German/English localization and bounded graph payloads.

### Fixed
- **Reliable multi-note deletion** — The explorer now deletes selected notes through one bounded batch request instead
  of consuming one write-rate-limit slot per note. File deletion and search-index cleanup share path locks, bulk
  deindexing uses one short-timeout transaction outside the event loop, mixed folder selections no longer poison the
  note batch, and stale index revisions are excluded from search results.
- **Web explorer no longer appears frozen after rate limiting** — HTMX file and folder requests now use a dedicated
  `ui` tier (`KIWIKI_UI_LIMIT`, default 240/min) instead of sharing the lower API/MCP read budget. HTTP 429 responses
  produce a visible retry toast, failed note navigation keeps the current URL and content, and failed folder loads
  roll back their open/persisted state. Request-specific HTMX sources prevent rapid clicks from confirming queued
  requests before their response, including during persisted tree restoration.
- **MCP OAuth redirect fallback** — Unknown or expired DCR client registrations (the in-memory registry doesn't survive restarts or its 24h TTL) now fall back to the redirect-host whitelist instead of hard-rejecting with `invalid_redirect_uri`, matching the existing CIMD-client behavior. Fixes ChatGPT connector re-authorization breaking after a container restart.
- **OAuth handshake rate-limit tier** — `/oauth/authorize`, `/oauth/token` and `/oauth/register` now share their own `oauth` tier (`KIWIKI_OAUTH_LIMIT`, default 20/min) instead of the 5/min `/login` brute-force tier. A single connector setup (form submit, retry, token exchange, refresh) could previously exhaust the shared login limit and silently 429 with no visible feedback on the plain HTML authorize form; that form now also gets a readable HTML error page instead of a raw JSON body when rate-limited.
- **OAuth consent form CSP form-action** — `form-action 'self'` in the global CSP also governs the redirect target after a form submission, not just the initial submit URL. This silently blocked the browser from following the 302 to `chatgpt.com` after submitting the `/oauth/authorize` consent form — clicking "Autorisieren" appeared to do nothing, with the actual block only visible in the browser console. The authorize page now sets its own CSP with a `form-action` exception scoped to the one, already-validated `redirect_uri` origin.

### Tests
- **Batch-delete regression coverage** — API, rate-limit, authorization, request-size, SQLite-lock, stale-index and
  real-browser tests cover deleting 35 selected notes with exactly one request.
- **Explorer rate-limit regression coverage** — Browser smoke checks now cover successful nested navigation, visible
  429 feedback, URL/content preservation, folder rollback, rapid competing clicks and failed persisted-folder restore.
  Unit tests keep UI, API-read and write rate-limit tiers independent.

## [3.0.0] - 2026-07-14

### Migration
- **Kubernetes secrets are no longer accepted from the ConfigMap values block.** Move `KIWIKI_USERS` and `KIWIKI_OAUTH_TOKEN_SECRET` to a pre-created Secret referenced through `existingSecret`, or use `secretEnv` for non-production installs. Keep the existing OAuth signing secret to preserve issued connector tokens.
- **Docker Compose requires explicit secrets.** Set both `KIWIKI_USERS` and `KIWIKI_OAUTH_TOKEN_SECRET` in `.env` before starting the stack.

### Added
- **Production health and observability** — `/livez`, dependency-aware `/readyz`, Docker/Compose/Helm health checks, request correlation IDs, latency logs and release version reporting.
- **Real browser regression gate** — Chromium smoke test for mobile zoom, sidebar focus/inert state, note deep links, dynamic titles, responsive settings and horizontal overflow.
- **Capacity controls** — request, tenant file/byte, list, JSON-RPC batch, SSE session/queue, OAuth state and staged-upload limits.
- **`PATCH /api/file/frontmatter`** — New REST endpoint that merges individual frontmatter fields (e.g. `tags`) server-side via `storage.update_frontmatter()` without touching content or other metadata. Replaces the previous destructive client-side regex merging in `kwBatchTag()`.

### Fixed
- **Security and data integrity review** — UI role checks, API-key-free hashed sessions, session revocation, central storage policy, transactional user-workspace rollback, atomic conflict-aware writes, search deindexing and thread-local SQLite connections.
- **MCP/OAuth hardening** — PKCE token binding, authenticated SSE message sessions, bounded in-memory state, redacted `0600` audit logs, safe Git arguments/timeouts and working asynchronous grep polling.
- **Mobile and accessibility regressions** — editor overlap, hidden-sidebar tab stops, focus visibility, pinch zoom, settings grid, incomplete ARIA tree pattern, unnamed editor controls and broken tags/search-history navigation.
- **Persistent breadcrumb XSS and external CDN exposure** — file paths no longer enter inline JavaScript; htmx, Toast UI and fonts are vendored locally and CSP is self-hosted only.
- **`kwBatchTag()` overwrote frontmatter (data loss)** — batch tagging via multi-select rebuilt the entire frontmatter block with a regex and lost `title`/`created`/`updated` in the process. It is now merged server-side via `PATCH /api/file/frontmatter`.
- **`GET /api/file` and `GET /api/files` failed on every call** — both endpoints passed Pydantic models (`FileContent`/`FileInfo`) directly to `starlette.responses.JSONResponse()`, which cannot serialise them. Every successful read ended in a 400 "Object of type … is not JSON serializable". Found during the manual end-to-end test of the frontmatter fix — it also affected `kwBatchTag()`, which needs this endpoint to read existing tags.
- **The frontmatter cache (A3) was inactive** — `_fm_cache` had existed since the A3 performance work, including its lock, but `_read_frontmatter_only()` never read or wrote it. It is now actually wired up with a `(path, mtime)` key.
- **The test suite aborted during collection** — union return type annotations (`HTMLResponse | RedirectResponse`) on `/login` made FastAPI crash at import with `FastAPIError: Invalid args for response field`. Fix: `response_model=None` on the affected routes.
- **Internal exception details visible in the UI** — several `/ui/*` handlers returned `str(exc)` raw into the rendered HTML, including for unexpected (not deliberately raised) exceptions that could contain internal paths or tracebacks. Controlled validation errors (`ValueError`/`FileNotFoundError`) remain visible; everything else is logged and replaced by a generic message.
- Silent `except Exception` blocks in the dashboard panels (`/ui/recent-edited`, `/ui/recent-created`) and in frontmatter parsing (`storage._read_frontmatter_only`) now log the error instead of swallowing it.

### Changed
- **Deployment defaults hardened** — Compose binds loopback and requires secrets; Helm supports existing Secrets/PVCs, enforces one replica, uses read-only root filesystem/seccomp and disables broad CORS defaults.
- **Reproducible delivery** — exact direct dependency pins, digest-pinned base images, unified version `3.0.0`, CI coverage/security/browser gates, and release SBOM/provenance.
- **Session cookie: `SameSite=Lax` → `SameSite=Strict`** — closes CSRF via the state-changing `/ui/*` POST endpoints (rename, export, search); kiwiki has no legitimate cross-site entry point (no login links from e-mails or similar).
- **Constant-time API key comparison** — bearer token and login checks now use `secrets.compare_digest()` across all configured keys instead of a dict lookup that can short-circuit on the first hash bucket hit.
- **`init_db()` checks the FTS5 schema only once per process and namespace** instead of re-running the `sqlite_master` query and `CREATE TABLE IF NOT EXISTS` statements on every `search()`/`index_file()`/reindex call.
- **Session persistence throttled** — sliding-expiration renewal used to rewrite the whole `sessions.json` on every authenticated request; now only when `expires_at` has moved by more than 60 s.
- **`@app.on_event("startup"/"shutdown")` → `lifespan` context manager** — fixes the DeprecationWarning on every test run; behaviour unchanged.
- Removed dead code `getKey()` in `kiwiki.js` (never called; pretended to retrieve the API key but only ran a `localStorage.removeItem`).
- **Start page layout order** — the dashboard panels ("Recently edited" / "Recently created") now appear below the kiwiki hero block instead of above it. Focus is on branding/tagline first, then on recent activity.

### Tests
- 221 tests pass with 64% branch coverage, including new regression tests for auth/session revocation, quotas, atomic writes, OAuth/MCP limits, browser accessibility, deployment configuration and health probes.

## [2.6.0] - 2026-07-06

### Added
- Collapsible desktop sidebar with an animated menu button and a draggable, locally persisted width.

### Fixed
- Removed the unintended green menu-button glow, cleared stale inline widths after resizing and limited the backdrop to mobile layouts.

## [2.5.0] - 2026-07-03

### Added
- **Dashboard on the start page** — two panels "Recently edited" and "Recently created" at the top of the start page (up to 8 files each, searched recursively). Gives quick access to recent work. The hero section with its info panel moved below.
- **`/ui/recent-edited`** — new HTMX endpoint: files sorted by frontmatter `updated` (recursive).
- **`/ui/recent-created`** — new HTMX endpoint: files sorted by frontmatter `created` (recursive).
- **`created` field in `list_all_files`** — `storage.py` now also returns the `created` frontmatter field for all files.

### Fixed
- **Touch swipe gesture completely dead on iPad** — two critical bugs: (1) `kiwiki.js` was loaded before the DOM (script in `layout.html` line 32, sidebar element only at line 58), so the IIFE found `null` and bound no listeners. Fix: `DOMContentLoaded` wrapper plus `querySelector` inside `bind()`. (2) The `max-width: 768px` breakpoint excluded every iPad (810–1024px). Fix: all breakpoints extended to `1024px` (CSS and JS).
- **Frequent logouts** — the session store was purely in memory; every container restart lost all sessions. Sessions used `time.monotonic()` (reset on restart) and had no sliding expiration (expiry after 12 h regardless of activity). Fix: JSON file persistence (`/data/sessions.json`), `time.time()`, sliding expiration on every access.

### Changed
- **Touch swipe gesture** for the sidebar: swipe right to open now works from the **entire content area** (no longer only from the left screen edge). Swipe left to close works from anywhere on the sidebar. The `edgeZone` threshold was removed and `openThreshold` set to 60px. The handler is always bound (no viewport check at load time any more).

## [2.2.0] - 2026-07-01

### Added — Accessibility
- **`<main>` landmark and skip link** ("Zum Inhalt springen") for keyboard and screen reader users (`layout.html`)
- **ARIA tree roles**: the file tree is now `role="tree"` with `treeitem`/`group`/`aria-level`/`aria-expanded`
- **Focus trap** in `kwDialog` modals — Tab/Shift+Tab stays inside the dialog
- **Mobile sidebar Escape**: `Esc` closes the sidebar and focus returns to the hamburger
- **`aria-live`** on the toast stack and search results; error toasts are `role="alert"`
- **`aria-label`** on the mobile selection buttons (move/tags/export/delete)
- **Clickable tags**: clicking starts a search with a `tag:<value>` prefix
- **Tag search** (`tag:<value>`) in FTS5 search via a LIKE fallback on the `tags` column
- **Reduced motion** global guard at the top of the stylesheet and in the login page

### Added — UX
- **`kwNewNote()`** — "New note" asks for a file name instead of overwriting `notes/neue-notiz.md`
- **Editor `beforeunload` warning** for unsaved changes
- **"No results for …"** as `role="status"` in the search results
- **Sidebar resizer** is now visible (4px hover indicator instead of hidden)

### Changed
- **`.btn-danger`** clearly set apart in red (error-dim background, error border) — delete actions no longer look harmless
- **`.file-meta`** uses `--md-on-surface-v` instead of the faint `--md-outline` (better contrast)
- **Breadcrumb** as `<button>` instead of `<a href="#">` (was dead without JS)
- **Editor save toast** via the central `kwToast()` instead of its own `.save-toast` markup
- **Tree filter** and select toggle on mobile now 44×44px / 16px font (WCAG 2.2, no iOS zoom)
- **Settings grid** responsive up to 1024px (two columns, submit on its own row)
- **Hint text** now reads the German "Doppelklick zum Umbenennen" instead of the English "double-click to rename" in the German UI
- **`#file-tree` `tabindex="0"`** removed (no duplicate tab stops next to the inner buttons)
- **Loading hint marked with `role="status"` and `aria-busy`** for tree/recent reloads

### Fixed — Codebase
- **CSS consolidation**: removed the redundant second `:root` from the "Professional UI refresh" block — the token source is now unambiguous
- **Empty `header-right` placeholder** removed

### Tests
- `tests/test_ui_file.py` extended with regression tests for: tags as clickable buttons, tag search, `<main>` landmark and skip link
- 136 tests green, Ruff clean

### Docs
- **`docs/ui-accessibility.md`** new: WCAG 2.2 AA model, keyboard shortcuts, ARIA tree, touch targets, focus management, PR checklist
- **`docs/architecture.md`** new: template hierarchy, tenancy/request flow, helper conventions, cache busting, test matrix
- **`README.md`** extended with v2.2 features, a keyboard shortcut table and an architecture reference
- **`CONTRIBUTING.md`** extended with the frontend workflow, UI PR checklist and helper naming
- **In-code comments** on `kwDialog` (focus trap), `openSidebar`/`closeSidebar` (focus management), `kwNewNote`/`kwSearchTag` (purpose) and the `beforeunload` guard

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

[Unreleased]: https://github.com/natorus87/kiwiki/compare/v4.1.0...HEAD
[4.1.0]: https://github.com/natorus87/kiwiki/compare/v4.0.0...v4.1.0
[4.0.0]: https://github.com/natorus87/kiwiki/compare/v3.2.0...v4.0.0
[3.2.0]: https://github.com/natorus87/kiwiki/compare/v3.1.1...v3.2.0
[3.1.1]: https://github.com/natorus87/kiwiki/compare/v3.1.0...v3.1.1
[3.1.0]: https://github.com/natorus87/kiwiki/compare/v3.0.0...v3.1.0
[3.0.0]: https://github.com/natorus87/kiwiki/compare/v2.6.0...v3.0.0
[2.6.0]: https://github.com/natorus87/kiwiki/compare/v2.5.0...v2.6.0
[2.5.0]: https://github.com/natorus87/kiwiki/compare/v2.4.1...v2.5.0
[2.2.0]: https://github.com/natorus87/kiwiki/compare/v2.1.1...v2.2.0
[2.1.1]: https://github.com/natorus87/kiwiki/compare/v2.1.0...v2.1.1
[2.1.0]: https://github.com/natorus87/kiwiki/compare/v2.0.2...v2.1.0
[2.0.2]: https://github.com/natorus87/kiwiki/releases/tag/v2.0.2
[2.0.1]: https://github.com/natorus87/kiwiki/releases/tag/v2.0.1
[2.0.0]: https://github.com/natorus87/kiwiki/releases/tag/v2.0.0
[0.1.0]: https://github.com/natorus87/kiwiki/releases/tag/v0.1.0
