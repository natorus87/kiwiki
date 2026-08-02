"""Regressionstests fuer das Loeschen mehrerer Notizen."""

import pytest
from fastapi.testclient import TestClient
from starlette.responses import Response

from app.main import RequestSizeLimitMiddleware, _delete_file_and_deindex, app


def test_batch_delete_removes_all_selected_notes_with_one_request(monkeypatch):
    """Mehrfachloeschungen duerfen nicht am Write-Rate-Limit pro Datei scheitern."""
    from app.search import index_file, init_db, search
    from app.tenancy import ensure_user_workspace, set_user_ns

    monkeypatch.setenv("KIWIKI_USERS", "admin:admin-key:admin")
    workspace = ensure_user_workspace("admin")
    notes_dir = workspace / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    paths = [f"notes/notiz-{index}.md" for index in range(35)]
    for path in paths:
        (workspace / path).write_text(f"# Batch Notiz {path}\n", encoding="utf-8")
    set_user_ns("admin")
    init_db()
    for path in paths:
        index_file(path)
    assert search("batch")

    with TestClient(app) as client:
        response = client.request(
            "DELETE",
            "/api/files",
            headers={"Authorization": "Bearer admin-key"},
            json={"paths": paths},
        )

    assert response.status_code == 200, response.text
    assert response.json() == {"deleted": paths, "failed": [], "index_cleanup_pending": []}
    assert all(not (workspace / path).exists() for path in paths)
    set_user_ns("admin")
    assert search("batch") == []


def test_delete_reports_success_when_only_index_cleanup_fails(monkeypatch):
    """Ein bereits geloeschtes Dokument darf wegen eines Indexfehlers nicht als fehlgeschlagen gelten."""
    from app.search import index_file, init_db, search
    from app.tenancy import ensure_user_workspace, set_user_ns

    monkeypatch.setenv("KIWIKI_USERS", "admin:admin-key:admin")
    workspace = ensure_user_workspace("admin")
    note = workspace / "notes" / "demo.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text(
        "---\ntitle: GeheimesAltesSuchwort\n---\n\nGleicher Inhalt\n",
        encoding="utf-8",
    )
    set_user_ns("admin")
    init_db()
    index_file("notes/demo.md")
    assert search("GeheimesAltesSuchwort")
    monkeypatch.setattr("app.main.deindex_file", lambda _path: (_ for _ in ()).throw(RuntimeError("DB locked")))

    with TestClient(app) as client:
        response = client.delete(
            "/api/file?path=notes/demo.md",
            headers={"Authorization": "Bearer admin-key"},
        )

    assert response.status_code == 200, response.text
    assert not note.exists()
    note.write_text(
        "---\ntitle: Harmloser neuer Titel\n---\n\nGleicher Inhalt\n",
        encoding="utf-8",
    )
    set_user_ns("admin")
    assert search("GeheimesAltesSuchwort") == []


def test_batch_delete_requires_admin_role(monkeypatch):
    """Eine Write-Rolle darf den destruktiven Batch-Endpunkt nicht verwenden."""
    from app.tenancy import ensure_user_workspace

    monkeypatch.setenv("KIWIKI_USERS", "writer:write-key:write")
    workspace = ensure_user_workspace("writer")
    note = workspace / "notes" / "demo.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("# Demo\n", encoding="utf-8")

    with TestClient(app) as client:
        response = client.request(
            "DELETE",
            "/api/files",
            headers={"Authorization": "Bearer write-key"},
            json={"paths": ["notes/demo.md"]},
        )

    assert response.status_code == 403
    assert note.exists()


def test_batch_delete_deindexes_all_paths_in_one_transaction(monkeypatch):
    """Ein Batch darf bei SQLite-Locks nicht pro Notiz erneut blockieren."""
    from app.tenancy import ensure_user_workspace

    monkeypatch.setenv("KIWIKI_USERS", "admin:admin-key:admin")
    workspace = ensure_user_workspace("admin")
    paths = ["notes/a.md", "notes/b.md", "notes/c.md"]
    for path in paths:
        note = workspace / path
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text("# Batch\n", encoding="utf-8")
    deindex_batches = []
    monkeypatch.setattr("app.main.deindex_files", lambda items: deindex_batches.append(list(items)))

    with TestClient(app) as client:
        response = client.request(
            "DELETE",
            "/api/files",
            headers={"Authorization": "Bearer admin-key"},
            json={"paths": paths},
        )

    assert response.status_code == 200, response.text
    assert deindex_batches == [paths]


@pytest.mark.asyncio
async def test_delete_body_without_content_length_is_size_limited():
    """Chunked DELETE-Bodies duerfen das globale Body-Limit nicht umgehen."""

    class ChunkedDeleteRequest:
        headers = {}
        method = "DELETE"

        async def stream(self):
            yield b"1234"
            yield b"5"

    async def call_next(_request):
        return Response(status_code=204)

    middleware = RequestSizeLimitMiddleware(app=None)
    middleware.max_body_bytes = 4

    response = await middleware.dispatch(ChunkedDeleteRequest(), call_next)

    assert response.status_code == 413


def test_delete_holds_path_lock_until_index_cleanup(monkeypatch):
    """Paralleles Neuschreiben darf nicht zwischen Dateiloeschung und Deindexierung gelangen."""
    import threading

    from app.storage import write_file
    from app.tenancy import ensure_user_workspace, set_user_ns

    workspace = ensure_user_workspace("admin")
    note = workspace / "notes" / "race.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("# Alt\n", encoding="utf-8")
    set_user_ns("admin")
    writer_finished = threading.Event()
    writer_threads = []
    writer_was_blocked = []

    def write_again():
        set_user_ns("admin")
        write_file("notes/race.md", "# Neu")
        writer_finished.set()

    def deindex_while_writer_waits(_path):
        writer = threading.Thread(target=write_again)
        writer_threads.append(writer)
        writer.start()
        writer_was_blocked.append(not writer_finished.wait(0.1))

    monkeypatch.setattr("app.main.deindex_file", deindex_while_writer_waits)

    _delete_file_and_deindex("notes/race.md")
    for writer in writer_threads:
        writer.join(timeout=1)

    assert writer_was_blocked == [True]
    assert writer_finished.is_set()
    assert note.exists()
