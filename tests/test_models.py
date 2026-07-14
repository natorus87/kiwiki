import pytest
from pydantic import ValidationError

from app.models import CreateNoteRequest, SearchRequest, WriteFileRequest


def test_path_length_is_bounded():
    with pytest.raises(ValidationError):
        WriteFileRequest(path="a" * 1025 + ".md", content="ok")


def test_write_content_size_is_bounded():
    with pytest.raises(ValidationError):
        WriteFileRequest(path="notes/test.md", content="x" * (2 * 1024 * 1024 + 1))


def test_search_query_length_is_bounded():
    with pytest.raises(ValidationError):
        SearchRequest(query="q" * 513)


def test_note_tag_count_is_bounded():
    with pytest.raises(ValidationError):
        CreateNoteRequest(title="Titel", content="Body", tags=[f"tag-{i}" for i in range(51)])


def test_http_request_body_is_rejected_before_model_parsing(monkeypatch):
    monkeypatch.setenv("KIWIKI_USERS", "writer:writer-key:write")
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as client:
        response = client.put(
            "/api/file",
            headers={"Authorization": "Bearer writer-key"},
            json={"path": "notes/large.md", "content": "x" * (2 * 1024 * 1024 + 64 * 1024 + 1)},
        )

    assert response.status_code == 413


def test_chunked_http_request_body_is_also_bounded(monkeypatch):
    monkeypatch.setenv("KIWIKI_USERS", "writer:writer-key:write")
    from fastapi.testclient import TestClient
    from app.main import app

    def chunks():
        yield b'{"path":"notes/large.md","content":"'
        yield b"x" * (2 * 1024 * 1024 + 64 * 1024 + 1)
        yield b'"}'

    with TestClient(app) as client:
        response = client.put(
            "/api/file",
            headers={
                "Authorization": "Bearer writer-key",
                "Content-Type": "application/json",
            },
            content=chunks(),
        )

    assert response.status_code == 413
