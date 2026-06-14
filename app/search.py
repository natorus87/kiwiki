import logging
import re
import sqlite3
from pathlib import Path

from .models import SearchResult
from .tenancy import user_root

logger = logging.getLogger("kiwiki.search")


def _db_file() -> Path:
    """Per-user FTS database location (one DB per namespace)."""
    db_dir = user_root() / ".kiwiki"
    db_dir.mkdir(parents=True, exist_ok=True)
    return db_dir / "index.sqlite"


def get_db() -> sqlite3.Connection:
    """Get database connection for the current user's namespace."""
    conn = sqlite3.connect(str(_db_file()))
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Initialize FTS5 table if not exists."""
    conn = get_db()
    conn.execute(
        """
    CREATE VIRTUAL TABLE IF NOT EXISTS files USING fts5(
        path,
        title,
        tags,
        content,
        updated_at,
        owner
    )
    """
    )
    conn.commit()
    conn.close()


def index_file(file_path: str) -> None:
    """
    Index a markdown file in FTS5.
    Overwrites existing entry if present.
    """
    from .storage import safe_path, read_file

    conn = None
    try:
        full_path = safe_path(file_path)
        if not full_path.exists() or not full_path.is_file():
            return
        content = read_file(file_path)
        title = content.frontmatter.get("title", full_path.stem)
        tags = ",".join(content.frontmatter.get("tags", []))
        updated_at = content.frontmatter.get("updated", "")
        owner = content.frontmatter.get("owner", "")
        conn = get_db()
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
    finally:
        if conn is not None:
            conn.close()


def deindex_file(file_path: str) -> None:
    """Remove a file from the FTS5 index."""
    conn = get_db()
    conn.execute("DELETE FROM files WHERE path = ?", (file_path,))
    conn.commit()
    conn.close()


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
    """Full-text search with FTS5; falls back to LIKE on FTS syntax errors."""
    init_db()
    conn = get_db()
    try:
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
        return results
    finally:
        conn.close()


def reindex_all() -> int:
    """
    Reindex all markdown files in the current user's namespace.
    Returns count of indexed files.
    """
    conn = get_db()
    conn.execute("DELETE FROM files")
    conn.commit()
    conn.close()
    count = 0
    root = user_root()
    for md_file in root.rglob("*.md"):
        rel_path = str(md_file.relative_to(root))
        if rel_path in {"AGENTS.md", "index.md"}:
            continue
        index_file(rel_path)
        count += 1
    return count
