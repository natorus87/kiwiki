"""Tests fuer den lokalen User-Store.

Die Luecken der Abdeckung lagen genau in der Fehlerbehandlung — und dort
liegen die Rollen- und Pfadvalidierungen, die die frueheren Security-Reviews
abgesichert haben. Ein lokaler User, der den API-Key eines Admins uebernimmt,
waere eine Rechteausweitung; `remove_workspace_for_user` loescht rekursiv.
"""

from __future__ import annotations

import pytest

from app import user_store as store_mod
from app.user_store import (
    create_local_user,
    delete_local_user,
    generate_api_key,
    local_users_by_key,
    remove_workspace_for_user,
    users_by_key,
)


@pytest.fixture(autouse=True)
def _diagnostics_reset():
    """Die Diagnose-Flags global begrenzen das Logging auf einen Eintrag."""
    store_mod._LOCAL_DIAG_LOGGED = False
    store_mod._MERGE_DIAG_LOGGED = False
    yield
    store_mod._LOCAL_DIAG_LOGGED = False
    store_mod._MERGE_DIAG_LOGGED = False


@pytest.fixture
def users_file(monkeypatch, tmp_path):
    path = tmp_path / "users.yaml"
    monkeypatch.setattr(store_mod, "_users_file", lambda: path)
    return path


def _write(path, payload: str) -> None:
    path.write_text(payload, encoding="utf-8")


# ── Unlesbare Datei (117-128) ──────────────────────────────────────────────

def test_unlesbare_datei_liefert_leere_und_loggt(users_file, caplog, monkeypatch):
    users_file.write_text("::: kaputt : [\n", encoding="utf-8")
    store_mod._LOCAL_DIAG_LOGGED = False

    with caplog.at_level("ERROR"):
        assert local_users_by_key() == {}

    assert any("cannot read" in record.getMessage() for record in caplog.records), (
        "eine unlesbare Datei muss benannt werden, nicht still ignoriert"
    )


def test_ohne_users_liste_wird_abgewiesen(users_file, caplog):
    # Valides YAML, aber die Struktur ist falsch: ein Mapping statt einer
    # Liste. Genau der Fall, den der Store abweisen soll.
    _write(users_file, "users:\n  admin:\n    key: k\n    role: read\n")

    with caplog.at_level("ERROR"):
        assert local_users_by_key() == {}

    assert any("top-level users list" in record.getMessage() for record in caplog.records)


# ── Ungueltige Eintraege (134-156) ─────────────────────────────────────────

@pytest.mark.parametrize(
    ("payload", "erwartet"),
    [
        ("users:\n  - kein-mapping\n", "expected mapping"),
        ("users:\n  - {username: '', key: 'k', role: 'read'}\n", "empty username"),
        ("users:\n  - {username: 'u', key: '', role: 'read'}\n", "empty username"),
        ("users:\n  - {username: 'u', key: 'k', role: ''}\n", "empty username"),
        ("users:\n  - {username: 'u', key: 'k', role: 'superuser'}\n", "unknown role"),
        ("users:\n  - {username: 'mit leerzeichen', key: 'k', role: 'read'}\n", "invalid username"),
    ],
)
def test_ungueltige_eintraege_werden_abgewiesen(users_file, caplog, payload, erwartet):
    _write(users_file, payload)

    with caplog.at_level("WARNING"):
        assert local_users_by_key() == {}, "ein ungueltiger Eintrag darf nicht teilweise geladen werden"

    assert any(erwartet in record.getMessage() for record in caplog.records), (
        f"Diagnose muss {erwartet!r} nennen"
    )


def test_doppelter_lokaler_key_wird_abgewiesen(users_file, caplog):
    _write(
        users_file,
        "users:\n"
        "  - {username: 'alice', key: 'gleicherkey', role: 'read'}\n"
        "  - {username: 'bob', key: 'gleicherkey', role: 'write'}\n",
    )

    with caplog.at_level("WARNING"):
        result = local_users_by_key()

    assert len(result) == 1, "nur der erste Eintrag darf gewinnen"
    assert any("duplicate local API key" in record.getMessage() for record in caplog.records)


def test_gueltige_eintraege_werden_alle_geladen(users_file):
    _write(
        users_file,
        "users:\n"
        "  - {username: 'alice', key: 'k1', role: 'read'}\n"
        "  - {username: 'bob', key: 'k2', role: 'write'}\n",
    )

    result = local_users_by_key()

    assert {r.username for r in result.values()} == {"alice", "bob"}
    assert all(r.source == "local" for r in result.values())


# ── Merge-Kollisionen mit Builtin-Usern (168-181) ──────────────────────────

