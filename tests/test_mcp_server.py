"""Tests fuer app/mcp_server.py — Tool-Dispatch, Auth, JSON-RPC."""

import base64
import hashlib
import json
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app.mcp_server import (
    _handle_message,
    _user_from_request,
    _api_key_from_signed_token,
    _make_oauth_token,
    validate_oauth_config,
    _rpc_ok,
    _rpc_err,
    _is_notification,
    TOOLS,
    _READ_ONLY_TOOLS,
    _DESTRUCTIVE_TOOLS,
)
from app.models import User

TOOL_COUNT = 49
ADDED_TOOLS = {
    "recent_files",
    "backlinks",
    "move_folder",
    "preview_edit",
    "replace_many",
    "write_many",
    "chunked_write",
    "validate_wiki",
    "upsert_note",
    "related_files",
    "tag_index",
    "reindex_all",
    "search_status",
    "whoami",
    "git_commit",
    "file_history",
    "diff",
    "statistics",
    "template",
    "validate_links",
    "link_graph",
    "rename",
    "batch_tag",
    "export",
    "duplicate_check",
    "ai_summarize",
}


class TestRpcHelpers:
    """_rpc_ok(), _rpc_err(), _is_notification()."""

    def test_rpc_ok(self):
        result = _rpc_ok(1, {"key": "value"})
        assert result == {"jsonrpc": "2.0", "id": 1, "result": {"key": "value"}}

    def test_rpc_err(self):
        result = _rpc_err(2, -32000, "Fehler")
        assert result["error"]["code"] == -32000
        assert result["error"]["message"] == "Fehler"

    def test_is_notification_keine_id(self):
        assert _is_notification({"method": "tools/list", "params": {}}) is True

    def test_is_notification_mit_id_keine_notification(self):
        assert _is_notification({"id": 1, "method": "tools/list"}) is False


class TestUserFromRequest:
    """_user_from_request() — User aus Header extrahieren (sync)."""

    def test_gueltiger_bearer_token(self, users_map):
        users_map(("alice", "tok1", "read"))
        req = AsyncMock()
        req.headers = {"Authorization": "Bearer tok1"}
        user = _user_from_request(req)
        assert user.username == "alice"
        assert user.role == "read"

    def test_kein_token(self):
        req = AsyncMock()
        req.headers = {}
        assert _user_from_request(req) is None

    def test_unbekannter_token(self, users_map):
        users_map(("alice", "tok1", "read"))
        req = AsyncMock()
        req.headers = {"Authorization": "Bearer wrong"}
        assert _user_from_request(req) is None

    def test_signed_oauth_access_token_works_without_memory_store(self, users_map):
        users_map(("alice", "tok1", "read"))
        token = _make_oauth_token("tok1", "access", 3600, client_id="chatgpt", resource="https://wiki.example/mcp")
        req = AsyncMock()
        req.headers = {"Authorization": f"Bearer {token}"}

        user = _user_from_request(req)

        assert user.username == "alice"
        assert user.role == "read"

    def test_expired_signed_oauth_access_token_rejected(self, users_map):
        users_map(("alice", "tok1", "read"))
        token = _make_oauth_token("tok1", "access", -1)
        req = AsyncMock()
        req.headers = {"Authorization": f"Bearer {token}"}
        assert _user_from_request(req) is None

    def test_refresh_token_does_not_validate_as_access_token(self, users_map):
        users_map(("alice", "tok1", "read"))
        token = _make_oauth_token("tok1", "refresh", 3600)
        assert _api_key_from_signed_token(token, expected_type="access") is None
        assert _api_key_from_signed_token(token, expected_type="refresh") == "tok1"

    def test_known_placeholder_oauth_secret_rejected(self, monkeypatch):
        monkeypatch.setenv("KIWIKI_OAUTH_TOKEN_SECRET", "change-me-to-a-random-secret")
        with pytest.raises(RuntimeError, match="placeholder"):
            validate_oauth_config()


