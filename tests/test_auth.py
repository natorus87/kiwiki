"""Tests fuer app/auth.py: parse_users(), get_current_user(), require_role()."""

from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.auth import parse_users, get_current_user, require_role, ROLE_HIERARCHY
from app.models import User


class TestParseUsers:
    """parse_users() — Umgebungsvariable parsen und validieren."""

    def test_leere_variable(self, monkeypatch):
        """Keine KIWIKI_USERS → leeres Dict."""
        monkeypatch.delenv("KIWIKI_USERS", raising=False)
        assert parse_users() == {}

    def test_ein_user(self, users_map):
        """Ein korrekter Eintrag wird geparst."""
        result = users_map(("alice", "key1", "read"))
        assert result == {"key1": ("alice", "read")}

    def test_mehrere_users(self, users_map):
        """Mehrere kommagetrennte Eintraege."""
        result = users_map(("alice", "k1", "read"), ("bob", "k2", "write"), ("carol", "k3", "admin"))
        assert len(result) == 3
        assert result["k1"] == ("alice", "read")
        assert result["k2"] == ("bob", "write")
        assert result["k3"] == ("carol", "admin")

    def test_whitespace_wird_gestrippt(self, users_map):
        """Leerzeichen um Felder werden ignoriert."""
        result = users_map((" alice ", " key1 ", " read "))
        assert result["key1"] == ("alice", "read")

    def test_leere_eingaenge_werden_uebersprungen(self, users_map):
        """Zusaetzliche Kommas erzeugen keine Fehler."""
        result = users_map(("a", "k1", "read"))
        # Ein String ",,a:k1:read,," wird durch split(",") leere Strings erzeugen,
        # die durch das `if not entry: continue` uebersprungen werden.
        assert len(result) == 1

    def test_falsche_anzahl_felder(self, monkeypatch):
        """Zuwenig oder zuviele Doppelpunkte → Eintrag wird verworfen."""
        monkeypatch.setenv("KIWIKI_USERS", "alice:key1:read:extra")
        assert parse_users() == {}

    def test_unbekannte_rolle(self, monkeypatch):
        """Ungueltige Rolle → Eintrag wird verworfen."""
        monkeypatch.setenv("KIWIKI_USERS", "alice:key1:editor")
        assert parse_users() == {}

    def test_duplikat_key_letzter_gewinnt(self, users_map):
        """Zwei Eintraege mit gleichem Key → zweiter ueberschreibt ersten."""
        result = users_map(("alice", "samekey", "read"), ("bob", "samekey", "write"))
        assert result["samekey"] == ("bob", "write")

    def test_unvalid_username(self, monkeypatch):
        """Username mit Sonderzeichen → verworfen."""
        monkeypatch.setenv("KIWIKI_USERS", "al ice:key1:read")
        assert parse_users() == {}

    def test_rolle_hierarchie(self):
        """ROLE_HIERARCHY hat die erwarteten Werte."""
        assert ROLE_HIERARCHY == {"read": 0, "write": 1, "admin": 2}


class TestGetCurrentUser:
    """get_current_user() — Bearer-Token aus Request-Header extrahieren."""

    @pytest.fixture
    def _mock_request(self, users_map):
        users_map(("alice", "tok1", "read"))

    async def test_bearer_token_gueltig(self, _mock_request):
        request = AsyncMock()
        request.headers = {"Authorization": "Bearer tok1"}
        user = await get_current_user(request)
        assert user.username == "alice"
        assert user.role == "read"

    async def test_bearer_token_unbekannt(self, _mock_request):
        request = AsyncMock()
        request.headers = {"Authorization": "Bearer unbekannt"}
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(request)
        assert exc_info.value.status_code == 401

    async def test_kein_authorization_header(self):
        request = AsyncMock()
        request.headers = {}
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(request)
        assert exc_info.value.status_code == 401

    async def test_falsches_auth_format(self):
        request = AsyncMock()
        request.headers = {"Authorization": "Basic tok1"}
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(request)
        assert exc_info.value.status_code == 401


class TestRequireRole:
    """require_role() — Rollen-Pruefung ueber FastAPI-Dependency-Factory."""

    async def test_read_erlaubt(self, users_map):
        dep = require_role("read")
        user = User(username="alice", role="read")
        result = await dep(user)
        assert result.role == "read"

    async def test_write_braucht_write(self, users_map):
        dep = require_role("write")
        user = User(username="alice", role="read")
        with pytest.raises(HTTPException) as exc_info:
            await dep(user)
        assert exc_info.value.status_code == 403
        assert "write" in str(exc_info.value.detail)

    async def test_admin_braucht_admin(self, users_map):
        dep = require_role("admin")
        user = User(username="bob", role="write")
        with pytest.raises(HTTPException) as exc_info:
            await dep(user)
        assert exc_info.value.status_code == 403

    async def test_admin_darf_admin(self, users_map):
        dep = require_role("admin")
        user = User(username="carol", role="admin")
        result = await dep(user)
        assert result.role == "admin"
