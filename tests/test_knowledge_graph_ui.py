"""Vertragstests fuer den interaktiven Knowledge-Graphen."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app


ROOT = Path(__file__).resolve().parent.parent


def _login(monkeypatch, username: str = "alice", key: str = "alice-key") -> TestClient:
    monkeypatch.setenv("KIWIKI_USERS", f"{username}:{key}:admin")
    client = TestClient(app)
    response = client.post("/login", data={"api_key": key}, follow_redirects=False)
    assert response.status_code == 303
    return client


def test_knowledge_page_is_self_hosted_and_accessible(monkeypatch):
    client = _login(monkeypatch)

    response = client.get("/knowledge")

    assert response.status_code == 200
    assert 'id="knowledge-graph"' in response.text
    assert 'aria-label="Interaktiver 3D-Wissensgraph"' in response.text
    assert '/static/knowledge-graph.js' in response.text
    assert '/static/knowledge-graph.css' in response.text
    assert "https://" not in response.text


def test_knowledge_page_supports_english_and_language_switch(monkeypatch):
    client = _login(monkeypatch)

    response = client.get("/knowledge?lang=en")

    assert response.status_code == 200
    assert '<html lang="en">' in response.text
    assert "Neural Atlas" in response.text
    assert "Explore connections" in response.text
    assert "Mouse wheel / pinch" in response.text
    assert "Back to wiki" in response.text
    assert 'href="/knowledge?lang=de"' in response.text
    assert "Verbindungen verfolgen" not in response.text


def test_knowledge_page_explains_mobile_pinch_zoom_in_german(monkeypatch):
    client = _login(monkeypatch)

    response = client.get("/knowledge?lang=de")

    assert response.status_code == 200
    assert "Mausrad / Pinch" in response.text
    assert "Mit zwei Fingern zoomen" in response.text


def test_knowledge_page_uses_accept_language_and_persists_choice(monkeypatch):
    client = _login(monkeypatch)

    negotiated = client.get("/knowledge", headers={"Accept-Language": "en-US,en;q=0.9"})
    selected = client.get("/knowledge?lang=de")
    persisted = client.get("/knowledge", headers={"Accept-Language": "en-US"})

    assert '<html lang="en">' in negotiated.text
    assert '<html lang="de">' in selected.text
    assert "kiwiki_language=de" in selected.headers["set-cookie"]
    assert '<html lang="de">' in persisted.text


def test_sidebar_links_to_knowledge_graph(monkeypatch):
    client = _login(monkeypatch)

    response = client.get("/")

    assert response.status_code == 200
    assert 'href="/knowledge"' in response.text
    assert "Wissensgraph" in response.text


def test_large_graph_simulation_scales_repulsion_by_node_count():
    """Viele Knoten duerfen nicht nach dem Laden aus dem Sichtfeld explodieren."""
    script = (ROOT / "app/static/knowledge-graph.js").read_text(encoding="utf-8")

    strength = re.search(r"var strength = ([^;]+);", script)

    assert strength is not None
    assert strength.group(1).strip() == "1 / Math.max(nodes.length, 1)"


def test_large_graph_uses_bounded_linear_simulation_and_camera_fit():
    """Mobile Grossgraphen brauchen lineare Arbeit und einen datenabhaengigen Reset."""
    script = (ROOT / "app/static/knowledge-graph.js").read_text(encoding="utf-8")

    assert "var MAX_PAIRWISE_NODES =" in script
    assert "var MAX_NODE_SPEED =" in script
    assert "function simulateLargeGraph(" in script
    assert "nodes.length > MAX_PAIRWISE_NODES" in script
    assert "var pull = (distance - 115) * 0.0025 / distance" in script
    assert "state.adjacency.get(source.id).size" in script
    assert "state.adjacency.get(target.id).size" in script
    assert "state.simulationSteps" in script
    assert "function restoreGraphPositions(" in script
    assert "Number.isFinite(node.x)" in script
    assert "lostpointercapture" in script
    assert "window.addEventListener('pageshow'" in script
    assert "state.frame = 0" in script
    assert "function fitGraphDistance(" in script
    assert "defaultDistance:" in script
    assert "setDistance(fitGraphDistance(), true)" in script
    assert "state.nodes.length <= MAX_PAIRWISE_NODES" in script


def test_mobile_graph_implements_bounded_two_pointer_pinch_zoom():
    """Der Canvas muss Pinch selbst behandeln, weil natives Touch-Zoom deaktiviert ist."""
    script = (ROOT / "app/static/knowledge-graph.js").read_text(encoding="utf-8")

    assert "pointers: new Map()" in script
    assert "function setDistance(" in script
    assert "function pointerDistance(" in script
    assert "state.pointers.size === 2" in script
    assert "state.distance * state.pinchDistance / distance" in script
    assert "state.pointers.delete(event.pointerId)" in script


def test_graph_api_is_disabled_without_creating_data(monkeypatch):
    monkeypatch.setenv("KIWIKI_KNOWLEDGE_ENABLED", "false")
    client = _login(monkeypatch)

    response = client.get("/api/knowledge/graph?max_nodes=80&max_edges=160")

    assert response.status_code == 200
    assert response.json() == {
        "status": "disabled",
        "nodes": [],
        "edges": [],
        "truncated": False,
    }


def test_graph_api_returns_bounded_tenant_local_graph(monkeypatch):
    from app.knowledge.db import open_database
    from app.knowledge.indexer import upsert_document
    from app.tenancy import ensure_user_workspace

    monkeypatch.setenv("KIWIKI_KNOWLEDGE_ENABLED", "true")
    client = _login(monkeypatch)
    workspace = ensure_user_workspace("alice")
    (workspace / "notes").mkdir(parents=True, exist_ok=True)
    (workspace / "notes" / "atlas.md").write_text(
        "---\ntitle: Atlas\ntags: [wissen, graph]\nrelated: [notes/zweig.md]\n---\n\n"
        "Weiter zu [Zweig](zweig.md).",
        encoding="utf-8",
    )
    (workspace / "notes" / "zweig.md").write_text(
        "---\ntitle: Zweig\ntags: [wissen]\n---\n\nEin zweiter Gedanke.",
        encoding="utf-8",
    )
    connection = open_database(workspace)
    try:
        upsert_document(connection, workspace, "notes/atlas.md")
        upsert_document(connection, workspace, "notes/zweig.md")
    finally:
        connection.close()

    response = client.get("/api/knowledge/graph?max_nodes=3&max_edges=4")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ready"
    assert len(payload["nodes"]) <= 3
    assert len(payload["edges"]) <= 4
    assert all(not node.get("path", "").startswith("/") for node in payload["nodes"])
    assert {node["kind"] for node in payload["nodes"]} <= {"document", "tag", "concept"}
    assert "atlas" in str(payload).lower()
    assert "/home/" not in str(payload)
