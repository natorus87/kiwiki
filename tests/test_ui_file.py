"""Regression-Tests fuer app/main.py — UI-Endpoints.

Sichert das Rendering der file_view-Partial ab: Edit-Button muss fuer
'write' und 'admin' erscheinen, fuer 'read' nicht. Hintergrund: ein
frueherer Bug liess 'user' nicht in den Template-Kontext fliessen, so
dass der Edit-Button fuer niemanden sichtbar war (auch nicht auf Mobile,
wo er am meisten auffaellt).

Zusaetzlich: Tags als klickbare Buttons, Tag-Suche via 'tag:<value>'
und Such-Empty-State.
"""

from fastapi.testclient import TestClient

from app.main import app


def _login_and_fetch(users: tuple[tuple[str, str, str], ...], file_owner: str, key: str, path: str) -> str:
    """Baut die User-Map auf, loggt sich ein und holt /ui/file."""
    import os

    os.environ["KIWIKI_USERS"] = ",".join(f"{u}:{k}:{r}" for u, k, r in users)
    from app import auth as auth_mod
    from app import user_store as user_store_mod

    auth_mod._PARSE_DIAG_LOGGED = False
    user_store_mod._PARSE_DIAG_LOGGED = False
    user_store_mod._LOCAL_DIAG_LOGGED = False
    user_store_mod._MERGE_DIAG_LOGGED = False

    client = TestClient(app)
    client.post("/login", data={"api_key": key}, follow_redirects=False)
    resp = client.get(f"/ui/file?path={path}")
    assert resp.status_code == 200, resp.text
    return resp.text


def test_ui_file_zeigt_edit_button_fuer_admin(tmp_path, monkeypatch):
    """Admin sieht Edit-, Export- und Delete-Button."""
    from app.tenancy import ensure_user_workspace

    monkeypatch.setenv("KIWIKI_USERS", "admin:adminkey:admin")
    ws = ensure_user_workspace("admin")
    (ws / "notes").mkdir(parents=True, exist_ok=True)
    (ws / "notes" / "demo.md").write_text("---\ntitle: Demo\n---\n\nHallo", encoding="utf-8")

    body = _login_and_fetch(
        users=(("admin", "adminkey", "admin"),),
        file_owner="admin",
        key="adminkey",
        path="notes/demo.md",
    )
    assert "openEditor(" in body
    assert 'class="btn btn-ghost" onclick="kwExportFile(' in body
    assert "deleteFile(" in body


def test_ui_file_zeigt_edit_button_fuer_write(tmp_path, monkeypatch):
    """Write-Rolle sieht Edit + Export, aber KEIN Delete."""
    from app.tenancy import ensure_user_workspace

    monkeypatch.setenv("KIWIKI_USERS", "alice:writekey:write")
    ws = ensure_user_workspace("alice")
    (ws / "notes").mkdir(parents=True, exist_ok=True)
    (ws / "notes" / "demo.md").write_text("---\ntitle: Demo\n---\n\nHallo", encoding="utf-8")

    body = _login_and_fetch(
        users=(("alice", "writekey", "write"),),
        file_owner="alice",
        key="writekey",
        path="notes/demo.md",
    )
    assert "openEditor(" in body, "Edit-Button fehlt fuer write-Rolle (Regressionsschutz)"
    assert "kwExportFile(" in body
    assert "deleteFile(" not in body


def test_ui_file_zeigt_kein_edit_button_fuer_read(tmp_path, monkeypatch):
    """Read-Rolle sieht nur Export, kein Edit und kein Delete."""
    from app.tenancy import ensure_user_workspace

    monkeypatch.setenv("KIWIKI_USERS", "bob:readkey:read")
    ws = ensure_user_workspace("bob")
    (ws / "notes").mkdir(parents=True, exist_ok=True)
    (ws / "notes" / "demo.md").write_text("---\ntitle: Demo\n---\n\nHallo", encoding="utf-8")

    body = _login_and_fetch(
        users=(("bob", "readkey", "read"),),
        file_owner="bob",
        key="readkey",
        path="notes/demo.md",
    )
    assert "openEditor(" not in body
    assert "deleteFile(" not in body


def _login(users, key):
    """Baut User-Map, loggt ein und gibt den TestClient zurück."""
    import os

    os.environ["KIWIKI_USERS"] = ",".join(f"{u}:{k}:{r}" for u, k, r in users)
    from app import auth as auth_mod
    from app import user_store as user_store_mod

    auth_mod._PARSE_DIAG_LOGGED = False
    user_store_mod._PARSE_DIAG_LOGGED = False
    user_store_mod._LOCAL_DIAG_LOGGED = False
    user_store_mod._MERGE_DIAG_LOGGED = False

    client = TestClient(app)
    client.post("/login", data={"api_key": key}, follow_redirects=False)
    return client


