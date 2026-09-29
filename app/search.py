import logging
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .models import SearchResult, MAX_QUERY_LENGTH
from .tenancy import user_root

logger = logging.getLogger("kiwiki.search")


def _db_file() -> Path:
    """Per-user FTS database location (one DB per namespace)."""
    db_dir = user_root() / ".kiwiki"
    db_dir.mkdir(parents=True, exist_ok=True)
    return db_dir / "index.sqlite"


# ── Connection pool (A4) ─────────────────────────────────────────────────────
# Simple per-thread connection pool: each thread gets one persistent connection
# per database file. Connections are recycled across requests within the same
# thread, avoiding the overhead of connect()/close() on every call.

_pool_lock = threading.Lock()
_pool: dict[tuple[str, int], sqlite3.Connection] = {}


def _get_pooled_conn(db_path: str) -> sqlite3.Connection:
    """Return a persistent connection for the given database path.

    Das Timeout bleibt bewusst bei 250 ms: die Schreibpfade des FTS-Index
    haben eine eigene Retry-Logik (`deindex_files` versucht dreimal mit
    Backoff 20/40/80 ms). Ein langes Warten wuerde diese Logik aushebeln —
    der Aufrufer wartet dann 10 s statt dreimal kurz zu probieren, und die
    Zeit wandert vom Aufrufer in den Lock.
    """
    key = (db_path, threading.get_ident())
    with _pool_lock:
        conn = _pool.get(key)
        if conn is not None:
            try:
                conn.execute("SELECT 1")
                return conn
            except sqlite3.ProgrammingError:
                _pool.pop(key, None)
        conn = sqlite3.connect(db_path, timeout=0.25, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=-8000")  # 8 MB page cache
        _pool[key] = conn
        return conn


def _drop_pooled_conn(db_path: str) -> None:
    """Verbindungen auf einen ersetzten DB-Pfad schliessen.

    Eine gepoolte Connection zeigt nach dem Loeschen der Datei weiterhin auf die
    alte Inode. Schreibzugriffe landen dann in einer Datei, die niemand mehr
    liest.
    """
    with _pool_lock:
        stale = [key for key in _pool if key[0] == db_path]
        for key in stale:
            try:
                _pool.pop(key).close()
            except Exception:
                logger.debug("Could not close stale pooled connection for %s", db_path, exc_info=True)


def close_pool() -> None:
    """Close all pooled connections (call on shutdown)."""
    with _pool_lock:
        for key, conn in _pool.items():
            try:
                conn.close()
            except Exception:
                # Beim Shutdown nicht fatal, aber nicht still: eine Leck
                # Verbindung waere sonst spaeter nicht mehr zuordenbar.
                logger.warning("Connection pool: cannot close entry %s", key, exc_info=True)
        _pool.clear()


@contextmanager
def get_db():
    """Context manager for database connections — uses connection pool."""
    db_path = str(_db_file())
    conn = _get_pooled_conn(db_path)
    yield conn


_FTS_VERSION = 3  # Bump to recreate table when indexed fields change.

# Per-namespace DBs are only schema-checked once per process lifetime;
# every search()/index_file() call was re-running the sqlite_master
# lookup and the CREATE TABLE IF NOT EXISTS statements otherwise.
_initialized_dbs_lock = threading.Lock()
_initialized_dbs: set[str] = set()


def init_db() -> None:
    """Initialize FTS5 table. Recreates with porter tokenizer on version bump.
    Uses CREATE TABLE IF NOT EXISTS for idempotency. Skips the schema check
    entirely once a given namespace's DB has been initialized this process."""
    db_path = str(_db_file())
    # Der Cache darf nur greifen, solange die Datei von damals noch da ist.
    # Wird ein Workspace geloescht und neu angelegt (z. B. Rollback in
    # api_create_user), zeigte der Eintrag sonst auf eine verschwundene DB und
    # verhinderte, dass die Tabellen je wieder erzeugt werden.
    if not Path(db_path).exists():
        with _initialized_dbs_lock:
            _initialized_dbs.discard(db_path)
        _drop_pooled_conn(db_path)
    with _initialized_dbs_lock:
        if db_path in _initialized_dbs:
            return
    schema_rebuilt = False
    with get_db() as conn:
        # Check if we need to recreate with porter tokenizer (E1)
        try:
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='files'"
            ).fetchone()
            schema = (row[0] or "") if row else ""
            if row and ("porter" not in schema or "revision" not in schema):
                # Alte Tabelle ohne aktuellen Tokenizer/Felder neu erstellen.
                conn.execute("DROP TABLE IF EXISTS files")
                schema_rebuilt = True
        except Exception:
            # Die Tabelle wird danach ohnehin per CREATE TABLE IF NOT EXISTS
            # angelegt; ein Fehler hier darf die Indexierung nicht abbrechen.
            # Verschwiegen waere er nur, wenn die Migration danach dauerhaft
            # fehlschlaegt — das zeigt der naechste Indexierungsversuch.
            logger.warning("Search index: cannot inspect existing schema", exc_info=True)

        conn.execute(
            """
        CREATE VIRTUAL TABLE IF NOT EXISTS files USING fts5(
            path,
            title,
            tags,
            content,
            revision UNINDEXED,
            updated_at,
            owner,
            tokenize='porter unicode61'
        )
        """
        )
        # E3: Create search history table
        conn.execute(
            """
        CREATE TABLE IF NOT EXISTS search_history (
            query TEXT,
            timestamp REAL,
            result_count INTEGER
        )
        """
        )
        conn.commit()
    if schema_rebuilt:
        try:
            (Path(db_path).parent / ".last_reindex").unlink(missing_ok=True)
        except OSError:
            logger.exception("Failed to invalidate incremental reindex timestamp for %s", db_path)
    with _initialized_dbs_lock:
        _initialized_dbs.add(db_path)


