"""Regression-Tests fuer GET /api/file und GET /api/files.

Hintergrund: beide Endpoints haben Pydantic-Modelle (FileContent bzw.
list[FileInfo]) direkt an starlette.responses.JSONResponse() uebergeben.
JSONResponse serialisiert mit dem Standard-json-Modul, das Pydantic-
Objekte nicht kennt — jeder erfolgreiche Read endete daher mit einem
400 'Object of type FileContent/FileInfo is not JSON serializable'.
Der Fix wandelt die Modelle vorher mit .model_dump() in reine dicts um.
"""

from fastapi.testclient import TestClient

from app.main import app


def _client_with_user(monkeypatch, username: str, key: str, role: str) -> TestClient:
    monkeypatch.setenv("KIWIKI_USERS", f"{username}:{key}:{role}")
    from app import auth as auth_mod
    from app import user_store as user_store_mod

    auth_mod._PARSE_DIAG_LOGGED = False
    user_store_mod._PARSE_DIAG_LOGGED = False
    user_store_mod._LOCAL_DIAG_LOGGED = False
    user_store_mod._MERGE_DIAG_LOGGED = False
    return TestClient(app)


def test_get_api_file_returns_content_and_frontmatter(monkeypatch, tmp_path):
    from app.tenancy import ensure_user_workspace

    ws = ensure_user_workspace("reader")
    (ws / "notes").mkdir(parents=True, exist_ok=True)
    (ws / "notes" / "demo.md").write_text(
        "---\ntitle: Demo\ntags: [a, b]\n---\n\nHallo Welt",
        encoding="utf-8",
    )

    client = _client_with_user(monkeypatch, "reader", "readkey", "read")
    resp = client.get(
        "/api/file?path=notes/demo.md",
        headers={"Authorization": "Bearer readkey"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["path"] == "notes/demo.md"
    assert body["content"].strip() == "Hallo Welt"
    assert body["frontmatter"]["title"] == "Demo"
    assert body["frontmatter"]["tags"] == ["a", "b"]


def test_get_api_files_returns_list_of_dicts(monkeypatch, tmp_path):
    from app.tenancy import ensure_user_workspace

    ws = ensure_user_workspace("reader")
    (ws / "notes").mkdir(parents=True, exist_ok=True)
    (ws / "notes" / "demo.md").write_text("---\ntitle: Demo\n---\n\nHallo", encoding="utf-8")

    client = _client_with_user(monkeypatch, "reader", "readkey", "read")
    resp = client.get(
        "/api/files?path=notes",
        headers={"Authorization": "Bearer readkey"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert isinstance(body, list)
    assert body[0]["path"] == "notes/demo.md"
    assert body[0]["is_dir"] is False


def test_unexpected_api_errors_do_not_expose_server_details(monkeypatch):
    import app.main as main_mod

    client = _client_with_user(monkeypatch, "reader", "readkey", "read")
    monkeypatch.setattr(main_mod, "list_files", lambda _path: (_ for _ in ()).throw(RuntimeError("/srv/private")))

    response = client.get("/api/files", headers={"Authorization": "Bearer readkey"})

    assert response.status_code == 400
    assert response.json()["detail"] == "Request could not be processed"
    assert "/srv/private" not in response.text