def test_ui_file_leakt_keine_internen_exception_details(tmp_path, monkeypatch):
    """Unerwartete Exceptions (nicht ValueError/FileNotFoundError) duerfen
    ihre Detailmeldung nicht an den Client durchreichen — sonst koennten
    interne Pfade/Tracebacks im gerenderten HTML landen.

    Session wird direkt ueber session_store gesetzt statt per POST /login,
    um den fuer diese Testdatei bereits knapp bemessenen, prozessweiten
    Login-Rate-Limiter nicht zusaetzlich zu belasten."""
    from app import session_store
    from app.tenancy import ensure_user_workspace

    monkeypatch.setenv("KIWIKI_USERS", "admin:adminkey:admin")
    ws = ensure_user_workspace("admin")
    (ws / "notes").mkdir(parents=True, exist_ok=True)
    (ws / "notes" / "demo.md").write_text("---\ntitle: Demo\n---\n\nHallo", encoding="utf-8")

    def _boom(path):
        raise RuntimeError("/data/admin/notes/demo.md ist kaputt: geheime Interna")

    monkeypatch.setattr("app.main.read_file", _boom)
    record = session_store.create_session("admin", "admin", "adminkey")
    client = TestClient(app)
    client.cookies.set("kiwiki_session", record.token)
    resp = client.get("/ui/file?path=notes/demo.md")
    assert resp.status_code == 200
    assert "geheime Interna" not in resp.text
    assert "/data/admin" not in resp.text
    assert "Datei konnte nicht geladen werden." in resp.text


def test_ui_file_tags_sind_klickbare_buttons(tmp_path, monkeypatch):
    """Tags in der file_view werden als klickbare Buttons gerendert,
    die kwSearchTag('<tag>') aufrufen — nicht als passive <span>."""
    from app.tenancy import ensure_user_workspace

    monkeypatch.setenv("KIWIKI_USERS", "admin:adminkey:admin")
    ws = ensure_user_workspace("admin")
    (ws / "notes").mkdir(parents=True, exist_ok=True)
    (ws / "notes" / "tagged.md").write_text(
        "---\ntitle: Tagged\ntags: [python, refactor]\n---\n\nBody",
        encoding="utf-8",
    )
    client = _login((("admin", "adminkey", "admin"),), "adminkey")
    body = client.get("/ui/file?path=notes/tagged.md").text
    assert 'onclick="kwSearchTag(' in body
    assert 'class="tag"' in body
    assert ">python</button>" in body
    assert ">refactor</button>" in body
    # Kein rein passiver span mehr mit 'tag python' inline
    assert '<span class="tag">python</span>' not in body


def test_search_tag_prefix_liefert_nur_treffer_mit_diesem_tag(tmp_path, monkeypatch):
    """Suche mit 'tag:<value>' findet nur Dateien, die diesen Tag tragen."""
    from app.search import init_db, index_file
    from app.tenancy import ensure_user_workspace, set_user_ns

    monkeypatch.setenv("KIWIKI_USERS", "admin:adminkey:admin")
    ws = ensure_user_workspace("admin")
    set_user_ns("admin")
    (ws / "notes").mkdir(parents=True, exist_ok=True)
    (ws / "notes" / "a.md").write_text(
        "---\ntitle: A\ntags: [python]\n---\n\nPython content", encoding="utf-8"
    )
    (ws / "notes" / "b.md").write_text(
        "---\ntitle: B\ntags: [rust]\n---\n\nRust content", encoding="utf-8"
    )
    init_db()
    index_file("notes/a.md")
    index_file("notes/b.md")

    from app.search import search

    results = search("tag:python")
    paths = {r.path for r in results}
    assert paths == {"notes/a.md"}, paths


def test_layout_hat_main_landmark_und_skip_link(tmp_path, monkeypatch):
    """layout.html muss <main id="main-content"> und einen Skip-Link enthalten."""
    monkeypatch.setenv("KIWIKI_USERS", "admin:adminkey:admin")
    from app.tenancy import ensure_user_workspace

    ensure_user_workspace("admin")
    client = _login((("admin", "adminkey", "admin"),), "adminkey")
    # Index rendert das Basis-Layout
    body = client.get("/").text
    assert '<main class="content-area" id="main-content" tabindex="-1">' in body
    assert 'class="skip-link' in body
    assert 'href="#main-content"' in body
