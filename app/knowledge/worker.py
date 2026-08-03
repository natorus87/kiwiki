"""Ein einzelner fairer Background-Worker für alle Tenant-Queues."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable, Iterable

from ..tenancy import CURRENT_USER_NS, is_valid_username
from .service import is_enabled, process_pending_current_workspace, rebuild_current_workspace

logger = logging.getLogger("kiwiki.knowledge.worker")


async def run_worker(stop: asyncio.Event, usernames: Callable[[], Iterable[str]]) -> None:
    reconciled: set[str] = set()
    while not stop.is_set():
        if is_enabled():
            for username in sorted(set(usernames())):
                if not is_valid_username(username):
                    continue
                token = CURRENT_USER_NS.set(username)
                try:
                    if username not in reconciled:
                        await asyncio.to_thread(rebuild_current_workspace)
                        reconciled.add(username)
                    await asyncio.to_thread(
                        process_pending_current_workspace,
                        int(os.getenv("KIWIKI_KNOWLEDGE_BACKFILL_BATCH_SIZE", "25")),
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Knowledge worker failed for tenant %r", username)
                finally:
                    CURRENT_USER_NS.reset(token)
        try:
            await asyncio.wait_for(stop.wait(), timeout=0.25)
        except TimeoutError:
            pass
