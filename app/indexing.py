"""Gemeinsame Fassade für alle aus Markdown abgeleiteten Indizes."""

from __future__ import annotations

import logging

from .search import deindex_file as _fts_delete
from .search import deindex_files as _fts_delete_many
from .search import index_file as _fts_upsert
from .storage import safe_path

logger = logging.getLogger("kiwiki.indexing")


def index_document(path: str) -> None:
    _fts_upsert(path)
    try:
        from .knowledge.service import queue_upsert

        file_path = safe_path(path)
        revision = file_path.stat().st_mtime_ns if file_path.is_file() else None
        queue_upsert(path, revision)
    except Exception:
        logger.exception("Knowledge enqueue failed after indexing %r", path)


def deindex_document(path: str) -> None:
    _fts_delete(path)
    try:
        from .knowledge.service import queue_delete

        queue_delete(path)
    except Exception:
        logger.exception("Knowledge delete enqueue failed for %r", path)


def deindex_documents(paths: list[str]) -> None:
    _fts_delete_many(paths)
    for path in paths:
        try:
            from .knowledge.service import queue_delete

            queue_delete(path)
        except Exception:
            logger.exception("Knowledge delete enqueue failed for %r", path)
