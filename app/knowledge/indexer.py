"""Transaktionaler Dokument-Indexer für den lokalen Knowledge-Graphen."""

from __future__ import annotations

import hashlib
import sqlite3
import time
import unicodedata
from pathlib import Path

from .extract import extract_document


EXTRACTION_VERSION = 1


def _normalized(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())[:200]


def _entity_id(kind: str, name: str) -> str:
    return hashlib.sha256(f"{kind}\0{_normalized(name)}".encode()).hexdigest()[:32]


def _relation_id(path: str, predicate: str, value: str) -> str:
    return hashlib.sha256(f"{path}\0{predicate}\0{value}".encode()).hexdigest()


def _upsert_entity(connection: sqlite3.Connection, kind: str, name: str, now: float) -> str:
    entity_id = _entity_id(kind, name)
    connection.execute(
        """INSERT INTO entities(id, kind, canonical_name, normalized_name, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(kind, normalized_name) DO UPDATE SET canonical_name=excluded.canonical_name,
        updated_at=excluded.updated_at""",
        (entity_id, kind, name[:200], _normalized(name), now, now),
    )
    return entity_id


def upsert_document(connection: sqlite3.Connection, workspace: Path, path: str) -> None:
    file_path = (Path(workspace).resolve() / path).resolve()
    root = Path(workspace).resolve()
    if file_path != root and not str(file_path).startswith(str(root) + "/"):
        raise ValueError("Document escapes workspace")
    max_bytes = int(__import__("os").getenv("KIWIKI_KNOWLEDGE_MAX_FILE_BYTES", "1048576"))
    stat = file_path.stat()
    if stat.st_size > max_bytes:
        raise ValueError("Document exceeds knowledge indexing size limit")
    raw = file_path.read_bytes()
    markdown = raw.decode("utf-8")
    extracted = extract_document(path, markdown)
    content_hash = hashlib.sha256(raw).hexdigest()
    now = time.time()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM documents WHERE path = ?", (path,))
        connection.execute(
            """INSERT INTO documents(path, revision, size_bytes, content_hash, title, document_type,
            owner, extraction_version, scan_generation, indexed_at, status, last_error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 'ready', NULL)""",
            (path, stat.st_mtime_ns, stat.st_size, content_hash, extracted.title,
             extracted.document_type, extracted.owner, EXTRACTION_VERSION, now),
        )
        document_entity = _upsert_entity(connection, "document", path, now)
        connection.execute(
            "INSERT INTO mentions(document_path, entity_id, heading, line_start, line_end, source_hash) "
            "VALUES (?, ?, NULL, NULL, NULL, ?)",
            (path, document_entity, content_hash),
        )
        for tag in extracted.tags[:100]:
            tag_entity = _upsert_entity(connection, "tag", tag, now)
            connection.execute(
                "INSERT OR IGNORE INTO mentions(document_path, entity_id, heading, line_start, line_end, source_hash) "
                "VALUES (?, ?, NULL, NULL, NULL, ?)",
                (path, tag_entity, content_hash),
            )
            connection.execute(
                """INSERT INTO relations(id, subject_id, predicate, object_id, object_value,
                source_path, source_revision, source_hash, extraction_kind, confidence)
                VALUES (?, ?, 'tagged_with', NULL, ?, ?, ?, ?, 'frontmatter', 1.0)""",
                (_relation_id(path, "tagged_with", tag), document_entity, tag, path,
                 stat.st_mtime_ns, content_hash),
            )
        for related in extracted.related[:100]:
            connection.execute(
                """INSERT INTO relations(id, subject_id, predicate, object_id, object_value,
                source_path, source_revision, source_hash, extraction_kind, confidence)
                VALUES (?, ?, 'related_to', NULL, ?, ?, ?, ?, 'frontmatter', 1.0)""",
                (_relation_id(path, "related_to", related), document_entity, related, path,
                 stat.st_mtime_ns, content_hash),
            )
        for link in extracted.links:
            connection.execute(
                "INSERT INTO document_links(source_path, target_path, label, line_number) VALUES (?, ?, ?, ?)",
                (path, link.target_path, link.label, link.line_number),
            )
        _cleanup_orphaned_entities(connection)
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def delete_document(connection: sqlite3.Connection, path: str) -> None:
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM documents WHERE path = ?", (path,))
        _cleanup_orphaned_entities(connection)
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _cleanup_orphaned_entities(connection: sqlite3.Connection) -> None:
    connection.execute(
        """DELETE FROM entities WHERE id NOT IN (SELECT entity_id FROM mentions)
        AND id NOT IN (SELECT subject_id FROM relations)
        AND id NOT IN (SELECT object_id FROM relations WHERE object_id IS NOT NULL)"""
    )