def test_lokaler_user_darf_nicht_den_key_eines_builtins_uebernehmen(users_file, caplog, monkeypatch):
    monkeypatch.setenv("KIWIKI_USERS", "admin:adminschluessel:admin")
    _write(users_file, "users:\n  - {username: 'angreifer', key: 'adminschluessel', role: 'admin'}\n")
    store_mod._MERGE_DIAG_LOGGED = False

    with caplog.at_level("WARNING"):
        result = users_by_key()

    assert result["adminschluessel"].username == "admin", "der Builtin muss gewinnen"
    assert result["adminschluessel"].source == "builtin"
    assert any("collides with builtin" in record.getMessage() for record in caplog.records), (
        "eine Rechteausweitung ueber eine Key-Kollision muss protokolliert werden"
    )


def test_lokaler_user_mit_builtin_namen_wird_ignoriert(users_file, caplog, monkeypatch):
    monkeypatch.setenv("KIWIKI_USERS", "admin:adminschluessel:admin")
    _write(users_file, "users:\n  - {username: 'admin', key: 'andererkey', role: 'read'}\n")
    store_mod._MERGE_DIAG_LOGGED = False

    with caplog.at_level("WARNING"):
        result = users_by_key()

    assert "andererkey" not in result, "der lokale User darf den Namen nicht uebernehmen"
    assert any("username is builtin" in record.getMessage() for record in caplog.records)


# ── create_local_user Validierung (225-238) ───────────────────────────────

@pytest.mark.parametrize(
    ("username", "key", "role", "erwartet"),
    [
        ("mit leerzeichen", "k123456", "read", "[a-zA-Z0-9_-]"),
        ("ok", "", "read", "nicht leer"),
        ("ok", "mit,komma", "read", "Kommas"),
        ("ok", "mit:doppelpunkt", "read", "Doppelpunkte"),
        ("ok", "k123456", "superuser", "Unbekannte Rolle"),
    ],
)
def test_create_local_user_weist_eingaben_zurueck(users_file, username, key, role, erwartet):
    with pytest.raises(ValueError, match=erwartet):
        create_local_user(username, key, role)


def test_create_local_user_weist_doppelten_key_zurueck(users_file, monkeypatch):
    monkeypatch.setenv("KIWIKI_USERS", "admin:belegterkey:admin")

    with pytest.raises(ValueError, match="bereits verwendet"):
        create_local_user("neu", "belegterkey", "read")


def test_create_local_user_weist_doppelten_namen_zurueck(users_file, monkeypatch):
    monkeypatch.setenv("KIWIKI_USERS", "admin:adminschluessel:admin")

    with pytest.raises(ValueError, match="existiert bereits"):
        create_local_user("admin", "neuerkey12345", "read")


def test_create_local_user_schreibt_atomar(users_file, monkeypatch):
    monkeypatch.setenv("KIWIKI_USERS", "admin:adminschluessel:admin")

    record = create_local_user("neu", "neuerkey12345", "write")

    assert record.source == "local"
    gespeichert = local_users_by_key()
    assert "neuerkey12345" in gespeichert
    assert gespeichert["neuerkey12345"].role == "write"
    assert users_file.stat().st_mode & 0o777 == 0o600, "die User-Datei traegt nur fuer den Owner"


# ── remove_workspace_for_user (250-258) ───────────────────────────────────

def test_remove_workspace_loescht_rekursiv(monkeypatch, tmp_path):
    from app import tenancy

    monkeypatch.setattr(tenancy, "base_data_dir", lambda: tmp_path)
    workspace = tmp_path / "alice"
    (workspace / "notes").mkdir(parents=True)
    (workspace / "notes" / "wichtig.md").write_text("nicht verlieren", encoding="utf-8")

    remove_workspace_for_user("alice")

    assert not workspace.exists(), "der Workspace muss restlos weg sein"


def test_remove_workspace_ignoriert_fehlenden_nutzer(monkeypatch, tmp_path):
    from app import tenancy

    monkeypatch.setattr(tenancy, "base_data_dir", lambda: tmp_path)

    remove_workspace_for_user("gibtesnicht")  # darf nicht werfen


@pytest.mark.parametrize("username", ["../etc", "mit/ slash", "", "x" * 65])
def test_remove_workspace_weist_traversal_zurueck(username, monkeypatch, tmp_path):
    """Der Pfad wird aus dem Namen gebaut — ein Name ausserhalb des
    Musters darf ihn nicht verlassen duerfen."""
    from app import tenancy

    monkeypatch.setattr(tenancy, "base_data_dir", lambda: tmp_path)
    ausserhalb = tmp_path.parent / "wichtiger-ordner"
    ausserhalb.mkdir(exist_ok=True)

    with pytest.raises(ValueError, match="Invalid username"):
        remove_workspace_for_user(username)

    assert ausserhalb.exists(), "darf nicht angefasst werden"


