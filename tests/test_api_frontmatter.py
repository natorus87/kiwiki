"""Regression-Test fuer PATCH /api/file/frontmatter.

Hintergrund: kwBatchTag() im Frontend hat frueher Tags per Regex direkt
in den rohen Datei-Content injiziert und dabei den gesamten Frontmatter-
Block (inkl. title/created) ueberschrieben — Datenverlust bei Batch-Tagging.
Der Fix verschiebt das Merging serverseitig auf update_frontmatter(),
das nur die uebergebenen Keys mergt und den Rest unberuehrt laesst.
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


def test_update_frontmatter_preserves_other_fields(monkeypatch, tmp_path):
    from app.tenancy import ensure_user_workspace

    ws = ensure_user_workspace("writer")
    (ws / "notes").mkdir(parents=True, exist_ok=True)
    (ws / "notes" / "demo.md").write_text(
        "---\ntitle: Demo\ncreated: '2026-01-01'\n---\n\nHallo Welt",
        encoding="utf-8",
    )

    client = _client_with_user(monkeypatch, "writer", "writekey", "write")
    resp = client.patch(
        "/api/file/frontmatter",
        json={"path": "notes/demo.md", "updates": {"tags": ["python", "async"]}},
        headers={"Authorization": "Bearer writekey"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["frontmatter"]["tags"] == ["python", "async"]
    assert body["frontmatter"]["title"] == "Demo"
    assert body["frontmatter"]["created"] == "2026-01-01"
    assert body["content"].strip() == "Hallo Welt"


def test_update_frontmatter_requires_write_role(monkeypatch, tmp_path):
    from app.tenancy import ensure_user_workspace

    ws = ensure_user_workspace("reader")
    (ws / "notes").mkdir(parents=True, exist_ok=True)
    (ws / "notes" / "demo.md").write_text("---\ntitle: Demo\n---\n\nHallo", encoding="utf-8")

    client = _client_with_user(monkeypatch, "reader", "readkey", "read")
    resp = client.patch(
        "/api/file/frontmatter",
        json={"path": "notes/demo.md", "updates": {"tags": ["x"]}},
        headers={"Authorization": "Bearer readkey"},
    )
    assert resp.status_code == 403


def test_update_frontmatter_missing_file(monkeypatch, tmp_path):
    from app.tenancy import ensure_user_workspace

    ensure_user_workspace("writer")

    client = _client_with_user(monkeypatch, "writer", "writekey", "write")
    resp = client.patch(
        "/api/file/frontmatter",
        json={"path": "notes/fehlt.md", "updates": {"tags": ["x"]}},
        headers={"Authorization": "Bearer writekey"},
    )
    assert resp.status_code == 404
