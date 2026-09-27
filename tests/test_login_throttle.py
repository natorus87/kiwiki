"""Regressionstest fuer den Login-Throttle.

`POST /login` liegt im login-Tier der RateLimitMiddleware (5 Versuche / 60 s).
Zwei Eigenschaften muessen zusammen gelten:

* Falsche Keys werden gedrosselt — das Formular darf Keys nicht beliebig oft
  durchprobieren lassen.
* Ein Erfolg gibt das Budget wieder frei. Vorher zaehlte die Middleware jeden
  /login-POST dauerhaft, also auch die erfolgreichen: wer sich zweimal
  vertippt hatte, kam danach 60 Sekunden lang nicht mehr rein, auch mit dem
  richtigen Key.

`_failed_key_attempts` (`KIWIKI_KEY_ATTEMPT_LIMIT`) bleibt davon unberuehrt: das
gilt /oauth/authorize, das im weiteren oauth-Tier liegt und dort schon vor dem
Handler greift — siehe tests/test_rate_limiter.py.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.rate_limiter import RateLimitMiddleware

VALID_KEY = "adminkey123456789012"
WRONG_KEY = "wrongkey123456789012"
LOGIN_LIMIT = 5


def _limiter() -> RateLimitMiddleware:
    """Die RateLimitMiddleware-Instanz im gebauten Starlette-Stack finden."""
    node = app.middleware_stack
    while node is not None:
        if isinstance(node, RateLimitMiddleware):
            return node
        node = getattr(node, "app", None)
    raise AssertionError("RateLimitMiddleware nicht im Stack gefunden")


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("KIWIKI_USERS", f"admin:{VALID_KEY}:admin")
    monkeypatch.setenv("KIWIKI_RATE_LIMIT_ENABLED", "true")
    from app import auth as auth_mod

    auth_mod._PARSE_DIAG_LOGGED = False
    with TestClient(app) as test_client:
        _limiter()._windows.clear()
        yield test_client
        _limiter()._windows.clear()


def test_a_wrong_key_is_throttled_after_the_limit(client):
    for attempt in range(LOGIN_LIMIT):
        response = client.post("/login", data={"api_key": WRONG_KEY})
        assert response.status_code == 401, f"Versuch {attempt + 1} sollte 401 sein"

    response = client.post("/login", data={"api_key": WRONG_KEY})
    assert response.status_code == 429, "nach dem Limit muss /login drosseln"


def test_a_successful_login_releases_the_budget(client):
    """Wer den richtigen Key hat, darf nach Fehlversuchen nicht ausgesperrt sein."""
    for _ in range(LOGIN_LIMIT):
        client.post("/login", data={"api_key": WRONG_KEY})

    response = client.post("/login", data={"api_key": VALID_KEY}, follow_redirects=False)
    assert response.status_code == 303, "der korrekte Key muss gelten"
    assert ("testclient", "login") not in _limiter()._windows, (
        "nach dem Erfolg muss das Login-Fenster der Quelle frei sein"
    )

    # Und es zaehlt wieder von vorn: LOGIN_LIMIT Fehlversuche sind erlaubt,
    # der naechste wird gedrosselt.
    statuses = [client.post("/login", data={"api_key": WRONG_KEY}).status_code for _ in range(LOGIN_LIMIT + 1)]
    assert statuses == [401] * LOGIN_LIMIT + [429], f"nach dem Login muss das Budget neu zaehlen, war: {statuses}"
