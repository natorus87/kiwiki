import logging
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .models import SearchResult
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
    """Return a persistent connection for the given database path."""
    key = (db_path, threading.get_ident())
    with _pool_lock:
        conn = _pool.get(key)
        if conn is not None:
            try:
                conn.execute("SELECT 1")
                return conn
            except sqlite3.ProgrammingError:
                _pool.pop(key, None)
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=-8000")  # 8 MB page cache
        _pool[key] = conn
        return conn


def close_pool() -> None:
    """Close all pooled connections (call on shutdown)."""
    with _pool_lock:
        for conn in _pool.values():
            try:
                conn.close()
            except Exception:
                pass
        _pool.clear()


@contextmanager
def get_db():
    """Context manager for database connections — uses connection pool."""
    db_path = str(_db_file())
    conn = _get_pooled_conn(db_path)
    yield conn


_FTS_VERSION = 2  # Bump to recreate table with new tokenizer

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
    with _initialized_dbs_lock:
        if db_path in _initialized_dbs:
            return
    with get_db() as conn:
        # Check if we need to recreate with porter tokenizer (E1)
        try:
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='files'"
            ).fetchone()
            if row and "porter" not in (row[0] or ""):
                # Old table without porter — drop and recreate
                conn.execute("DROP TABLE IF EXISTS files")
                conn.execute("DELETE FROM sqlite_master WHERE type='table' AND name='files'")
        except Exception:
            pass

        conn.execute(
            """
        CREATE VIRTUAL TABLE IF NOT EXISTS files USING fts5(
            path,
            title,
            tags,
            content,
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
    with _initialized_dbs_lock:
        _initialized_dbs.add(db_path)


def index_file(file_path: str) -> None:
    """
    Index a markdown file in FTS5.
    Overwrites existing entry if present.
    """
    from .storage import safe_path, read_file

    try:
        full_path = safe_path(file_path)
        if not full_path.exists() or not full_path.is_file():
            return
        content = read_file(file_path)
        title = str(content.frontmatter.get("title", full_path.stem))
        tags = ",".join(str(tag) for tag in content.frontmatter.get("tags", []))
        updated_at = str(content.frontmatter.get("updated", "") or "")
        owner = str(content.frontmatter.get("owner", "") or "")
        with get_db() as conn:
            conn.execute("DELETE FROM files WHERE path = ?", (file_path,))
            conn.execute(
                """
            INSERT INTO files (path, title, tags, content, updated_at, owner)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
                (file_path, title, tags, content.content, updated_at, owner),
            )
            conn.commit()
    except Exception:
        logger.exception("Failed to index markdown file %r", file_path)


def deindex_file(file_path: str) -> None:
    """Remove a file from the FTS5 index."""
    with get_db() as conn:
        conn.execute("DELETE FROM files WHERE path = ?", (file_path,))
        conn.commit()


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
        "SELECT path, title, content, rank FROM files WHERE files MATCH ? ORDER BY rank LIMIT 20",
        (fts_query,),
    ).fetchall()


def _path_rows(conn, raw_query: str):
    """Fallback: LIKE search on path and title when FTS returns nothing."""
    term = f"%{raw_query.strip().split()[0]}%"
    return conn.execute(
        "SELECT path, title, content, 0 AS rank FROM files WHERE path LIKE ? OR title LIKE ? LIMIT 20",
        (term, term),
    ).fetchall()


def _to_results(rows) -> list[SearchResult]:
    out = []
    seen = set()
    for row in rows:
        if row["path"] in seen:
            continue
        seen.add(row["path"])
        content = row["content"] or ""
        snippet = content[:200] + ("..." if len(content) > 200 else "")
        out.append(SearchResult(
            path=row["path"],
            title=row["title"],
            snippet=snippet,
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
    init_db()
    with get_db() as conn:
        tag_match = re.match(r'^\s*tag:(.+?)\s*$', query, re.IGNORECASE)
        if tag_match:
            tag_term = tag_match.group(1).strip()
            rows = conn.execute(
                "SELECT path, title, content, 0 AS rank FROM files "
                "WHERE instr(',' || lower(tags) || ',', ',' || lower(?) || ',') > 0 "
                "ORDER BY title LIMIT 50",
                (tag_term,),
            ).fetchall()
            return _to_results(rows)

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
        results = _to_results(rows)
        # If FTS found nothing, try a path/title LIKE search as last resort
        if not results:
            results = _to_results(_path_rows(conn, query))

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
                pass

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
            pass