def index_file(file_path: str) -> None:
    """
    Index a markdown file in FTS5.
    Overwrites existing entry if present.
    """
    from .storage import _path_lock, read_file, safe_path

    try:
        with _path_lock(file_path):
            full_path = safe_path(file_path)
            if not full_path.exists() or not full_path.is_file():
                return
            content = read_file(file_path)
            revision = full_path.stat().st_mtime_ns
            title = str(content.frontmatter.get("title", full_path.stem))
            tags = ",".join(str(tag) for tag in content.frontmatter.get("tags", []))
            updated_at = str(content.frontmatter.get("updated", "") or "")
            owner = str(content.frontmatter.get("owner", "") or "")
            with get_db() as conn:
                conn.execute("DELETE FROM files WHERE path = ?", (file_path,))
                conn.execute(
                    """
                INSERT INTO files (path, title, tags, content, revision, updated_at, owner)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                    (file_path, title, tags, content.content, revision, updated_at, owner),
                )
                conn.commit()
    except Exception:
        logger.exception("Failed to index markdown file %r", file_path)


def deindex_file(file_path: str) -> None:
    """Remove a file from the FTS5 index."""
    deindex_files([file_path])


def deindex_files(file_paths: list[str]) -> None:
    """Remove multiple files from the FTS5 index in one bounded transaction."""
    params = [(file_path,) for file_path in file_paths]
    if not params:
        return
    for attempt in range(3):
        with get_db() as conn:
            try:
                conn.executemany("DELETE FROM files WHERE path = ?", params)
                conn.commit()
                return
            except sqlite3.OperationalError:
                conn.rollback()
                if attempt == 2:
                    raise
                logger.debug(
                    "deindex_files: database locked, retrying (attempt %d of 3, %d paths)",
                    attempt + 2,
                    len(params),
                )
        time.sleep(0.02 * (2 ** attempt))


def _sanitize_fts(query: str) -> str:
    """Normalize a query so FTS5 won't throw a syntax error.

    - Strips any col:value prefix (filename:, path:, etc.) keeping only the value part
    - Removes characters FTS5 can't handle in token position (. : / @ -)
    """
    # Always drop column prefix, keep value — FTS5 column filters are brittle
    query = re.sub(r'\w+:(\S*)', r'\1', query)
    # Remove chars invalid in FTS5 token positions
    query = re.sub(r'[./:@\-]', ' ', query)
    return ' '.join(query.split()) or query


def _fts_rows(conn, fts_query: str):
    return conn.execute(
        "SELECT path, title, content, revision, rank FROM files WHERE files MATCH ? ORDER BY rank LIMIT 20",
        (fts_query,),
    ).fetchall()


def _path_rows(conn, raw_query: str):
    """Fallback: LIKE search on path and title when FTS returns nothing."""
    term = f"%{raw_query.strip().split()[0]}%"
    return conn.execute(
        "SELECT path, title, content, revision, 0 AS rank "
        "FROM files WHERE path LIKE ? OR title LIKE ? LIMIT 20",
        (term, term),
    ).fetchall()


_MD_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
# [[ziel|Label]] -> Label, [[pfad/ziel.md]] -> ziel. Muss vor _MD_NOISE laufen:
# das entfernt `|` als Tabellen-Pipe und klebte Ziel und Label zusammen
# ("[[../adr-001-sqliteADR 001]]").
_WIKILINK = re.compile(r"!?\[\[([^\]|]+?)(?:\|([^\]]+?))?\]\]")


def _wikilink_text(match: re.Match) -> str:
    if match.group(2):
        return match.group(2).strip()
    stem = match.group(1).strip().rsplit("/", 1)[-1]
    return stem[:-3] if stem.endswith(".md") else stem

_MD_NOISE = re.compile(
    r"^\s{0,3}\|?[\s:|-]*\|[\s:|-]*$"      # Tabellen-Trennzeilen (|---|:--:|)
    r"|^\s{0,3}(#{1,6}\s+|>\s?|[-*+]\s+|\d+\.\s+)"  # Ueberschrift, Zitat, Liste
    r"|^\s*-{3,}\s*$"                       # horizontale Linie
    r"|[*_`~|]",                            # Auszeichnung und Tabellen-Pipes
    re.M,
)


def _clean_snippet(content: str, query: str, width: int = 180) -> str:
    """Lesbarer Textausschnitt rund um den Treffer.

    Vorher waren es die ersten 200 Zeichen der Rohdatei: meist Frontmatter und
    Markdown-Syntax, und nie die Stelle, die zur Suche passt.
    """
    body = content.split("---", 2)[2] if content.lstrip().startswith("---") else content
    body = _MD_LINK.sub(r"\1", body)  # [Text](url) -> Text
    body = _WIKILINK.sub(_wikilink_text, body)
    text = " ".join(_MD_NOISE.sub("", body).split())
    if not text:
        return ""
    term = next((w for w in query.replace("tag:", " ").split() if len(w) > 1), "")
    hit = text.lower().find(term.lower()) if term else -1
    if hit <= width // 2:
        start = 0
    else:
        start = max(0, min(hit - width // 3, len(text) - width))
    excerpt = text[start:start + width]
    return ("…" if start else "") + excerpt.strip() + ("…" if start + width < len(text) else "")


def content_excerpt(content: str, width: int = 120, title: str = "") -> str:
    """Erste sinnvolle Zeilen als Vorschau (Dashboard-Listen haben keine Query).

    Nutzt dieselbe Bereinigung wie die Treffer-Snippets: ohne Frontmatter
    und ohne Markdown-Syntax, sonst staende in jeder Zeile `---\ntitle:`.
    Eine fuehrende Ueberschrift, die dem angezeigten Titel entspricht, wird
    uebersprungen — sonst stand der Titel in der Liste zweimal hintereinander.
    """
    if title:
        lines = content.lstrip("\n").split("\n", 1)
        heading = re.match(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$", lines[0])
        if heading and _normalize_heading(heading.group(1)) == _normalize_heading(title):
            content = lines[1] if len(lines) > 1 else ""
    return _clean_snippet(content, "", width=width)


def _normalize_heading(text: str) -> str:
    return " ".join(re.sub(r"[*_`~]", "", text).split()).casefold()


def _to_results(rows, query: str = "") -> list[SearchResult]:
    from .storage import safe_path

    out = []
    seen = set()
    for row in rows:
        if row["path"] in seen:
            continue
        try:
            file_path = safe_path(row["path"])
            if not file_path.is_file():
                continue
            if file_path.stat().st_mtime_ns != int(row["revision"]):
                continue
        except (OSError, ValueError):
            continue
        seen.add(row["path"])
        out.append(SearchResult(
            path=row["path"],
            title=row["title"],
            snippet=_clean_snippet(row["content"] or "", query),
            score=abs(row["rank"]),
        ))
    return out


def search(query: str) -> list[SearchResult]:
    """Full-text search with FTS5 (porter tokenizer); falls back to LIKE on FTS syntax errors.

    Special prefix ``tag:<value>`` performs a LIKE search on the ``tags``
    column (FTS5 column filters are brittle, so we sidestep them here).
    Records search in history (E3).
    """
    if not query.strip():
        return []
    # MCP- und UI-Pfade haben kein Pydantic-Limit (nur REST via SearchRequest).
    # Unbegrenzt liefe ein MB-Query in FTS-Sanitize + LIKE-Fallback und brennt CPU.
    query = query.strip()[:MAX_QUERY_LENGTH]
    init_db()
    with get_db() as conn:
        tag_match = re.match(r'^\s*tag:(.+?)\s*$', query, re.IGNORECASE)
        if tag_match:
            tag_term = tag_match.group(1).strip()
            rows = conn.execute(
                "SELECT path, title, content, revision, 0 AS rank FROM files "
                "WHERE instr(',' || lower(tags) || ',', ',' || lower(?) || ',') > 0 "
                "ORDER BY title LIMIT 50",
                (tag_term,),
            ).fetchall()
            return _to_results(rows, query)

        clean = _sanitize_fts(query)
        try:
            rows = _fts_rows(conn, clean)
        except sqlite3.OperationalError:
            # sanitized query still invalid — strip to bare words
            words = re.sub(r'[^\w\s]', ' ', query).split()
            try:
                rows = _fts_rows(conn, ' '.join(words)) if words else []
            except sqlite3.OperationalError:
                rows = []
        results = _to_results(rows, query)
        # If FTS found nothing, try a path/title LIKE search as last resort.
        # Auch dieser Zweig muss OperationalError abfangen — sonst schlaegt eine
        # fehlende oder beschaedigte Tabelle bis zum Aufrufer durch, waehrend der
        # FTS-Zweig darueber sie sauber behandelt.
        if not results:
            try:
                results = _to_results(_path_rows(conn, query), query)
            except sqlite3.OperationalError:
                logger.warning("Search fallback unavailable for %r", query, exc_info=True)
                return []

        # E3: Record search in history (skip tag: and empty queries)
        if query.strip() and not query.strip().startswith("tag:"):
            try:
                conn.execute(
                    "INSERT INTO search_history (query, timestamp, result_count) VALUES (?, ?, ?)",
                    (query.strip(), time.time(), len(results)),
                )
                # Prune old entries (keep last 1000)
                conn.execute(
                    "DELETE FROM search_history WHERE rowid NOT IN "
                    "(SELECT rowid FROM search_history ORDER BY timestamp DESC LIMIT 1000)"
                )
                conn.commit()
            except Exception:
                # Die Trefferliste selbst ist gueltig; nur die Historie fehlt dann.
                # Ohne Log sieht der Nutzer eine leere Historie und weiss nicht,
                # ob die Suche kaputt ist oder nichts gefunden hat.
                logger.error("Search history: cannot record query %r", query, exc_info=True)

        return results


def reindex_all() -> int:
    """
    Reindex all markdown files in the current user's namespace.
    Returns count of indexed files.
    """
    init_db()
    with get_db() as conn:
        conn.execute("DELETE FROM files")
        conn.commit()
    count = 0
    root = user_root()
    for md_file in root.rglob("*.md"):
        rel_path = str(md_file.relative_to(root))
        if rel_path in {"AGENTS.md", "index.md"}:
            continue
        index_file(rel_path)
        count += 1
    return count


def reindex_changed() -> int:
    """
    A6: Lazy reindex — only reindex files whose mtime is newer than the
    last index timestamp. Falls back to full reindex if no timestamp exists.
    Returns count of (re)indexed files.
    """
    init_db()
    db_dir = user_root() / ".kiwiki"
    db_dir.mkdir(parents=True, exist_ok=True)
    timestamp_file = db_dir / ".last_reindex"

    last_reindex = 0.0
    if timestamp_file.exists():
        try:
            last_reindex = float(timestamp_file.read_text().strip())
        except (ValueError, OSError):
            last_reindex = 0.0

    root = user_root()
    count = 0

    current_paths = {
        str(md_file.relative_to(root))
        for md_file in root.rglob("*.md")
        if str(md_file.relative_to(root)) not in {"AGENTS.md", "index.md"}
        and ".kiwiki" not in md_file.relative_to(root).parts
    }
    with get_db() as conn:
        indexed_paths = {row[0] for row in conn.execute("SELECT path FROM files").fetchall()}
        deleted_paths = indexed_paths - current_paths
        if deleted_paths:
            conn.executemany("DELETE FROM files WHERE path = ?", [(path,) for path in deleted_paths])
            conn.commit()

    if last_reindex > 0:
        # Incremental: only reindex files modified since last reindex
        for rel_path in sorted(current_paths):
            md_file = root / rel_path
            try:
                if md_file.stat().st_mtime > last_reindex:
                    index_file(rel_path)
                    count += 1
            except Exception:
                continue
    else:
        # No timestamp yet — full reindex
        count = reindex_all()

    # Write current timestamp
    try:
        timestamp_file.write_text(str(time.time()))
    except OSError:
        pass

    return count


def get_search_history(limit: int = 10) -> list[dict]:
    """E3: Return recent unique search queries, newest first."""
    init_db()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT query, MAX(timestamp) as ts, MAX(result_count) as cnt "
            "FROM search_history GROUP BY query ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [{"query": r["query"], "timestamp": r["ts"], "result_count": r["cnt"]} for r in rows]


def record_search(query: str, result_count: int) -> None:
    """E3: Explicitly record a search (for external callers like MCP tools)."""
    init_db()
    with get_db() as conn:
        try:
            conn.execute(
                "INSERT INTO search_history (query, timestamp, result_count) VALUES (?, ?, ?)",
                (query.strip(), time.time(), result_count),
            )
            conn.commit()
        except Exception:
            logger.error("Search history: cannot record query %r", query, exc_info=True)