class TestOAuthFlow:
    """OAuth compatibility for ChatGPT-style MCP connectors."""

    def test_authorization_code_exchange_returns_refreshable_signed_tokens(self, users_map):
        users_map(("alice", "tok1", "admin"))
        from app.main import app

        verifier = "a" * 64
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
        redirect_uri = "https://chatgpt.com/connector/oauth/test-callback"
        resource = "http://testserver/mcp"
        client = TestClient(app)

        authorize = client.post(
            "/oauth/authorize",
            data={
                "apikey": "tok1",
                "redirect_uri": redirect_uri,
                "client_id": "https://chatgpt.com/oauth/kiwiki/client.json",
                "state": "state-1",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "resource": resource,
            },
            follow_redirects=False,
        )

        assert authorize.status_code == 302
        location = authorize.headers["location"]
        query = parse_qs(urlparse(location).query)
        code = query["code"][0]
        assert query["state"] == ["state-1"]

        token = client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": "https://chatgpt.com/oauth/kiwiki/client.json",
                "redirect_uri": redirect_uri,
                "code_verifier": verifier,
                "resource": resource,
            },
        )
        assert token.status_code == 200
        payload = token.json()
        assert payload["token_type"] == "bearer"
        assert payload["access_token"].startswith("kiwiki1.")
        assert payload["refresh_token"].startswith("kiwiki1.")
        assert _api_key_from_signed_token(payload["access_token"], expected_type="access") == "tok1"

        refreshed = client.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": payload["refresh_token"],
                "client_id": "https://chatgpt.com/oauth/kiwiki/client.json",
                "resource": resource,
            },
        )
        assert refreshed.status_code == 200
        assert refreshed.json()["access_token"].startswith("kiwiki1.")

    def test_authorize_rejects_unregistered_arbitrary_https_redirect(self, users_map):
        users_map(("alice", "tok1", "admin"))
        from app.main import app

        verifier = "a" * 64
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
        client = TestClient(app)

        authorize = client.post(
            "/oauth/authorize",
            data={
                "apikey": "tok1",
                "redirect_uri": "https://evil.example/callback",
                "client_id": "not-registered",
                "state": "state-1",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "resource": "http://testserver/mcp",
            },
            follow_redirects=False,
        )

        assert authorize.status_code == 400
        assert authorize.json()["error"] == "invalid_redirect_uri"

    def test_registered_redirect_is_required_for_dcr_client(self, users_map):
        users_map(("alice", "tok1", "admin"))
        from app.main import app

        verifier = "a" * 64
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
        client = TestClient(app)

        registration = client.post("/oauth/register", json={"redirect_uris": ["https://client.example/callback"]})
        assert registration.status_code == 201
        client_id = registration.json()["client_id"]

        rejected = client.post(
            "/oauth/authorize",
            data={
                "apikey": "tok1",
                "redirect_uri": "https://other.example/callback",
                "client_id": client_id,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "resource": "http://testserver/mcp",
            },
            follow_redirects=False,
        )
        assert rejected.status_code == 400

        accepted = client.post(
            "/oauth/authorize",
            data={
                "apikey": "tok1",
                "redirect_uri": "https://client.example/callback",
                "client_id": client_id,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "resource": "http://testserver/mcp",
            },
            follow_redirects=False,
        )
        assert accepted.status_code == 302


class TestToolDefinitions:
    """TOOL-Liste — Struktur und Annotationen."""

    def test_anzahl_tools(self):
        assert len(TOOLS) == TOOL_COUNT

    def test_jedes_tool_hat_name_und_schema(self):
        names = {t["name"] for t in TOOLS}
        assert len(names) == TOOL_COUNT  # Keine Duplikate
        assert ADDED_TOOLS.issubset(names)
        for tool in TOOLS:
            assert "inputSchema" in tool
            assert "outputSchema" in tool

    def test_jedes_tool_hat_annotationen(self):
        for tool in TOOLS:
            assert "annotations" in tool
            assert "readOnlyHint" in tool["annotations"]
            assert "destructiveHint" in tool["annotations"]

    def test_output_schemas_sind_definiert(self):
        for tool in TOOLS:
            schema = tool["outputSchema"]
            assert schema["type"] in {"object", "array"}
            if schema["type"] == "object":
                assert "properties" in schema or "additionalProperties" in schema
            if schema["type"] == "array":
                assert "items" in schema

    def test_read_only_tools(self):
        assert "read_file" in _READ_ONLY_TOOLS
        assert "write_file" not in _READ_ONLY_TOOLS

    def test_destructive_tools(self):
        assert "delete_file" in _DESTRUCTIVE_TOOLS
        assert "read_file" not in _DESTRUCTIVE_TOOLS

    def test_read_only_annotationen_stimmen(self):
        for tool in TOOLS:
            if tool["name"] in _READ_ONLY_TOOLS:
                assert tool["annotations"]["readOnlyHint"] is True
            else:
                assert tool["annotations"]["readOnlyHint"] is False