# ── delete_local_user (264-272) ────────────────────────────────────────────

def test_delete_local_user_weist_unbekannten_zurueck(users_file, monkeypatch):
    monkeypatch.setenv("KIWIKI_USERS", "admin:adminschluessel:admin")
    _write(users_file, "users:\n  - {username: 'alice', key: 'k1', role: 'read'}\n")

    with pytest.raises(FileNotFoundError, match="nicht gefunden"):
        delete_local_user("bob")


def test_delete_local_user_schuetzt_builtin(users_file, monkeypatch):
    monkeypatch.setenv("KIWIKI_USERS", "admin:adminschluessel:admin")

    with pytest.raises(ValueError, match="Builtin"):
        delete_local_user("admin")


def test_delete_local_user_entfernt_nur_die_zieldatei(users_file, monkeypatch):
    monkeypatch.setenv("KIWIKI_USERS", "admin:adminschluessel:admin")
    _write(
        users_file,
        "users:\n"
        "  - {username: 'alice', key: 'k1', role: 'read'}\n"
        "  - {username: 'bob', key: 'k2', role: 'write'}\n",
    )

    delete_local_user("alice")

    verbleibend = local_users_by_key()
    assert "k1" not in verbleibend
    assert "k2" in verbleibend, "bob muss unberuehrt bleiben"


# ── generate_api_key (189-195) ─────────────────────────────────────────────

def test_generate_api_key_ist_eindeutig(monkeypatch):
    monkeypatch.setenv("KIWIKI_USERS", "admin:adminschluessel:admin")

    key = generate_api_key()

    assert key, "es muss ein Key entstehen"
    assert key not in users_by_key(), (
        "ein generierter Key gehoert niemandem, sonst waere er ein versteckter Zweitzugang"
    )


def test_generate_api_key_gibt_nach_10_kollisionen_auf(monkeypatch):
    monkeypatch.setenv("KIWIKI_USERS", "admin:belegterkey:admin")
    monkeypatch.setattr(store_mod.secrets, "token_urlsafe", lambda n: "belegterkey")

    with pytest.raises(RuntimeError, match="unique API key"):
        generate_api_key()


# ── builtin_users_by_key: Env-Parsing (44, 49, 59-60) ─────────────────────

def test_leere_env_liefert_leeres_ergebnis(monkeypatch):
    monkeypatch.setenv("KIWIKI_USERS", "")
    store_mod._PARSE_DIAG_LOGGED = False

    assert store_mod.builtin_users_by_key() == {}


def test_leere_eintraege_in_der_env_werden_uebersprungen(monkeypatch):
    """`a:k:read,,,b:k2:read` — Leerabschnitte sind kein Fehler."""
    monkeypatch.setenv("KIWIKI_USERS", "alice:k1:read,,,bob:k2:read")
    store_mod._PARSE_DIAG_LOGGED = False

    result = store_mod.builtin_users_by_key()

    assert {r.username for r in result.values()} == {"alice", "bob"}


def test_leere_felder_in_der_env_werden_diagnostiziert(monkeypatch, caplog):
    monkeypatch.setenv("KIWIKI_USERS", "alice::read")
    store_mod._PARSE_DIAG_LOGGED = False

    with caplog.at_level("WARNING"):
        assert store_mod.builtin_users_by_key() == {}

    assert any("empty username, key or role" in r.getMessage() for r in caplog.records)


def test_doppelter_builtin_key_ueberschreibt_und_wird_gememeldet(monkeypatch, caplog):
    """Bewusste Semantik, aber sie muss sichtbar sein.

    Anders als im lokalen Store gewinnt hier der LETZTE Eintrag: ein doppelter
    Key in KIWIKI_USERS ueberschreibt den vorherigen User still. Das ist eine
    Betriebsentscheidung, kein Versehen — deshalb wird es protokolliert, aber
    nicht abgewiesen.
    """
    monkeypatch.setenv("KIWIKI_USERS", "erster:shared:admin,zweiter:shared:read")
    store_mod._PARSE_DIAG_LOGGED = False

    with caplog.at_level("WARNING"):
        result = store_mod.builtin_users_by_key()

    assert result["shared"].username == "zweiter", "der letzte Eintrag gewinnt"
    assert result["shared"].role == "read"
    assert any("duplicate API key" in r.getMessage() for r in caplog.records), (
        "ein Ueberschreiben einer Rolle muss im Log stehen"
    )


def test_delete_local_user_weist_traversal_zurueck(users_file, monkeypatch):
    monkeypatch.setenv("KIWIKI_USERS", "admin:adminschluessel:admin")

    with pytest.raises(ValueError, match="Ungültiger Benutzername"):
        delete_local_user("../etc")
