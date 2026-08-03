"""Restart-sicherer Abgleich zwischen Markdown-Quelle und Knowledge-Index."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .jobs import enqueue_job


def reconcile_workspace(connection: sqlite3.Connection, workspace: Path, batch_size: int = 25) -> int:
    workspace = Path(workspace).resolve()
    batch_size = max(1, min(int(batch_size), 1000))
    current: dict[str, int] = {}
    for file_path in sorted(workspace.rglob("*.md")):
        relative = file_path.relative_to(workspace)
        if ".kiwiki" in relative.parts or str(relative) in {"AGENTS.md", "index.md"}:
            continue
        current[str(relative)] = file_path.stat().st_mtime_ns
    indexed = {
        str(path): int(revision)
        for path, revision in connection.execute("SELECT path, revision FROM documents").fetchall()
    }
    queued = 0
    for path, revision in current.items():
        if indexed.get(path) != revision:
            enqueue_job(connection, path, "upsert", revision)
            queued += 1
    for path in indexed.keys() - current.keys():
        enqueue_job(connection, path, "delete")
        queued += 1
    return queued
