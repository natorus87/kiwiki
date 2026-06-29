"""
Einfache, zustandslose Rate-Limiting-Middleware.

Zwei Tier-Grenzen pro Client-IP:
  - login       : 5 Versuche / Minute  (Brute-Force-Schutz)
  - write       : 30 Anfragen / Minute (Schreiboperationen)
  - alles andere: 60 Anfragen / Minute (Lesen / UI / MCP)

Schaltung über KIWIKI_RATE_LIMIT_ENABLED (default "true").
"""

from __future__ import annotations

import logging
import os
import time
from collections import defaultdict
from typing import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

logger = logging.getLogger("kiwiki.rate_limiter")

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

_ENABLED: bool = os.getenv("KIWIKI_RATE_LIMIT_ENABLED", "true").lower() != "false"
_TRUST_PROXY: bool = os.getenv("KIWIKI_TRUST_PROXY", "false").lower() == "true"

_LOGIN_LIMIT: int = int(os.getenv("KIWIKI_LOGIN_LIMIT", "5"))
_LOGIN_WINDOW: int = 60  # Sekunden

_WRITE_LIMIT: int = int(os.getenv("KIWIKI_WRITE_LIMIT", "30"))
_WRITE_WINDOW: int = 60

_READ_LIMIT: int = int(os.getenv("KIWIKI_READ_LIMIT", "60"))
_READ_WINDOW: int = 60

# Statische Pfade werden nie gedrosselt
_STATIC_PATHS: set[str] = {
    "/health",
    "/docs",
    "/openapi.json",
    "/redoc",
}


def _get_client_ip(request: Request) -> str:
    """Ermittle die Client-IP — respektiere X-Forwarded-Header hinter Proxy."""
    forwarded = request.headers.get("X-Forwarded-For", "") if _TRUST_PROXY else ""
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _classify_path(path: str, method: str) -> str:
    """Ordne Pfad+Methode einer der drei Limits zu."""
    if path == "/login" and method == "POST":
        return "login"
    if path.startswith("/api/") and method in ("POST", "PUT", "DELETE", "PATCH"):
        return "write"
    return "read"


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Simple sliding-window rate limiter."""

    def __init__(
        self,
        app,
        defaults: dict[str, tuple[int, int]] | None = None,
    ) -> None:
        super().__init__(app)
        self._limits: dict[str, tuple[int, int]] = defaults or {
            "login": (_LOGIN_LIMIT, _LOGIN_WINDOW),
            "write": (_WRITE_LIMIT, _WRITE_WINDOW),
            "read":  (_READ_LIMIT,  _READ_WINDOW),
        }
        # { (client_ip, tier): [timestamp, ...] }
        self._windows: dict[tuple[str, str], list[float]] = defaultdict(list)
        self._cleanup_threshold = 100  # bei >100 Keys aggressiv aufräumen

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if not _ENABLED:
            return await call_next(request)

        path = request.url.path
        if path in _STATIC_PATHS or path.startswith("/static/"):
            return await call_next(request)

        tier = _classify_path(path, request.method)
        limit, window = self._limits.get(tier, (_READ_LIMIT, _READ_WINDOW))
        client_ip = _get_client_ip(request)
        key = (client_ip, tier)

        now = time.monotonic()
        window_start = now - window

        # Alte Eintraege verwerfen. Wenn die Liste danach leer ist, ist
        # der Key verwaist — loeschen, damit das Dict nicht durch IPs
        # wchst, die einmal anfragten und nie wieder.
        recent = [t for t in self._windows.get(key, []) if t > window_start]
        if recent:
            self._windows[key] = recent
        elif key in self._windows:
            del self._windows[key]

        # Pruefen
        if len(self._windows.get(key, [])) >= limit:
            logger.warning(
                "Rate limit %s exceeded for %s (%s requests in %ds)",
                tier,
                client_ip,
                len(self._windows[key]),
                window,
            )
            reset_at = (
                self._windows[key][0] + window
                if self._windows.get(key)
                else now + window
            )
            response = JSONResponse(
                status_code=429,
                content={
                    "detail": "Zu viele Anfragen. Bitte später erneut versuchen.",
                    "retry_after": max(0, int(reset_at - now)),
                },
                headers={"Retry-After": str(max(0, int(reset_at - now)))},
            )
            return response  # type: ignore[return-value]

        # Zaehler erhohen
        self._windows.setdefault(key, []).append(now)

        # Gelegentlich altes Müll aufräumen
        if len(self._windows) > self._cleanup_threshold:
            self._prune_stale(now)

        return await call_next(request)

    def _prune_stale(self, now: float) -> None:
        # Maximales Window ueber alle Tiers — Keys, deren juengster
        # Timestamp aelter ist, koennen weg.
        cutoff = now - max(w for _, w in self._limits.values())
        # Auch Login-Keys werden geprunt: ein Angreifer, der von vielen
        # IPs /login bombardiert, soll das Dict nicht endlos aufblaehen.
        stale_keys = [
            k for k in self._windows
            if not self._windows[k] or min(self._windows[k]) < cutoff
        ]
        for k in stale_keys:
            del self._windows[k]
