from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import user_store


def test_create_local_user_is_persisted_and_combined_with_builtin(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("KIWIKI_USERS", "admin:builtin-key:admin")

    created = user_store.create_local_user("alice", "alice-key", "write")

    assert created.source == "local"
    assert created.username == "alice"
    users = user_store.users_by_key()
    assert users["builtin-key"].source == "builtin"
    assert users["alice-key"].source == "local"
    assert (tmp_path / ".kiwiki" / "users.yaml").exists()


def test_local_user_cannot_shadow_builtin_username(monkeypatch):
    monkeypatch.setenv("KIWIKI_USERS", "admin:builtin-key:admin")

    with pytest.raises(ValueError, match="Benutzername existiert"):
        user_store.create_local_user("admin", "other-key", "read")


def test_builtin_user_cannot_be_deleted(monkeypatch):
    monkeypatch.setenv("KIWIKI_USERS", "admin:builtin-key:admin")

    with pytest.raises(ValueError, match="Builtin"):
        user_store.delete_local_user("admin")


def test_generate_api_key_is_usable_and_unique(monkeypatch):
    monkeypatch.setenv("KIWIKI_USERS", "admin:builtin-key:admin")

    first = user_store.generate_api_key()
    second = user_store.generate_api_key()

    assert first != second
    assert len(first) >= 32
    assert ":" not in first
    assert "," not in first


def test_admin_api_manages_local_users(monkeypatch):
    monkeypatch.setenv("KIWIKI_USERS", "admin:admin-key:admin")
    from app.main import app

    with TestClient(app) as client:
        _login_as_admin(client)

        create = client.post(
            "/api/users",
            json={"username": "bob", "key": "bob-key", "role": "read"},
        )
        assert create.status_code == 200, f"Create failed: {create.text}"
        assert create.json()["source"] == "local"

        listed = client.get("/api/users")
        assert listed.status_code == 200
        users = listed.json()
        assert {"username": "admin", "role": "admin", "source": "builtin", "builtin": True} in users
        assert {"username": "bob", "role": "read", "source": "local", "builtin": False} in users

        generated = client.post("/api/users/generate-key")
        assert generated.status_code == 200
        key = generated.json()["key"]
        assert len(key) >= 32
        assert ":" not in key
        assert "," not in key

        forbidden = client.delete("/api/users/admin")
        assert forbidden.status_code == 400

        deleted = client.delete("/api/users/bob")
        assert deleted.status_code == 200
        assert "bob-key" not in user_store.users_by_key()


def _login_as_admin(client) -> None:
    """Login als 'admin' und Cookie explizit registrieren, damit
    Folge-Requests das Session-Token mitsenden."""
    r = client.post("/login", data={"api_key": "admin-key"}, follow_redirects=False)
    assert r.status_code == 303, f"Login failed: {r.text}"
    token = client.cookies.get("kiwiki_session")
    assert token is not None
    # TestClient schickt client.cookies erst, nachdem das Cookie explizit
    # gesetzt wurde. Ohne diesen Re-Set gehen Folge-Requests ohne Cookie raus.
    client.cookies.set("kiwiki_session", token)


def test_create_user_workspace_persisted_on_success(monkeypatch, tmp_path):
    """Happy-Path: User und Workspace werden beide angelegt."""
    monkeypatch.setenv("KIWIKI_USERS", "admin:admin-key:admin")
    from app.main import app

    with TestClient(app) as client:
        _login_as_admin(client)
        r = client.post(
            "/api/users",
            json={"username": "carol", "key": "carol-key", "role": "write"},
        )
        assert r.status_code == 200
        # User-Eintrag persistiert
        assert "carol-key" in user_store.users_by_key()
        # Workspace existiert
        assert (tmp_path / "carol").exists()
        # DB ist initialisiert
        assert (tmp_path / "carol" / ".kiwiki" / "index.sqlite").exists()


def test_create_user_rolls_back_workspace_on_duplicate_key(monkeypatch, tmp_path):
    """Wenn der User-Eintrag scheitert (Duplikat), darf kein leerer
    Workspace zurueckbleiben."""
    monkeypatch.setenv("KIWIKI_USERS", "admin:admin-key:admin")
    from app.main import app

    with TestClient(app) as client:
        _login_as_admin(client)
        # Ersten User anlegen (sollte klappen)
        r1 = client.post(
            "/api/users",
            json={"username": "dave", "key": "dave-key", "role": "read"},
        )
        assert r1.status_code == 200
        # Zweiter Versuch mit gleichem Key, aber neuem Username
        r2 = client.post(
            "/api/users",
            json={"username": "dave2", "key": "dave-key", "role": "read"},
        )
        assert r2.status_code == 400
        # Wichtig: kein leerer Workspace fuer "dave2" hinterlassen
        assert not (tmp_path / "dave2").exists(), (
            "Workspace fuer fehlgeschlagenen User-Insert wurde nicht aufgeraeumt"
        )


def test_create_user_rolls_back_workspace_on_duplicate_username(monkeypatch, tmp_path):
    """Selbe Garantie fuer doppelten Username."""
    monkeypatch.setenv("KIWIKI_USERS", "admin:admin-key:admin")
    from app.main import app

    with TestClient(app) as client:
        _login_as_admin(client)
        client.post(
            "/api/users",
            json={"username": "eve", "key": "eve-key", "role": "read"},
        )
        r2 = client.post(
            "/api/users",
            json={"username": "eve", "key": "eve-key-2", "role": "read"},
        )
        assert r2.status_code == 400
        assert not (tmp_path / "eve" / "AGENTS.md.tmp").exists()


def test_create_user_validation_error_no_workspace(monkeypatch, tmp_path):
    """Validierungsfehler hinterlassen keinen halben Workspace."""
    monkeypatch.setenv("KIWIKI_USERS", "admin:admin-key:admin")
    from app.main import app

    with TestClient(app) as client:
        _login_as_admin(client)
        # Ungueltiger Username (zu lang, mit Sonderzeichen)
        r = client.post(
            "/api/users",
            json={"username": "evil name!", "key": "k1", "role": "read"},
        )
        assert r.status_code == 400
        assert not (tmp_path / "evil name!").exists()
