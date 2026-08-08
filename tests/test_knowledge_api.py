"""Vertragstests fuer die optionale native Knowledge Engine.

Die Tests beschreiben bewusst nur die oeffentlichen REST- und MCP-Vertraege.
Markdown bleibt die Quelle der Wahrheit; die abgeleitete Knowledge-Datenbank darf
bei deaktiviertem Feature nicht einmal angelegt werden.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from app.models import User


READ_ONLY_KNOWLEDGE_TOOLS = {
    "knowledge_search",
    "entity_details",
    "entity_neighbors",
    "fact_timeline",
    "explain_relation",
    "knowledge_status",
}


def _configure_users(monkeypatch) -> None:
    monkeypatch.setenv(
        "KIWIKI_USERS",
        "alice:alice-key:admin,bob:bob-key:admin,reader:reader-key:read,writer:writer-key:write",
    )


def _headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _write_note(username: str, relative_path: str, content: str) -> Path:
    from app.tenancy import ensure_user_workspace

    path = ensure_user_workspace(username) / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _wait_until_ready(client: TestClient, key: str, timeout: float = 3.0) -> dict:
    deadline = time.monotonic() + timeout
    last_status: dict = {}
    while time.monotonic() < deadline:
        response = client.get("/api/knowledge/status", headers=_headers(key))
        assert response.status_code == 200, response.text
        last_status = response.json()
        if last_status.get("status") == "ready":
            return last_status
        time.sleep(0.02)
    raise AssertionError(f"Knowledge Engine wurde nicht ready: {last_status}")


def _schedule_reindex(client: TestClient, key: str) -> dict:
    response = client.post("/api/knowledge/reindex", headers=_headers(key))
    assert response.status_code == 202, response.text
    payload = response.json()
    assert payload["status"] in {"queued", "running"}
    return payload


def test_disabled_status_does_not_create_database(monkeypatch, tmp_path):
    _configure_users(monkeypatch)
    monkeypatch.setenv("KIWIKI_KNOWLEDGE_ENABLED", "false")

    with TestClient(app) as client:
        response = client.get("/api/knowledge/status", headers=_headers("reader-key"))

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "disabled"
    assert response.json()["enabled"] is False
    assert not list(tmp_path.rglob("knowledge.sqlite*"))


def test_knowledge_endpoints_enforce_read_and_write_roles(monkeypatch):
    _configure_users(monkeypatch)
    monkeypatch.setenv("KIWIKI_KNOWLEDGE_ENABLED", "true")

    with TestClient(app) as client:
        unauthenticated = client.get("/api/knowledge/status")
        reader_search = client.post(
            "/api/knowledge/search",
            json={"query": "atlas", "limit": 10},
            headers=_headers("reader-key"),
        )
        writer_reindex = client.post(
            "/api/knowledge/reindex",
            headers=_headers("writer-key"),
        )
        reader_reindex = client.post(
            "/api/knowledge/reindex",
            headers=_headers("reader-key"),
        )

    assert unauthenticated.status_code == 401
    assert reader_search.status_code == 200, reader_search.text
    assert writer_reindex.status_code == 202, writer_reindex.text
    assert reader_reindex.status_code == 403


def test_enabled_status_search_and_reindex_are_tenant_isolated(monkeypatch):
    _configure_users(monkeypatch)
    monkeypatch.setenv("KIWIKI_KNOWLEDGE_ENABLED", "true")
    _write_note(
        "alice",
        "projects/atlas.md",
        "---\ntitle: Projekt Atlas\ntype: project\nowner: Alice\ntags: [atlas]\n---\n\nSiehe [Plan](../notes/atlas-plan.md).",
    )
    _write_note(
        "alice",
        "notes/atlas-plan.md",
        "---\ntitle: Atlas Plan\ntype: note\ntags: [atlas]\n---\n\nNur fuer Alice.",
    )
    _write_note(
        "bob",
        "projects/borealis.md",
        "---\ntitle: Geheimes Borealis\ntype: project\ntags: [borealis]\n---\n\nNur fuer Bob.",
    )

    with TestClient(app) as client:
        _schedule_reindex(client, "alice-key")
        alice_status = _wait_until_ready(client, "alice-key")
        _schedule_reindex(client, "bob-key")
        bob_status = _wait_until_ready(client, "bob-key")

        alice_result = client.post(
            "/api/knowledge/search",
            json={"query": "atlas", "limit": 20},
            headers=_headers("alice-key"),
        )
        alice_cannot_find_bob = client.post(
            "/api/knowledge/search",
            json={"query": "borealis", "limit": 20},
            headers=_headers("alice-key"),
        )
        bob_result = client.post(
            "/api/knowledge/search",
            json={"query": "borealis", "limit": 20},
            headers=_headers("bob-key"),
        )

    assert alice_status["enabled"] is True
    assert alice_status["documents"] >= 2
    assert bob_status["documents"] >= 1
    assert alice_result.status_code == 200, alice_result.text
    assert bob_result.status_code == 200, bob_result.text

    alice_payload = alice_result.json()
    bob_payload = bob_result.json()
    isolated_payload = alice_cannot_find_bob.json()
    assert alice_payload["results"]
    assert bob_payload["results"]
    assert "projects/atlas.md" in json.dumps(alice_payload)
    assert "projects/borealis.md" in json.dumps(bob_payload)
    assert "borealis" not in json.dumps(isolated_payload).lower()
    assert "/home/" not in json.dumps(alice_payload)
    assert "/home/" not in json.dumps(bob_payload)


def test_knowledge_mcp_tool_definitions_have_bounded_schemas_and_annotations():
    from app.mcp_server import TOOLS

    definitions = {tool["name"]: tool for tool in TOOLS}
    assert READ_ONLY_KNOWLEDGE_TOOLS | {"knowledge_reindex"} <= definitions.keys()

    for name in READ_ONLY_KNOWLEDGE_TOOLS:
        tool = definitions[name]
        assert tool["annotations"] == {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        }
        assert tool["inputSchema"]["type"] == "object"
        assert tool["outputSchema"]["type"] in {"object", "array"}

    search_schema = definitions["knowledge_search"]["inputSchema"]
    assert search_schema["properties"]["query"]["maxLength"] <= 512
    assert search_schema["properties"]["limit"]["maximum"] <= 100
    neighbors_schema = definitions["entity_neighbors"]["inputSchema"]
    assert neighbors_schema["properties"]["depth"]["maximum"] <= 3
    assert neighbors_schema["properties"]["limit"]["maximum"] <= 100

    assert definitions["knowledge_reindex"]["annotations"] == {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }


def test_knowledge_mcp_dispatch_reports_disabled_status(monkeypatch):
    from app.mcp_server import _handle_message

    _configure_users(monkeypatch)
    monkeypatch.setenv("KIWIKI_KNOWLEDGE_ENABLED", "false")
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "knowledge_status", "arguments": {}},
    }

    result = asyncio.run(_handle_message(body, User(username="reader", role="read")))

    assert result["result"].get("isError") is not True
    assert result["result"]["structuredContent"]["status"] == "disabled"


def test_knowledge_mcp_reindex_requires_write(monkeypatch):
    from app.mcp_server import _handle_message

    _configure_users(monkeypatch)
    monkeypatch.setenv("KIWIKI_KNOWLEDGE_ENABLED", "true")
    body = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "knowledge_reindex", "arguments": {}},
    }

    denied = asyncio.run(_handle_message(body, User(username="reader", role="read")))
    accepted = asyncio.run(_handle_message(body, User(username="writer", role="write")))

    assert denied["result"]["isError"] is True
    assert "Write" in denied["result"]["content"][0]["text"]
    assert accepted["result"].get("isError") is not True
    assert accepted["result"]["structuredContent"]["status"] in {"queued", "running"}
