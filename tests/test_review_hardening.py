"""Regressionstests fuer die Befunde aus dem Code-Review vom 2026-09-29.

Je ein Abschnitt pro Kanban-Karte (Board `kiwiki`); die Karten-ID steht im
Abschnittskommentar, damit der Zusammenhang ohne Chat-Verlauf nachvollziehbar
bleibt.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _client_with_session(monkeypatch, users: str = "alice:alicekey:admin", key: str = "alicekey") -> TestClient:
    monkeypatch.setenv("KIWIKI_USERS", users)
    monkeypatch.setenv("KIWIKI_RATE_LIMIT_ENABLED", "false")
    from app.main import app

    client = TestClient(app)
    antwort = client.post("/login", data={"api_key": key}, follow_redirects=False)
    assert antwort.status_code == 303
    return client


# ── t_947d9358: Dashboard mutiert den list_all_files-Cache ──────────────────

def test_dashboard_excerpts_veraendern_den_gemeinsamen_cache_nicht(active_user):
    from app import main, storage

    storage.create_note("Hallo", "Body text", ["x"], "alice")
    angezeigt = [f for f in storage.list_all_files(".") if f["path"] not in ("index.md", "AGENTS.md")]
    main._attach_excerpts(angezeigt)
    assert angezeigt and "excerpt" in angezeigt[0]

    erneut = storage.list_all_files(".")
    assert all("excerpt" not in item for item in erneut)


def test_list_all_files_bleibt_nach_dashboard_schemakonform(active_user):
    jsonschema = pytest.importorskip("jsonschema")
    from app import main, storage
    from app.mcp_server import TOOLS, _handle_message
    from app.models import User

    storage.create_note("Hallo", "Body", ["x"], "alice")
    main._attach_excerpts([f for f in storage.list_all_files(".")])

    antwort = asyncio.run(_handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "list_all_files", "arguments": {}}},
        User(username="alice", role="admin"),
    ))
    schema = next(t for t in TOOLS if t["name"] == "list_all_files")["outputSchema"]
    jsonschema.validate(antwort["result"]["structuredContent"], schema)


def test_list_all_files_kopien_teilen_keine_tag_listen(active_user):
    from app import storage

    storage.create_note("Hallo", "Body", ["x"], "alice")
    erste = storage.list_all_files(".")
    next(f for f in erste if f["title"] == "Hallo")["tags"].append("manipuliert")
    zweite = storage.list_all_files(".")
    assert next(f for f in zweite if f["title"] == "Hallo")["tags"] == ["x"]


# ── t_6cc27e48: Markdown darf keine UI-Steuerklassen setzen ────────────────

@pytest.mark.parametrize("klasse", ["kw-file-link", "kw-view-link", "kw-search-link", "btn btn-danger"])
def test_markdown_klassen_auf_links_werden_entfernt(klasse):
    from app.main import _render_markdown_safe

    html = _render_markdown_safe(f'<a class="{klasse}" href="https://evil.example">Klick</a>')
    assert "class=" not in html
    assert 'href="https://evil.example"' in html


def test_wikilink_klassen_bleiben_erhalten(active_user):
    from app.main import _render_markdown_safe

    html = _render_markdown_safe("[[Geist]] und <a class=\"wikilink kw-file-link\" href=\"/x\">y</a>", "notes/a.md")
    assert '<a class="wikilink missing" href="/?file=notes/Geist.md">Geist</a>' in html
    assert 'class="wikilink"' in html
    assert "kw-file-link" not in html


def test_codeblock_sprachklasse_bleibt_erhalten():
    from app.main import _render_markdown_safe

    html = _render_markdown_safe("```python\nx = 1\n```")
    assert 'class="language-python"' in html


# ── t_b5939bd1: Bearer-Header umgeht die Session-Pruefung der UI ───────────

@pytest.mark.parametrize("method,url", [
    ("GET", "/editor?path=index.md"),
    ("GET", "/ui/file?path=index.md"),
    ("GET", "/ui/tags"),
    ("POST", "/ui/search"),
    ("POST", "/ui/export"),
    ("GET", "/settings"),
])
def test_bearer_header_ersetzt_keine_ui_session(monkeypatch, method, url):
    monkeypatch.setenv("KIWIKI_USERS", "alice:alicekey:admin")
    from app.main import app

    antwort = TestClient(app).request(
        method, url, headers={"Authorization": "Bearer garbage"}, follow_redirects=False,
    )
    assert antwort.status_code == 302
    assert antwort.headers["location"] == "/login"


def test_json_rpc_an_root_mit_bearer_bleibt_erreichbar(monkeypatch):
    monkeypatch.setenv("KIWIKI_USERS", "alice:alicekey:admin")
    from app.main import app

    antwort = TestClient(app).post(
        "/",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Authorization": "Bearer alicekey"},
        follow_redirects=False,
    )
    assert antwort.status_code == 200
    assert "tools" in antwort.json()["result"]


def test_ui_export_mit_session_funktioniert(monkeypatch):
    client = _client_with_session(monkeypatch)
    antwort = client.post("/ui/export", data={"path": "index.md"})
    assert antwort.status_code == 200
    assert "kiwiki" in antwort.text


# ── t_af2ee055: Editor-Vorschau loest Wikilinks wie der Server auf ─────────

def _editor_resolver_js() -> str:
    template = (Path(__file__).resolve().parent.parent / "app" / "templates" / "editor.html").read_text("utf-8")
    match = re.search(r"function kwResolveWikilink\(sourcePath, rawTarget\) \{.*?\n  \}\n", template, re.DOTALL)
    assert match, "kwResolveWikilink fehlt in editor.html"
    return match.group(0)


FAELLE = [
    ("notes/a.md", "Ziel"),
    ("notes/a.md", "sub/Ziel.md"),
    ("notes/sub/a.md", "../Ziel"),
    ("a.md", "../../etc/passwd"),
    ("a.md", "https://example.com/x"),
    ("a.md", "/abs"),
    ("a.md", "#anker"),
    ("notes/a.md", "./x"),
    ("", "Ziel"),
    ("notes/a.md", "   "),
]


def test_editor_und_server_loesen_wikilinks_identisch():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node nicht installiert")
    from app.main import _resolve_wikilink

    script = _editor_resolver_js() + (
        "const faelle = JSON.parse(process.argv[1]);"
        "console.log(JSON.stringify(faelle.map(([s, t]) => kwResolveWikilink(s, t))));"
    )
    ausgabe = subprocess.run(
        [node, "-e", script, json.dumps(FAELLE)], capture_output=True, text=True, timeout=20, check=True,
    ).stdout
    js = json.loads(ausgabe)
    py = [_resolve_wikilink(s, t) for s, t in FAELLE]
    assert js == py


def test_editor_vorschau_nutzt_den_dateipfad_als_quelle():
    template = (Path(__file__).resolve().parent.parent / "app" / "templates" / "editor.html").read_text("utf-8")
    assert "kwPreviewLinkify(node.literal, kwPreviewSourcePath())" in template


# ── t_9cba1679: OAuth-Registrierungszaehler waechst unbegrenzt ─────────────

def test_register_hits_fremder_quellen_werden_bereinigt(monkeypatch):
    from app import mcp_server

    monkeypatch.setattr(mcp_server, "_oauth_register_hits", {
        "198.51.100.1": [0.0],
        "198.51.100.2": [1000.0, 5000.0],
    })
    mcp_server._prune_oauth_register_hits(now=5000.0 + 10)
    assert mcp_server._oauth_register_hits == {"198.51.100.2": [5000.0]}


def test_register_hits_haben_eine_obergrenze(monkeypatch):
    from app import mcp_server

    monkeypatch.setattr(mcp_server, "_OAUTH_REGISTER_SOURCES_MAX", 2)
    monkeypatch.setattr(mcp_server, "_oauth_register_hits", {
        "a": [100.0], "b": [300.0], "c": [200.0],
    })
    mcp_server._prune_oauth_register_hits(now=400.0)
    assert set(mcp_server._oauth_register_hits) == {"b", "c"}


def test_replay_grenze_ist_dokumentiert():
    security = (Path(__file__).resolve().parent.parent / "SECURITY.md").read_text("utf-8")
    assert "Refresh-token replay protection is process-local" in security


# ── t_3855da9e: Wikilinks in Code, Titel-Doppelung ─────────────────────────

@pytest.mark.parametrize("markdown", [
    "~~~\n[[x]]\n~~~",
    "```\n[[x]]\n```",
    "````\n```\n[[x]]\n````",
    "Text `[[x]]` Text",
    "Text ``a ` [[x]]`` Text",
])
def test_wikilinks_in_code_bleiben_woertlich(markdown):
    from app.main import _render_markdown_safe

    html = _render_markdown_safe(markdown, "a.md")
    assert "[[x]]" in html
    assert "wikilink" not in html
    assert "&lt;a" not in html


def test_wikilink_nach_codeblock_wird_trotzdem_verlinkt(active_user):
    from app.main import _render_markdown_safe

    html = _render_markdown_safe("~~~\n[[a]]\n~~~\n\nSiehe [[b]] und `[[c]]`", "notes/n.md")
    assert html.count('class="wikilink missing"') == 1
    assert 'href="/?file=notes/b.md"' in html
    assert "[[a]]" in html and "[[c]]" in html


def test_erste_ueberschrift_ignoriert_trennlinie_und_code():
    from app.main import _first_content_heading

    assert _first_content_heading("---\nText\n---\n# Echt") == "echt"
    assert _first_content_heading("```bash\n# Kommentar\n```\n## **Titel**") == "titel"
    assert _first_content_heading("#hashtag\nText") == ""


# ── t_b508b8c1: Session-Cookie-Haertung ────────────────────────────────────

def test_session_cookie_ist_bei_direktem_tls_secure(monkeypatch):
    monkeypatch.setenv("KIWIKI_USERS", "alice:alicekey:admin")
    monkeypatch.setenv("KIWIKI_TRUST_PROXY", "false")
    monkeypatch.setenv("KIWIKI_RATE_LIMIT_ENABLED", "false")
    from app.main import app

    antwort = TestClient(app, base_url="https://testserver").post(
        "/login", data={"api_key": "alicekey"}, follow_redirects=False,
    )
    cookie = antwort.headers["set-cookie"]
    assert "kiwiki_session=" in cookie
    assert "Secure" in cookie


def test_session_cookie_ohne_tls_und_proxy_nicht_secure(monkeypatch):
    monkeypatch.setenv("KIWIKI_USERS", "alice:alicekey:admin")
    monkeypatch.setenv("KIWIKI_TRUST_PROXY", "false")
    monkeypatch.setenv("KIWIKI_RATE_LIMIT_ENABLED", "false")
    from app.main import app

    antwort = TestClient(app).post("/login", data={"api_key": "alicekey"}, follow_redirects=False)
    assert "Secure" not in antwort.headers["set-cookie"]


def test_roher_api_key_im_cookie_warnt_einmal(monkeypatch, caplog):
    monkeypatch.setenv("KIWIKI_USERS", "alice:alicekey:admin")
    from app.main import app

    client = TestClient(app)
    client.cookies.set("kiwiki_session", "alicekey")
    with caplog.at_level(logging.WARNING, logger="kiwiki.auth"):
        for _ in range(2):
            assert client.get("/api/files").status_code == 200
    warnungen = [r for r in caplog.records if "Deprecated" in r.getMessage()]
    assert len(warnungen) == 1


# ── t_f0da831a: users.yaml wird nicht pro Aufruf neu geparst ───────────────

def test_users_yaml_wird_gecacht_und_bei_aenderung_neu_gelesen(monkeypatch, tmp_path):
    from app import user_store

    monkeypatch.delenv("KIWIKI_USERS", raising=False)
    user_store.create_local_user("bob", "bobkey", "read")
    aufrufe = []
    original = user_store._load_local_users

    def gezaehlt(path):
        aufrufe.append(path)
        return original(path)

    monkeypatch.setattr(user_store, "_load_local_users", gezaehlt)
    for _ in range(5):
        assert "bobkey" in user_store.local_users_by_key()
    assert len(aufrufe) == 1

    # Manuelle Aenderung (anderer Inhalt, andere Groesse) wird ohne Neustart wirksam.
    pfad = tmp_path / ".kiwiki" / "users.yaml"
    pfad.write_text("users:\n  - username: carol\n    key: carolkey123\n    role: write\n", encoding="utf-8")
    nutzer = user_store.local_users_by_key()
    assert "carolkey123" in nutzer and "bobkey" not in nutzer
    assert len(aufrufe) == 2


def test_eigene_schreibvorgaenge_invalidieren_den_cache(monkeypatch):
    from app import user_store

    monkeypatch.delenv("KIWIKI_USERS", raising=False)
    user_store.create_local_user("bob", "bobkey", "read")
    assert "bobkey" in user_store.local_users_by_key()
    user_store.delete_local_user("bob")
    assert user_store.local_users_by_key() == {}


def test_rueckgabe_ist_kopie_des_caches(monkeypatch):
    from app import user_store

    monkeypatch.delenv("KIWIKI_USERS", raising=False)
    user_store.create_local_user("bob", "bobkey", "read")
    user_store.local_users_by_key().clear()
    assert "bobkey" in user_store.local_users_by_key()
