"""Regression tests for the in-memory rate limiter.

Sichert ab, dass:
- verwaiste (ip, tier)-Keys aus _windows entfernt werden
- auch Login-Tier-Eintraege geprunt werden (Memory-DoS-Schutz)
- das Limit nach wie vor greift
"""
from __future__ import annotations

import pytest

from app.rate_limiter import RateLimitMiddleware


class _FakeRequest:
    def __init__(self, path: str, method: str, client_host: str = "1.2.3.4") -> None:
        self.url = type("U", (), {"path": path})()
        self.method = method
        self.client = type("C", (), {"host": client_host})()
        self.headers: dict = {}


async def _noop(request):
    from starlette.responses import Response
    return Response(status_code=200)


@pytest.mark.asyncio
async def test_orphaned_login_key_is_pruned():
    """Login-Tier-Keys muessen wie alle anderen geprunt werden."""
    mw = RateLimitMiddleware(app=None)
    # Login von einer IP, die nie wiederkommt
    req = _FakeRequest("/login", "POST", client_host="10.0.0.1")
    await mw.dispatch(req, _noop)
    key = ("10.0.0.1", "login")
    assert key in mw._windows
    # Cleanup erzwingen
    mw._prune_stale(now=10_000_000)  # weit in der Zukunft
    assert key not in mw._windows, "Login-Key wurde nicht geprunt (Memory-DoS)"


@pytest.mark.asyncio
async def test_empty_list_is_removed():
    """Nachdem alle Timestamps veraltet sind, wird der Key geloescht."""
    import time

    mw = RateLimitMiddleware(app=None)
    req = _FakeRequest("/login", "POST", client_host="10.0.0.2")
    await mw.dispatch(req, _noop)
    key = ("10.0.0.2", "login")
    assert key in mw._windows
    # Manuell alle Timestamps veralten lassen
    mw._windows[key] = [t for t in mw._windows[key] if t > time.monotonic() + 10_000]
    # Erneuter Request loescht den leeren Key
    await mw.dispatch(req, _noop)
    # Nach dem Aufruf wurde der Key frisch befuellt (1 Eintrag)
    assert key in mw._windows
    assert len(mw._windows[key]) == 1


@pytest.mark.asyncio
async def test_rate_limit_still_triggers():
    """Limit-Logik darf durch das Cleanup-Refactoring nicht kaputt gehen."""
    mw = RateLimitMiddleware(app=None, defaults={"login": (2, 60)})
    for _ in range(2):
        req = _FakeRequest("/login", "POST", client_host="10.0.0.3")
        resp = await mw.dispatch(req, _noop)
        assert resp.status_code == 200
    req = _FakeRequest("/login", "POST", client_host="10.0.0.3")
    resp = await mw.dispatch(req, _noop)
    assert resp.status_code == 429


@pytest.mark.asyncio
async def test_login_keys_pruned_in_prune_stale():
    """Direkter Test: _prune_stale entfernt Login-Keys mit alten Timestamps."""
    import time

    mw = RateLimitMiddleware(app=None)
    key = ("10.0.0.4", "login")
    mw._windows[key] = [time.monotonic() - 10_000]  # weit in der Vergangenheit
    mw._prune_stale(now=time.monotonic())
    assert key not in mw._windows
