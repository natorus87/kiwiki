"""Regression tests for the in-memory rate limiter.

Sichert ab, dass:
- verwaiste (ip, tier)-Keys aus _windows entfernt werden
- auch Login-Tier-Eintraege geprunt werden (Memory-DoS-Schutz)
- das Limit nach wie vor greift
"""
from __future__ import annotations

import pytest

from app.rate_limiter import RateLimitMiddleware, _classify_path, _get_client_ip


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


def test_prune_stale_preserves_key_with_recent_request():
    mw = RateLimitMiddleware(app=None)
    key = ("10.0.0.5", "login")
    mw._windows[key] = [100.0, 199.0]

    mw._prune_stale(now=200.0)

    assert key in mw._windows
    assert mw._windows[key] == [199.0]


def test_forwarded_for_is_only_used_from_configured_proxy(monkeypatch):
    from app import rate_limiter

    request = _FakeRequest("/login", "POST", client_host="10.0.0.10")
    request.headers = {"X-Forwarded-For": "198.51.100.20, 10.0.0.9"}
    monkeypatch.setattr(rate_limiter, "_TRUST_PROXY", True)
    monkeypatch.setattr(rate_limiter, "_TRUSTED_PROXY_NETWORKS", ())
    assert _get_client_ip(request) == "10.0.0.10"

    monkeypatch.setattr(
        rate_limiter,
        "_TRUSTED_PROXY_NETWORKS",
        rate_limiter._parse_trusted_proxy_networks("10.0.0.0/24"),
    )
    assert _get_client_ip(request) == "198.51.100.20"


def test_oauth_endpoints_use_own_tier():
    """OAuth-Handshake braucht mehr Spielraum als das /login-Bruteforce-Tier
    (Formular-Retry, Token-Exchange, Refresh) — eigenes 'oauth'-Tier statt 'login'."""
    assert _classify_path("/oauth/token", "POST") == "oauth"
    assert _classify_path("/oauth/authorize", "POST") == "oauth"
    assert _classify_path("/oauth/register", "POST") == "oauth"


@pytest.mark.asyncio
async def test_oauth_tier_is_independent_from_login_tier():
    """Regression: Vorher teilten sich /login und /oauth/* dasselbe 5/min-Tier,
    wodurch ein normaler OAuth-Connector-Aufbau (Formular + Retry + Token-Exchange)
    schon die Login-Bruteforce-Grenze sprengte und legitime Autorisierungen mit
    429 blockierte."""
    mw = RateLimitMiddleware(app=None, defaults={"login": (2, 60), "oauth": (5, 60)})
    for _ in range(2):
        req = _FakeRequest("/login", "POST", client_host="10.0.0.20")
        resp = await mw.dispatch(req, _noop)
        assert resp.status_code == 200
    # /login-Tier ist jetzt erschoepft
    req = _FakeRequest("/login", "POST", client_host="10.0.0.20")
    assert (await mw.dispatch(req, _noop)).status_code == 429

    # /oauth/authorize von derselben IP ist davon unberuehrt
    for _ in range(5):
        req = _FakeRequest("/oauth/authorize", "POST", client_host="10.0.0.20")
        req.headers = {"accept": "application/json"}
        resp = await mw.dispatch(req, _noop)
        assert resp.status_code == 200


@pytest.mark.asyncio
async def test_rate_limited_authorize_form_gets_html_error():
    """Ein blockierter Browser-Formular-POST auf /oauth/authorize soll eine
    lesbare HTML-Fehlerseite bekommen statt eines unstyled JSON-Blobs, der auf
    einer sonst leeren Seite wie 'nichts passiert' wirkt."""
    mw = RateLimitMiddleware(app=None, defaults={"oauth": (1, 60)})
    req = _FakeRequest("/oauth/authorize", "POST", client_host="10.0.0.21")
    req.headers = {"accept": "text/html,application/xhtml+xml"}
    assert (await mw.dispatch(req, _noop)).status_code == 200

    blocked = await mw.dispatch(req, _noop)
    assert blocked.status_code == 429
    assert blocked.headers["content-type"].startswith("text/html")
    assert "Zu viele Anfragen" in blocked.body.decode()
    assert "Retry-After" in blocked.headers


@pytest.mark.asyncio
async def test_rate_limited_authorize_json_client_gets_json_error():
    """Programmatische Aufrufer (z.B. ChatGPTs Backend) bekommen weiterhin JSON."""
    mw = RateLimitMiddleware(app=None, defaults={"oauth": (1, 60)})
    req = _FakeRequest("/oauth/authorize", "POST", client_host="10.0.0.22")
    req.headers = {"accept": "application/json"}
    assert (await mw.dispatch(req, _noop)).status_code == 200

    blocked = await mw.dispatch(req, _noop)
    assert blocked.status_code == 429
    assert blocked.headers["content-type"].startswith("application/json")
