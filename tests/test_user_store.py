from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import user_store


@pytest.fixture(autouse=True)
def _disable_rate_limit_for_user_api_tests(monkeypatch):
    """Diese Tests pruefen Auth/User-Semantik, nicht das globale IP-Limit."""
    from app import rate_limiter

    monkeypatch.setattr(rate_limiter, "_ENABLED", False)


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


def test_rest_write_api_rejects_non_markdown_and_system_paths(monkeypatch):
    monkeypatch.setenv("KIWIKI_USERS", "admin:admin-key:admin")
    from app.main import app

    with TestClient(app) as client:
        _login_as_admin(client)

        text_file = client.put(
            "/api/file",
            json={"path": "notes/readme.txt", "content": "plain text"},
        )
        assert text_file.status_code == 400
        assert "Only .md files" in text_file.json()["detail"]

        system_file = client.put(
            "/api/file",
            json={"path": ".kiwiki/users.md", "content": "system"},
        )
        assert system_file.status_code == 400
        assert ".kiwiki" in system_file.json()["detail"]

        system_folder = client.post("/api/folder", json={"path": ".kiwiki/tmp"})
        assert system_folder.status_code == 400
        assert ".kiwiki" in system_folder.json()["detail"]


def _login_as_admin(client) -> None:
    """Register an admin session without consuming the shared login rate limit."""
    from app import session_store

    record = session_store.create_session("admin", "admin", "admin-key")
    client.cookies.set("kiwiki_session", record.token)


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


def test_reader_cannot_rename_file_via_ui(monkeypatch, tmp_path):
    monkeypatch.setenv("KIWIKI_USERS", "reader:reader-key:read")
    from app.main import app
    from app import session_store
    from app.tenancy import ensure_user_workspace

    workspace = ensure_user_workspace("reader")
    source = workspace / "notes" / "original.md"
    source.write_text("---\ntitle: Original\n---\n\nBody", encoding="utf-8")

    with TestClient(app) as client:
        record = session_store.create_session("reader", "read", "reader-key")
        client.cookies.set("kiwiki_session", record.token)
        response = client.post(
            "/ui/rename",
            data={"old_path": "notes/original.md", "new_path": "notes/renamed.md"},
        )

    assert response.status_code == 403
    assert source.exists()
    assert not (workspace / "notes" / "renamed.md").exists()


def test_deleted_user_sessions_are_revoked(monkeypatch):
    monkeypatch.setenv("KIWIKI_USERS", "admin:admin-key:admin")
    from app.main import app
    from app import session_store

    with TestClient(app) as client:
        _login_as_admin(client)
        created = client.post(
            "/api/users",
            json={"username": "bob", "key": "bob-key", "role": "read"},
        )
        assert created.status_code == 200
        bob_session = session_store.create_session("bob", "read", "bob-key")

        deleted = client.delete("/api/users/bob")

    assert deleted.status_code == 200
    assert session_store.lookup_session(bob_session.token) is None


def test_session_role_is_revalidated_against_current_user_store(monkeypatch):
    monkeypatch.setenv("KIWIKI_USERS", "admin:admin-key:admin")
    user_store.create_local_user("bob", "bob-key", "read")
    from app.main import app
    from app import session_store

    with TestClient(app) as client:
        stale = session_store.create_session("bob", "admin", "bob-key")
        client.cookies.set("kiwiki_session", stale.token)
        response = client.post("/api/folder", json={"path": "notes/forbidden"})

    assert response.status_code == 401
    assert session_store.lookup_session(stale.token) is None


def test_failed_user_creation_preserves_preexisting_workspace(monkeypatch, tmp_path):
    monkeypatch.setenv("KIWIKI_USERS", "admin:admin-key:admin")
    from app.main import app
    from app.tenancy import ensure_user_workspace

    workspace = ensure_user_workspace("existing")
    sentinel = workspace / "notes" / "keep.md"
    sentinel.write_text("wichtige Daten", encoding="utf-8")

    def fail_persist(*_args, **_kwargs):
        raise RuntimeError("simulierter Persistenzfehler")

    monkeypatch.setattr(user_store, "create_local_user", fail_persist)
    with TestClient(app) as client:
        _login_as_admin(client)
        response = client.post(
            "/api/users",
            json={"username": "existing", "key": "existing-key", "role": "write"},
        )

    assert response.status_code == 400
    assert sentinel.read_text(encoding="utf-8") == "wichtige Daten"


def test_user_removed_outside_endpoint_cannot_keep_ui_session(monkeypatch):
    monkeypatch.setenv("KIWIKI_USERS", "admin:admin-key:admin")
    user_store.create_local_user("bob", "bob-key", "read")
    from app.main import app
    from app import session_store

    stale = session_store.create_session("bob", "read", "bob-key")
    user_store.delete_local_user("bob")
    with TestClient(app, follow_redirects=False) as client:
        client.cookies.set("kiwiki_session", stale.token)
        response = client.get("/")

    assert response.status_code == 302
    assert response.headers["location"] == "/login"
    assert session_store.lookup_session(stale.token) is None


def test_folder_delete_removes_all_search_index_entries(monkeypatch):
    monkeypatch.setenv("KIWIKI_USERS", "admin:admin-key:admin")
    from app.main import app
    from app.search import index_file, init_db, search
    from app.tenancy import ensure_user_workspace, set_user_ns

    workspace = ensure_user_workspace("admin")
    folder = workspace / "notes" / "obsolete"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "one.md").write_text(
        "---\ntitle: One\n---\n\nEindeutigerOrdnerTreffer", encoding="utf-8"
    )
    (folder / "two.md").write_text(
        "---\ntitle: Two\n---\n\nEindeutigerOrdnerTreffer", encoding="utf-8"
    )
    set_user_ns("admin")
    init_db()
    index_file("notes/obsolete/one.md")
    index_file("notes/obsolete/two.md")
    assert len(search("EindeutigerOrdnerTreffer")) == 2

    with TestClient(app) as client:
        _login_as_admin(client)
        response = client.delete("/api/folder", params={"path": "notes/obsolete"})

    set_user_ns("admin")
    assert response.status_code == 200
    assert search("EindeutigerOrdnerTreffer") == []
