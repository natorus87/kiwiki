"""SQLite-Schema und sichere Verbindungen für den per-Tenant-Knowledge-Index."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path


SCHEMA_VERSION = 1


class UnsupportedSchemaVersion(RuntimeError):
    """Die Datenbank stammt von einer neueren, unbekannten Kiwiki-Version."""


def _database_path(workspace: Path) -> Path:
    workspace = workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    internal = workspace / ".kiwiki"
    if internal.is_symlink():
        raise ValueError("Knowledge directory must not be a symlink")
    internal.mkdir(mode=0o700, parents=True, exist_ok=True)
    internal.chmod(0o700)
    database = internal / "knowledge.sqlite"
    if database.is_symlink():
        raise ValueError("Knowledge database must not be a symlink")
    return database


def open_database(workspace: Path) -> sqlite3.Connection:
    """Öffnet oder migriert den abgeleiteten Index eines einzelnen Workspaces."""
    database = _database_path(Path(workspace))
    connection = sqlite3.connect(database, timeout=1.0, check_same_thread=False)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 1000")
    connection.execute("PRAGMA synchronous = NORMAL")
    connection.execute("PRAGMA cache_size = -2048")
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version > SCHEMA_VERSION:
        connection.close()
        raise UnsupportedSchemaVersion(
            f"Knowledge schema {version} is newer than supported version {SCHEMA_VERSION}"
        )
    if version == 0:
        # journal_mode ist persistent. Ihn nur beim Anlegen umzuschalten vermeidet
        # exklusive Locks bei parallelen API-, Worker- und Graph-Reads.
        connection.execute("PRAGMA journal_mode = WAL")
        _migrate_v1(connection)
    database.chmod(0o600)
    return connection


def _migrate_v1(connection: sqlite3.Connection) -> None:
    statements = (
        "CREATE TABLE IF NOT EXISTS knowledge_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
        """CREATE TABLE IF NOT EXISTS knowledge_jobs (
            path TEXT PRIMARY KEY,
            operation TEXT NOT NULL CHECK(operation IN ('upsert','delete','reconcile')),
            revision INTEGER CHECK(revision IS NULL OR revision >= 0),
            attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
            state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','running','failed')),
            last_error TEXT,
            updated_at REAL NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS documents (
            path TEXT PRIMARY KEY,
            revision INTEGER NOT NULL CHECK(revision >= 0),
            size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
            content_hash TEXT NOT NULL,
            title TEXT NOT NULL,
            document_type TEXT,
            owner TEXT,
            extraction_version INTEGER NOT NULL,
            scan_generation INTEGER NOT NULL DEFAULT 0,
            indexed_at REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'ready' CHECK(status IN ('ready','failed')),
            last_error TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS entities (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            canonical_name TEXT NOT NULL,
            normalized_name TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            UNIQUE(kind, normalized_name)
        )""",
        """CREATE TABLE IF NOT EXISTS aliases (
            entity_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
            alias TEXT NOT NULL,
            normalized_alias TEXT NOT NULL,
            PRIMARY KEY(entity_id, normalized_alias)
        )""",
        """CREATE TABLE IF NOT EXISTS mentions (
            document_path TEXT NOT NULL REFERENCES documents(path) ON DELETE CASCADE,
            entity_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
            heading TEXT,
            line_start INTEGER CHECK(line_start IS NULL OR line_start >= 1),
            line_end INTEGER CHECK(line_end IS NULL OR line_end >= line_start),
            source_hash TEXT NOT NULL,
            PRIMARY KEY(document_path, entity_id, source_hash)
        )""",
        """CREATE TABLE IF NOT EXISTS relations (
            id TEXT PRIMARY KEY,
            subject_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
            predicate TEXT NOT NULL,
            object_id TEXT REFERENCES entities(id) ON DELETE CASCADE,
            object_value TEXT,
            source_path TEXT NOT NULL REFERENCES documents(path) ON DELETE CASCADE,
            source_revision INTEGER NOT NULL CHECK(source_revision >= 0),
            source_hash TEXT NOT NULL,
            extraction_kind TEXT NOT NULL,
            confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
            valid_from TEXT,
            valid_until TEXT,
            CHECK((object_id IS NULL) != (object_value IS NULL))
        )""",
        """CREATE TABLE IF NOT EXISTS document_links (
            source_path TEXT NOT NULL REFERENCES documents(path) ON DELETE CASCADE,
            target_path TEXT NOT NULL,
            label TEXT,
            line_number INTEGER CHECK(line_number IS NULL OR line_number >= 1),
            PRIMARY KEY(source_path, target_path, line_number)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(normalized_name)",
        "CREATE INDEX IF NOT EXISTS idx_relations_source ON relations(source_path)",
        "CREATE INDEX IF NOT EXISTS idx_relations_value ON relations(object_value)",
        "CREATE INDEX IF NOT EXISTS idx_links_target ON document_links(target_path)",
    )
    try:
        connection.execute("BEGIN IMMEDIATE")
        for statement in statements:
            connection.execute(statement)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def database_size_bytes(workspace: Path) -> int:
    database = _database_path(Path(workspace))
    return database.stat().st_size if database.exists() else 0


def has_safe_free_space(workspace: Path) -> bool:
    stat = os.statvfs(Path(workspace))
    free = stat.f_bavail * stat.f_frsize
    floor = int(os.getenv("KIWIKI_KNOWLEDGE_MIN_FREE_BYTES", str(128 * 1024 * 1024)))
    return free >= floor