class TestHandleMessage:
    """_handle_message() — JSON-RPC-Methoden dispatchen."""

    @pytest.fixture
    def _setup_users(self, users_map):
        users_map(("alice", "tok1", "admin"))

    @pytest.fixture
    def _setup_auth(self, _setup_users):
        """Mockt parse_users fuer auth-pruefende Methoden."""
        from app import mcp_server

        original_parse = mcp_server.parse_users
        mcp_server.parse_users = lambda: {"tok1": ("alice", "admin")}
        try:
            yield
        finally:
            mcp_server.parse_users = original_parse

    async def test_initialize(self, _setup_auth):
        body = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        result = await _handle_message(body, User(username="alice", role="admin"))
        assert result["result"]["protocolVersion"] == "2025-03-26"
        assert "tools" in result["result"]["capabilities"]

    async def test_tools_list(self, _setup_auth):
        body = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        result = await _handle_message(body, User(username="alice", role="read"))
        assert len(result["result"]["tools"]) == TOOL_COUNT
        assert all("outputSchema" in tool for tool in result["result"]["tools"])

    async def test_unknown_method(self, _setup_auth):
        body = {"jsonrpc": "2.0", "id": 3, "method": "unknown_method"}
        result = await _handle_message(body, User(username="alice", role="read"))
        assert "error" in result
        assert result["error"]["code"] == -32601

    async def test_notification_keineantwort(self, _setup_auth):
        body = {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
        result = await _handle_message(body, User(username="alice", role="read"))
        assert result is None  # Notifications antworten nicht.


class TestToolDispatch:
    """_dispatch() — Spezifische Tool-Aufrufe."""

    @pytest.fixture
    def _setup_users(self, users_map):
        users_map(("alice", "tok1", "admin"))

    async def test_read_file_tool(self, tmp_file, active_user, _setup_users):
        from app import mcp_server

        original_parse = mcp_server.parse_users
        mcp_server.parse_users = lambda: {"tok1": ("alice", "admin")}
        try:
            rel = tmp_file("notes/test.md", "---\ntitle: Test\n---\n\nBody")
            body = {
                "jsonrpc": "2.0",
                "id": 10,
                "method": "tools/call",
                "params": {"name": "read_file", "arguments": {"path": rel}},
            }
            result = await _handle_message(body, User(username="alice", role="admin"))
            assert result["result"]["content"][0]["type"] == "text"
            assert result["result"]["structuredContent"]["path"] == rel
            resp = json.loads(result["result"]["content"][0]["text"])
            assert resp["path"] == rel
            assert "Body" in resp["content"]
        finally:
            mcp_server.parse_users = original_parse

    async def test_write_file_tool(self, tmp_file, active_user, _setup_users, tmp_path):
        from app import mcp_server

        original_parse = mcp_server.parse_users
        mcp_server.parse_users = lambda: {"tok1": ("alice", "admin")}
        try:
            body = {
                "jsonrpc": "2.0",
                "id": 11,
                "method": "tools/call",
                "params": {"name": "write_file", "arguments": {"path": "notes/neu.md", "content": "---\ntitle: Neu\n---\n\nHallo"}},
            }
            result = await _handle_message(body, User(username="alice", role="admin"))
            assert result["result"]["structuredContent"]["status"] == "written"
            resp = json.loads(result["result"]["content"][0]["text"])
            assert resp["status"] == "written"
            assert (tmp_path / "notes/neu.md").exists()
        finally:
            mcp_server.parse_users = original_parse

    async def test_write_many_continues_after_per_file_error(self, active_user, _setup_users):
        from app import mcp_server

        original_parse = mcp_server.parse_users
        mcp_server.parse_users = lambda: {"tok1": ("alice", "admin")}
        try:
            body = {
                "jsonrpc": "2.0",
                "id": 41,
                "method": "tools/call",
                "params": {
                    "name": "write_many",
                    "arguments": {
                        "files": [
                            {"path": "notes/batch-a.md", "content": "---\ntitle: A\n---\n\nA"},
                            {"path": ".kiwiki/system.md", "content": "blocked"},
                            {"path": "notes/batch-b.md", "content": "B", "mode": "append"},
                        ]
                    },
                },
            }
            result = await _handle_message(body, User(username="alice", role="admin"))
            payload = result["result"]["structuredContent"]
            assert payload["written"] == 2
            assert payload["failed"] == 1
            assert (active_user / "notes/batch-a.md").exists()
            assert (active_user / "notes/batch-b.md").exists()
            assert payload["results"][1]["status"] == "error"
            assert ".kiwiki" in payload["results"][1]["error"]
        finally:
            mcp_server.parse_users = original_parse

    async def test_chunked_write_finalize_with_sha256(self, active_user, _setup_users):
        from app import mcp_server

        original_parse = mcp_server.parse_users
        mcp_server.parse_users = lambda: {"tok1": ("alice", "admin")}
        try:
            content = "---\ntitle: Chunked\n---\n\n" + ("large-body\n" * 50)
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            first = await _handle_message({
                "jsonrpc": "2.0",
                "id": 42,
                "method": "tools/call",
                "params": {
                    "name": "chunked_write",
                    "arguments": {
                        "path": "notes/chunked.md",
                        "upload_id": "chunked-ok",
                        "chunk": content[:100],
                        "chunk_index": 0,
                        "total_chunks": 2,
                    },
                },
            }, User(username="alice", role="admin"))
            assert first["result"]["structuredContent"]["status"] == "staged"

            second = await _handle_message({
                "jsonrpc": "2.0",
                "id": 43,
                "method": "tools/call",
                "params": {
                    "name": "chunked_write",
                    "arguments": {
                        "path": "notes/chunked.md",
                        "upload_id": "chunked-ok",
                        "chunk": content[100:],
                        "chunk_index": 1,
                        "total_chunks": 2,
                        "finalize": True,
                        "expected_sha256": digest,
                    },
                },
            }, User(username="alice", role="admin"))
            payload = second["result"]["structuredContent"]
            assert payload["status"] == "written"
            assert payload["sha256"] == digest
            assert "large-body" in (active_user / "notes/chunked.md").read_text(encoding="utf-8")
        finally:
            mcp_server.parse_users = original_parse

    async def test_chunked_write_reports_missing_chunks(self, active_user, _setup_users):
        from app import mcp_server

        original_parse = mcp_server.parse_users
        mcp_server.parse_users = lambda: {"tok1": ("alice", "admin")}
        try:
            result = await _handle_message({
                "jsonrpc": "2.0",
                "id": 44,
                "method": "tools/call",
                "params": {
                    "name": "chunked_write",
                    "arguments": {
                        "path": "notes/missing.md",
                        "upload_id": "chunked-missing",
                        "chunk": "tail",
                        "chunk_index": 1,
                        "total_chunks": 2,
                        "finalize": True,
                    },
                },
            }, User(username="alice", role="admin"))
            payload = result["result"]["structuredContent"]
            assert payload["status"] == "missing_chunks"
            assert payload["missing_chunks"] == [0]
            assert not (active_user / "notes/missing.md").exists()
        finally:
            mcp_server.parse_users = original_parse

    async def test_whoami_tool(self, active_user, _setup_users):
        from app import mcp_server

        original_parse = mcp_server.parse_users
        mcp_server.parse_users = lambda: {"tok1": ("alice", "admin")}
        try:
            body = {
                "jsonrpc": "2.0",
                "id": 30,
                "method": "tools/call",
                "params": {"name": "whoami", "arguments": {}},
            }
            result = await _handle_message(body, User(username="alice", role="admin"))
            resp = result["result"]["structuredContent"]
            assert resp["username"] == "alice"
            assert resp["role"] == "admin"
        finally:
            mcp_server.parse_users = original_parse

    async def test_recent_files_and_tag_index_tools(self, tmp_file, active_user, _setup_users):
        from app import mcp_server

        original_parse = mcp_server.parse_users
        mcp_server.parse_users = lambda: {"tok1": ("alice", "admin")}
        try:
            tmp_file("notes/a.md", "---\ntitle: A\ntags: [python]\n---\n\nA")
            tmp_file("notes/b.md", "---\ntitle: B\ntags: [python, mcp]\n---\n\nB")

            recent = await _handle_message({
                "jsonrpc": "2.0",
                "id": 31,
                "method": "tools/call",
                "params": {"name": "recent_files", "arguments": {"limit": 5}},
            }, User(username="alice", role="admin"))
            assert any(item["path"] == "notes/b.md" for item in recent["result"]["structuredContent"])

            tags = await _handle_message({
                "jsonrpc": "2.0",
                "id": 32,
                "method": "tools/call",
                "params": {"name": "tag_index", "arguments": {}},
            }, User(username="alice", role="admin"))
            tag_map = {item["tag"]: item for item in tags["result"]["structuredContent"]}
            assert tag_map["python"]["count"] == 2
        finally:
            mcp_server.parse_users = original_parse

    async def test_preview_edit_and_replace_many_tools(self, tmp_file, active_user, _setup_users):
        from app import mcp_server

        original_parse = mcp_server.parse_users
        mcp_server.parse_users = lambda: {"tok1": ("alice", "admin")}
        try:
            rel = tmp_file("notes/edit.md", "---\ntitle: Edit\n---\n\nold text")
            preview = await _handle_message({
                "jsonrpc": "2.0",
                "id": 33,
                "method": "tools/call",
                "params": {"name": "preview_edit", "arguments": {"path": rel, "old_str": "old", "new_str": "new"}},
            }, User(username="alice", role="admin"))
            assert preview["result"]["structuredContent"]["changed"] is True
            assert "+new text" in preview["result"]["structuredContent"]["diff"]

            replaced = await _handle_message({
                "jsonrpc": "2.0",
                "id": 34,
                "method": "tools/call",
                "params": {"name": "replace_many", "arguments": {"path": rel, "replacements": [{"old_str": "old", "new_str": "new"}]}},
            }, User(username="alice", role="admin"))
            assert replaced["result"]["structuredContent"]["total_replacements"] == 1
            assert "new text" in (active_user / rel).read_text(encoding="utf-8")
        finally:
            mcp_server.parse_users = original_parse

    async def test_backlinks_related_and_validate_tools(self, tmp_file, active_user, _setup_users):
        from app import mcp_server

        original_parse = mcp_server.parse_users
        mcp_server.parse_users = lambda: {"tok1": ("alice", "admin")}
        try:
            target = tmp_file("notes/target.md", "---\ntitle: Target\ntags: [mcp]\n---\n\nTarget")
            tmp_file("notes/source.md", "---\ntitle: Source\ntags: [mcp]\n---\n\nSee [Target](target.md).")

            backlinks = await _handle_message({
                "jsonrpc": "2.0",
                "id": 35,
                "method": "tools/call",
                "params": {"name": "backlinks", "arguments": {"path": target}},
            }, User(username="alice", role="admin"))
            assert backlinks["result"]["structuredContent"]["count"] == 1

            related = await _handle_message({
                "jsonrpc": "2.0",
                "id": 36,
                "method": "tools/call",
                "params": {"name": "related_files", "arguments": {"path": target}},
            }, User(username="alice", role="admin"))
            assert related["result"]["structuredContent"]["related"][0]["path"] == "notes/source.md"

            validation = await _handle_message({
                "jsonrpc": "2.0",
                "id": 37,
                "method": "tools/call",
                "params": {"name": "validate_wiki", "arguments": {}},
            }, User(username="alice", role="admin"))
            assert validation["result"]["structuredContent"]["checked_files"] >= 2
        finally:
            mcp_server.parse_users = original_parse

    async def test_upsert_note_move_folder_and_search_status_tools(self, active_user, _setup_users):
        from app import mcp_server

        original_parse = mcp_server.parse_users
        mcp_server.parse_users = lambda: {"tok1": ("alice", "admin")}
        try:
            created = await _handle_message({
                "jsonrpc": "2.0",
                "id": 38,
                "method": "tools/call",
                "params": {"name": "upsert_note", "arguments": {"title": "Folder Note", "content": "Body", "folder": "notes/folder", "tags": ["mcp"]}},
            }, User(username="alice", role="admin"))
            assert created["result"]["structuredContent"]["status"] == "created"

            moved = await _handle_message({
                "jsonrpc": "2.0",
                "id": 39,
                "method": "tools/call",
                "params": {"name": "move_folder", "arguments": {"src": "notes/folder", "dst": "notes/moved"}},
            }, User(username="alice", role="admin"))
            assert moved["result"]["structuredContent"]["moved_files"] == 1
            assert (active_user / "notes/moved/folder-note.md").exists()

            status = await _handle_message({
                "jsonrpc": "2.0",
                "id": 40,
                "method": "tools/call",
                "params": {"name": "search_status", "arguments": {}},
            }, User(username="alice", role="admin"))
            assert status["result"]["structuredContent"]["markdown_files"] >= 3
        finally:
            mcp_server.parse_users = original_parse

    async def test_unauth_access(self):
        """Kein User → isError im Result (MCP-konform)."""
        body = {
            "jsonrpc": "2.0",
            "id": 12,
            "method": "tools/call",
            "params": {"name": "read_file", "arguments": {"path": "notes/test.md"}},
        }
        result = await _handle_message(body, None)
        assert "result" in result
        assert result["result"]["isError"] is True
        assert "Permission denied" in result["result"]["content"][0]["text"]

    async def test_read_without_admin(self, _setup_users):
        from app import mcp_server

        original_parse = mcp_server.parse_users
        mcp_server.parse_users = lambda: {"tok1": ("alice", "read")}
        try:
            body = {
                "jsonrpc": "2.0",
                "id": 13,
                "method": "tools/call",
                "params": {"name": "delete_file", "arguments": {"path": "notes/test.md"}},
            }
            result = await _handle_message(body, User(username="alice", role="read"))
            assert "result" in result
            assert result["result"]["isError"] is True
            assert "Admin permission required" in result["result"]["content"][0]["text"]
        finally:
            mcp_server.parse_users = original_parse

    async def test_unknown_tool(self):
        body = {
            "jsonrpc": "2.0",
            "id": 14,
            "method": "tools/call",
            "params": {"name": "nicht_existent", "arguments": {}},
        }
        result = await _handle_message(body, User(username="alice", role="admin"))
        assert "result" in result
        assert result["result"]["isError"] is True
        assert "Unknown tool" in result["result"]["content"][0]["text"]


class TestGrepRedosHardening:
    """Sichert ab, dass das grep-Tool katastrophales Backtracking ablehnt
    und nicht den Event-Loop blockiert."""

    @pytest.fixture
    def _setup(self, users_map, tmp_path, active_user):
        from app import mcp_server

        users_map(("alice", "tok1", "admin"))
        original = mcp_server.parse_users
        mcp_server.parse_users = lambda: {"tok1": ("alice", "admin")}
        # Datei mit potentiell angreifbarem Inhalt anlegen
        md = tmp_path / "notes" / "victim.md"
        md.parent.mkdir(parents=True, exist_ok=True)
        md.write_text("a" * 200, encoding="utf-8")
        yield mcp_server
        mcp_server.parse_users = original

    async def test_nested_quantifier_pattern_rejected(self, _setup):
        body = {
            "jsonrpc": "2.0",
            "id": 20,
            "method": "tools/call",
            "params": {
                "name": "grep",
                "arguments": {"pattern": "(a+)+$", "path": "."},
            },
        }
        result = await _handle_message(body, User(username="alice", role="admin"))
        # Tool wirft ValueError → isError im Result (MCP-konform)
        assert "result" in result
        assert result["result"]["isError"] is True
        text = result["result"]["content"][0]["text"]
        assert "ReDoS" in text or "rejected" in text.lower()

    async def test_alternation_bomb_rejected(self, _setup):
        body = {
            "jsonrpc": "2.0",
            "id": 21,
            "method": "tools/call",
            "params": {
                "name": "grep",
                "arguments": {"pattern": "(a|a)+$", "path": "."},
            },
        }
        result = await _handle_message(body, User(username="alice", role="admin"))
        assert "result" in result
        assert result["result"]["isError"] is True

    async def test_pathological_pattern_with_long_input_rejected(self, _setup):
        body = {
            "jsonrpc": "2.0",
            "id": 22,
            "method": "tools/call",
            "params": {
                "name": "grep",
                "arguments": {"pattern": "(.*)*$", "path": "."},
            },
        }
        result = await _handle_message(body, User(username="alice", role="admin"))
        assert "result" in result
        assert result["result"]["isError"] is True

    async def test_pattern_too_long_rejected(self, _setup):
        body = {
            "jsonrpc": "2.0",
            "id": 23,
            "method": "tools/call",
            "params": {
                "name": "grep",
                "arguments": {"pattern": "a" * 501, "path": "."},
            },
        }
        result = await _handle_message(body, User(username="alice", role="admin"))
        assert "result" in result
        assert result["result"]["isError"] is True
        assert "too long" in result["result"]["content"][0]["text"].lower()

    async def test_simple_grep_still_works(self, _setup):
        body = {
            "jsonrpc": "2.0",
            "id": 24,
            "method": "tools/call",
            "params": {
                "name": "grep",
                "arguments": {"pattern": "a", "path": "."},
            },
        }
        result = await _handle_message(body, User(username="alice", role="admin"))
        # Normaler Treffer, kein Fehler
        assert "result" in result
        text = result["result"]["content"][0]["text"]
        assert "matches" in text
