"""Transport-, Ressourcen- und Prompt-Pfade des MCP-Servers.

Diese Zweige waren ungetestet (mcp_server.py bei 67 % Coverage, Kanban-Karte
t_7c289e0a): JSON-RPC-Batches und Fehlerzweige der Streamable-HTTP-Route,
die Legacy-SSE-Sitzung (/mcp/sse + /mcp/messages), resources/* und prompts/*
sowie das Audit-Log inklusive Rotation.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app.models import User

ALICE = User(username="alice", role="admin")


def _rpc(method: str, params: dict | None = None, req_id: int | None = 1) -> dict:
    body = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if req_id is not None:
        body["id"] = req_id
    return body


def _handle(body, user: User | None = ALICE):
    from app.mcp_server import _handle_payload

    return asyncio.run(_handle_payload(body, user))


@pytest.fixture
def client(users_map, active_user):
    users_map(("alice", "tok1", "admin"), ("bob", "tok2", "read"))
    from app.main import app

    return TestClient(app)


AUTH = {"Authorization": "Bearer tok1"}


# ── Streamable HTTP: Fehlerzweige ───────────────────────────────────────────

def test_post_mcp_ohne_token_liefert_401_mit_discovery_hinweis(client):
    antwort = client.post("/mcp", json=_rpc("tools/list"))
    assert antwort.status_code == 401
    assert "resource_metadata=" in antwort.headers["www-authenticate"]


def test_post_mcp_mit_kaputtem_json_liefert_parse_error(client):
    antwort = client.post("/mcp", content=b"{nope", headers={**AUTH, "Content-Type": "application/json"})
    assert antwort.status_code == 400
    assert antwort.json()["error"]["code"] == -32700


def test_notification_ohne_id_liefert_202(client):
    antwort = client.post("/mcp", json=_rpc("notifications/initialized", req_id=None), headers=AUTH)
    assert antwort.status_code == 202


def test_ungueltiger_request_liefert_400(client):
    antwort = client.post("/mcp", json="kein-objekt", headers=AUTH)
    assert antwort.status_code == 400
    assert antwort.json()["error"]["code"] == -32600


# ── JSON-RPC-Batches ────────────────────────────────────────────────────────

def test_batch_liefert_eine_antwort_pro_request_und_keine_fuer_notifications():
    antwort = _handle([
        _rpc("tools/list", req_id=1),
        _rpc("notifications/initialized", req_id=None),
        "kaputt",
        _rpc("resources/templates/list", req_id=2),
    ])
    assert [eintrag.get("id") for eintrag in antwort] == [1, None, 2]
    assert antwort[1]["error"]["code"] == -32600


def test_leerer_batch_ist_ungueltig():
    assert _handle([])["error"]["code"] == -32600


def test_zu_grosser_batch_wird_abgelehnt(monkeypatch):
    from app import mcp_server

    monkeypatch.setattr(mcp_server, "_MCP_MAX_JSONRPC_BATCH", 2)
    antwort = _handle([_rpc("tools/list", req_id=i) for i in range(3)])
    assert antwort["error"]["code"] == -32600
    assert "exceeds 2" in antwort["error"]["message"]


def test_batch_nur_aus_notifications_liefert_nichts():
    assert _handle([_rpc("notifications/initialized", req_id=None)]) is None


# ── resources/* ─────────────────────────────────────────────────────────────

def test_resources_list_nennt_alle_uris():
    antwort = _handle(_rpc("resources/list"))
    uris = {r["uri"] for r in antwort["result"]["resources"]}
    assert uris == {"kiwiki://index", "kiwiki://agents", "kiwiki://tags", "kiwiki://recent", "kiwiki://search_history"}


@pytest.mark.parametrize("uri,mime", [
    ("kiwiki://index", "text/markdown"),
    ("kiwiki://agents", "text/markdown"),
    ("kiwiki://tags", "application/json"),
    ("kiwiki://recent", "application/json"),
    ("kiwiki://search_history", "application/json"),
])
def test_resources_read_liefert_inhalt(active_user, uri, mime):
    antwort = _handle(_rpc("resources/read", {"uri": uri}))
    inhalt = antwort["result"]["contents"][0]
    assert inhalt["uri"] == uri
    assert inhalt["mimeType"] == mime
    assert inhalt["text"]
    if mime == "application/json":
        json.loads(inhalt["text"])


def test_resources_read_ohne_index_liefert_platzhalter(active_user):
    (active_user / "index.md").unlink()
    antwort = _handle(_rpc("resources/read", {"uri": "kiwiki://index"}))
    assert "No index.md found" in antwort["result"]["contents"][0]["text"]


def test_resources_read_unbekannte_uri():
    antwort = _handle(_rpc("resources/read", {"uri": "kiwiki://gibtesnicht"}))
    assert antwort["error"]["code"] == -32602


# ── prompts/* ───────────────────────────────────────────────────────────────

def test_prompts_list_und_jeder_prompt_ist_abrufbar():
    namen = [p["name"] for p in _handle(_rpc("prompts/list"))["result"]["prompts"]]
    assert len(namen) == 5
    for name in namen:
        antwort = _handle(_rpc("prompts/get", {"name": name, "arguments": {"title": "X", "context": "Warum"}}))
        nachricht = antwort["result"]["messages"][0]
        assert nachricht["role"] == "user"
        assert nachricht["content"]["text"]


def test_decision_prompt_uebernimmt_kontext():
    antwort = _handle(_rpc("prompts/get", {"name": "decision_record", "arguments": {"title": "DB", "context": "Last"}}))
    assert "Context: Last" in antwort["result"]["messages"][0]["content"]["text"]


def test_unbekannter_prompt():
    assert _handle(_rpc("prompts/get", {"name": "nix"}))["error"]["code"] == -32602


# ── tools/call: Fehlerabbildung ─────────────────────────────────────────────

def test_tool_fehler_werden_als_iserror_gemeldet(active_user):
    reader = User(username="alice", role="read")
    verweigert = _handle(_rpc("tools/call", {"name": "write_file", "arguments": {"path": "x.md", "content": "y"}}), reader)
    assert verweigert["result"]["isError"] is True
    assert "Permission denied" in verweigert["result"]["content"][0]["text"]

    fehlt = _handle(_rpc("tools/call", {"name": "read_file", "arguments": {"path": "gibtsnicht.md"}}))
    assert fehlt["result"]["isError"] is True


def test_unerwartete_tool_ausnahme_verraet_keine_details(active_user, monkeypatch):
    from app import mcp_server

    async def kaputt(*_args, **_kwargs):
        raise RuntimeError("geheimes Detail")

    monkeypatch.setattr(mcp_server, "_dispatch", kaputt)
    antwort = _handle(_rpc("tools/call", {"name": "read_index", "arguments": {}}))
    text = antwort["result"]["content"][0]["text"]
    assert antwort["result"]["isError"] is True
    assert "geheimes Detail" not in text


# ── Audit-Log ───────────────────────────────────────────────────────────────

def test_audit_log_speichert_nur_strukturfelder(active_user):
    from app.mcp_server import _AGENT_LOG_FILE, _log_agent_call

    _log_agent_call(ALICE, "write_file", {"path": "a.md", "content": "Passwort=geheim"}, success=True)
    eintrag = json.loads((active_user / _AGENT_LOG_FILE).read_text("utf-8").splitlines()[-1])
    assert eintrag["args"] == {"path": "a.md"}
    assert eintrag["tool"] == "write_file"
    assert oct((active_user / _AGENT_LOG_FILE).stat().st_mode & 0o777) == "0o600"


def test_audit_log_rotiert_bei_ueberschreitung(active_user, monkeypatch):
    from app import mcp_server

    monkeypatch.setattr(mcp_server, "_AGENT_LOG_MAX_BYTES", 10)
    for _ in range(3):
        mcp_server._log_agent_call(ALICE, "read_file", {"path": "a.md"}, success=False, error="x" * 500)
    log = active_user / mcp_server._AGENT_LOG_FILE
    rotiert = log.with_suffix(".jsonl.1")
    assert rotiert.exists()
    assert len(log.read_text("utf-8").splitlines()) == 1
    assert len(json.loads(rotiert.read_text("utf-8"))["error"]) == 200


def test_audit_log_fehler_bricht_nichts_ab(active_user, monkeypatch):
    from app import mcp_server

    def kaputt(*_args, **_kwargs):
        raise OSError("Platte voll")

    monkeypatch.setattr(mcp_server.os, "open", kaputt)
    mcp_server._log_agent_call(ALICE, "read_file", {}, success=True)


# ── Legacy-SSE: /mcp/messages ───────────────────────────────────────────────

def _sse_session(user: User) -> tuple[str, asyncio.Queue]:
    from app import mcp_server

    queue: asyncio.Queue = asyncio.Queue(maxsize=1)
    mcp_server._sse_sessions["sid-1"] = (queue, user)
    return "sid-1", queue


def test_sse_nachricht_landet_in_der_session_queue(client):
    sid, queue = _sse_session(ALICE)
    antwort = client.post(f"/mcp/messages?sessionId={sid}", json=_rpc("tools/list"), headers=AUTH)
    assert antwort.status_code == 202
    assert "tools" in queue.get_nowait()["result"]


def test_sse_unbekannte_session(client):
    antwort = client.post("/mcp/messages?sessionId=nix", json=_rpc("tools/list"), headers=AUTH)
    assert antwort.status_code == 404


def test_sse_fremder_user_wird_abgelehnt(client):
    sid, _queue = _sse_session(ALICE)
    antwort = client.post(
        f"/mcp/messages?sessionId={sid}", json=_rpc("tools/list"), headers={"Authorization": "Bearer tok2"},
    )
    assert antwort.status_code == 403


def test_sse_ohne_token_und_mit_kaputtem_json(client):
    sid, _queue = _sse_session(ALICE)
    assert client.post(f"/mcp/messages?sessionId={sid}", json=_rpc("tools/list")).status_code == 401
    kaputt = client.post(
        f"/mcp/messages?sessionId={sid}", content=b"{", headers={**AUTH, "Content-Type": "application/json"},
    )
    assert kaputt.status_code == 400


def test_sse_volle_queue_liefert_429(client):
    sid, queue = _sse_session(ALICE)
    queue.put_nowait({"belegt": True})
    antwort = client.post(f"/mcp/messages?sessionId={sid}", json=_rpc("tools/list"), headers=AUTH)
    assert antwort.status_code == 429


def test_sse_notification_liefert_202_ohne_queue_eintrag(client):
    sid, queue = _sse_session(ALICE)
    antwort = client.post(
        f"/mcp/messages?sessionId={sid}", json=_rpc("notifications/initialized", req_id=None), headers=AUTH,
    )
    assert antwort.status_code == 202
    assert queue.empty()


def test_sse_endpunkte_verlangen_token_und_begrenzen_sessions(client, monkeypatch):
    from app import mcp_server

    assert client.get("/mcp/sse").status_code == 401
    assert client.get("/mcp").status_code == 401
    monkeypatch.setattr(mcp_server, "_SSE_MAX_SESSIONS", 0)
    assert client.get("/mcp/sse", headers=AUTH).status_code == 503


def test_sse_stream_meldet_endpoint_und_raeumt_session_auf(users_map, active_user, monkeypatch):
    """Der Generator direkt: erst endpoint-Event, dann Nachricht, dann Aufraeumen."""
    users_map(("alice", "tok1", "admin"))
    from starlette.requests import Request

    from app import mcp_server

    verbunden = iter([False, True])

    async def lauf():
        scope = {
            "type": "http", "method": "GET", "path": "/mcp/sse", "query_string": b"",
            "headers": [(b"authorization", b"Bearer tok1")], "scheme": "http",
            "server": ("testserver", 80), "root_path": "",
        }
        request = Request(scope)

        async def is_disconnected():
            return next(verbunden)

        monkeypatch.setattr(request, "is_disconnected", is_disconnected)
        antwort = await mcp_server.mcp_sse(request)
        stream = antwort.body_iterator
        erstes = await stream.__anext__()
        sid = erstes.split("sessionId=")[1].strip()
        assert sid in mcp_server._sse_sessions
        mcp_server._sse_sessions[sid][0].put_nowait({"id": 7})
        zweites = await stream.__anext__()
        with pytest.raises(StopAsyncIteration):
            await stream.__anext__()
        return erstes, zweites, sid

    erstes, zweites, sid = asyncio.run(lauf())
    assert erstes.startswith("event: endpoint\n")
    assert zweites == 'event: message\ndata: {"id": 7}\n\n'
    assert sid not in mcp_server._sse_sessions
