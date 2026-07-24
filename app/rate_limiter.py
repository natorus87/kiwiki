"""
Einfache, prozesslokale Rate-Limiting-Middleware.

Fuenf Tier-Grenzen pro Client-IP:
  - login       : 5 Versuche / Minute  (Brute-Force-Schutz für /login)
  - oauth       : 20 Anfragen / Minute (MCP-OAuth-Handshake: authorize/token/register)
  - write       : 30 Anfragen / Minute (Schreiboperationen)
  - ui          : 240 Anfragen / Minute (HTMX-Fragmente der Weboberflaeche)
  - alles andere: 60 Anfragen / Minute (API-/MCP-Lesezugriffe)

Der OAuth-Handshake braucht ein eigenes, grosszuegigeres Tier: Ein einzelner
Connector-Aufbau (Authorize-Formular, ggf. Tippfehler-Retry, Token-Exchange,
spaetere Refreshes) verbraucht schnell mehr als die 5 Versuche, die fuer
Passwort-Brute-Force am /login-Formular gedacht sind — sonst landen legitime
Nutzer im selben 429 wie ein Angreifer.

Schaltung über KIWIKI_RATE_LIMIT_ENABLED (default "true").
"""

from __future__ import annotations

import html
import logging
import os
import time
import ipaddress
from collections import defaultdict
from typing import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import HTMLResponse, JSONResponse

logger = logging.getLogger("kiwiki.rate_limiter")

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

_ENABLED: bool = os.getenv("KIWIKI_RATE_LIMIT_ENABLED", "true").lower() != "false"
_TRUST_PROXY: bool = os.getenv("KIWIKI_TRUST_PROXY", "false").lower() == "true"


def _parse_trusted_proxy_networks(raw: str) -> tuple:
    networks = []
    for value in raw.split(","):
        value = value.strip()
        if not value:
            continue
        try:
            networks.append(ipaddress.ip_network(value, strict=False))
        except ValueError:
            logger.warning("Ignoring invalid KIWIKI_TRUSTED_PROXY_CIDRS entry: %s", value)
    return tuple(networks)


_TRUSTED_PROXY_NETWORKS = _parse_trusted_proxy_networks(os.getenv("KIWIKI_TRUSTED_PROXY_CIDRS", ""))

_LOGIN_LIMIT: int = int(os.getenv("KIWIKI_LOGIN_LIMIT", "5"))
_LOGIN_WINDOW: int = 60  # Sekunden

_OAUTH_LIMIT: int = int(os.getenv("KIWIKI_OAUTH_LIMIT", "20"))
_OAUTH_WINDOW: int = 60

_WRITE_LIMIT: int = int(os.getenv("KIWIKI_WRITE_LIMIT", "30"))
_WRITE_WINDOW: int = 60

_UI_LIMIT: int = int(os.getenv("KIWIKI_UI_LIMIT", "240"))
_UI_WINDOW: int = 60

_READ_LIMIT: int = int(os.getenv("KIWIKI_READ_LIMIT", "60"))
_READ_WINDOW: int = 60

# Statische Pfade werden nie gedrosselt
_STATIC_PATHS: set[str] = {
    "/health",
    "/livez",
    "/readyz",
    "/docs",
    "/openapi.json",
    "/redoc",
}


def _get_client_ip(request: Request) -> str:
    """Ermittle die Client-IP nur über explizit vertrauenswürdige Proxies."""
    peer = request.client.host if request.client else "unknown"
    if not _TRUST_PROXY or not _TRUSTED_PROXY_NETWORKS:
        return peer
    try:
        peer_ip = ipaddress.ip_address(peer)
    except ValueError:
        return peer
    if not any(peer_ip in network for network in _TRUSTED_PROXY_NETWORKS):
        return peer
    forwarded = request.headers.get("X-Forwarded-For", "")
    if not forwarded:
        return peer
    chain = [value.strip() for value in forwarded.split(",") if value.strip()] + [peer]
    for value in reversed(chain):
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            return peer
        if not any(address in network for network in _TRUSTED_PROXY_NETWORKS):
            return value
    return chain[0]


def _classify_path(path: str, method: str) -> str:
    """Ordne Pfad+Methode einer der vier Limits zu."""
    if path == "/login" and method == "POST":
        return "login"
    if path in {"/oauth/token", "/oauth/authorize", "/oauth/register"} and method == "POST":
        return "oauth"
    if path.startswith("/mcp") and method in ("POST", "PUT", "DELETE", "PATCH"):
        return "write"
    if path.startswith("/oauth/") and method in ("POST", "PUT", "DELETE", "PATCH"):
        return "write"
    if path.startswith("/api/") and method in ("POST", "PUT", "DELETE", "PATCH"):
        return "write"
    if path == "/ui/search" and method == "POST":
        return "ui"
    if path.startswith("/ui/"):
        return "write" if method in ("POST", "PUT", "DELETE", "PATCH") else "ui"
    return "read"


def _build_rate_limit_response(request: Request, retry_after: int) -> Response:
    """429-Antwort passend zum Client: HTML fuer das browser-native
    /oauth/authorize-Formular (sonst landet dort ein unstyled JSON-Blob
    auf sonst leerer Seite und wirkt wie 'nichts passiert'), JSON fuer
    alle anderen (programmatischen) Aufrufer."""
    detail = "Zu viele Anfragen. Bitte später erneut versuchen."
    headers = {"Retry-After": str(retry_after)}

    is_authorize_form = (
        request.url.path == "/oauth/authorize"
        and request.method == "POST"
        and "text/html" in request.headers.get("accept", "")
    )
    if is_authorize_form:
        return HTMLResponse(
            status_code=429,
            headers=headers,
            content=f"""<!DOCTYPE html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>kiwiki – Zu viele Anfragen</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 400px; margin: 80px auto; padding: 0 1rem; }}
    h1 {{ font-size: 1.4rem; margin-bottom: 0.25rem; }}
    p {{ color: #555; font-size: 0.9rem; }}
  </style>
</head>
<body>
  <h1>Zu viele Anfragen</h1>
  <p>{html.escape(detail)} (in ca. {retry_after} Sekunden erneut versuchen.)</p>
</body>
</html>""",
        )
    return JSONResponse(status_code=429, content={"detail": detail, "retry_after": retry_after}, headers=headers)


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
            "oauth": (_OAUTH_LIMIT, _OAUTH_WINDOW),
            "write": (_WRITE_LIMIT, _WRITE_WINDOW),
            "ui":    (_UI_LIMIT,    _UI_WINDOW),
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
            return _build_rate_limit_response(request, max(0, int(reset_at - now)))

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
        for key in list(self._windows):
            recent = [timestamp for timestamp in self._windows[key] if timestamp > cutoff]
            if recent:
                self._windows[key] = recent
            else:
                del self._windows[key]
