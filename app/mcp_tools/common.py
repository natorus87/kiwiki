"""Geteilter Aufrufkontext und Registry der MCP-Werkzeuge.

Ersetzt die Closures der alten _dispatch-Funktion (_need_read,
_need_write, _need_admin, _bounded_list): gleiche Guards, ein
Ort, einzeln testbar.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any
from ..auth import ROLE_HIERARCHY
from ..models import User


_MCP_MAX_LIST_ITEMS = 50


Handler = Callable[["McpContext"], Coroutine[Any, Any, str]]
HANDLERS: dict[str, Handler] = {}


def tool(name: str):
    """Registriert einen Werkzeug-Handler unter seinem MCP-Namen."""
    def wrap(fn: Handler) -> Handler:
        HANDLERS[name] = fn
        return fn
    return wrap


class McpContext:
    """Geteilter Aufrufkontext aller MCP-Werkzeug-Handler."""

    def __init__(self, args: dict, user: User | None,
                 dispatcher=None) -> None:
        self.args = args
        self.user = user
        self._dispatcher = dispatcher

    def need_read(self):
        if self.user is None:
            raise PermissionError("Authentication required")

    def need_write(self):
        if self.user is None:
            raise PermissionError("Authentication required")
        if ROLE_HIERARCHY.get(self.user.role, -1) < ROLE_HIERARCHY["write"]:
            raise PermissionError("Write permission required")

    def need_admin(self):
        if self.user is None:
            raise PermissionError("Authentication required")
        if ROLE_HIERARCHY.get(self.user.role, -1) < ROLE_HIERARCHY["admin"]:
            raise PermissionError("Admin permission required")

    def bounded_list(self, key: str, *, required: bool = False) -> list:
        value = self.args.get(key, [])
        if not isinstance(value, list) or (required and not value):
            raise ValueError(f"Missing required argument: {key}")
        if len(value) > _MCP_MAX_LIST_ITEMS:
            raise ValueError(f"Too many {key} (max {_MCP_MAX_LIST_ITEMS})")
        return value

    async def dispatch(self, name: str, args: dict) -> str:
        """Interner Werkzeugaufruf (z.B. related_files -> backlinks)."""
        if self._dispatcher is None:
            raise RuntimeError("kein Dispatcher gesetzt")
        return await self._dispatcher(name, args, self.user)
