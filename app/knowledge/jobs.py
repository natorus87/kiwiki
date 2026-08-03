"""Persistente, deduplizierte Dirty-Queue."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class KnowledgeJob:
    path: str
    operation: str
    revision: int | None
    attempts: int


def enqueue_job(
    connection: sqlite3.Connection,
    path: str,
    operation: str,
    revision: int | None = None,
) -> None:
    connection.execute(
        """INSERT INTO knowledge_jobs(path, operation, revision, attempts, state, last_error, updated_at)
        VALUES (?, ?, ?, 0, 'pending', NULL, ?)
        ON CONFLICT(path) DO UPDATE SET
            operation=excluded.operation,
            revision=CASE
                WHEN excluded.revision IS NULL THEN knowledge_jobs.revision
                WHEN knowledge_jobs.revision IS NULL THEN excluded.revision
                ELSE MAX(knowledge_jobs.revision, excluded.revision)
            END,
            attempts=0, state='pending', last_error=NULL, updated_at=excluded.updated_at""",
        (path, operation, revision, time.time()),
    )
    connection.commit()


def claim_jobs(connection: sqlite3.Connection, limit: int = 25) -> list[KnowledgeJob]:
    limit = max(1, min(int(limit), 100))
    try:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            "SELECT path, operation, revision, attempts FROM knowledge_jobs "
            "WHERE state = 'pending' ORDER BY updated_at, path LIMIT ?",
            (limit,),
        ).fetchall()
        connection.executemany(
            "UPDATE knowledge_jobs SET state='running', attempts=attempts+1, updated_at=? WHERE path=?",
            [(time.time(), row[0]) for row in rows],
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return [KnowledgeJob(str(row[0]), str(row[1]), row[2], int(row[3]) + 1) for row in rows]


def recover_running_jobs(connection: sqlite3.Connection) -> None:
    connection.execute(
        "UPDATE knowledge_jobs SET state='pending', updated_at=? WHERE state='running'",
        (time.time(),),
    )
    connection.commit()


def complete_job(connection: sqlite3.Connection, path: str) -> None:
    connection.execute("DELETE FROM knowledge_jobs WHERE path = ?", (path,))
    connection.commit()


def fail_job(connection: sqlite3.Connection, path: str, exc: Exception) -> None:
    row = connection.execute("SELECT attempts FROM knowledge_jobs WHERE path = ?", (path,)).fetchone()
    attempts = int(row[0]) if row else 3
    state = "failed" if attempts >= 3 else "pending"
    error = type(exc).__name__[:128]
    connection.execute(
        "UPDATE knowledge_jobs SET state=?, last_error=?, updated_at=? WHERE path=?",
        (state, error, time.time(), path),
    )
    connection.commit()
