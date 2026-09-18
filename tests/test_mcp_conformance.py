"""Konformanz aller MCP-Werkzeuge gegen ihre eigenen Schemas.

Seit Revision 2025-06-18 validieren Clients `structuredContent` gegen das
deklarierte `outputSchema`. Weicht ein einziges Feld ab, verwirft ein strikter
Client die komplette Antwort — nicht nur den betroffenen Eintrag. Genau so sind
`list_all_files` (undeklariertes `created`), `grep_status` (`result: null`),
`template` (undeklariertes `template_type`) und die frei getippten
Frontmatter-Werte durchgerutscht: die bisherigen Tests prüften nur, *dass* ein
Schema existiert, nie ob die Antwort es einhält.

Dieser Test ruft jedes Werkzeug einmal auf und validiert das Ergebnis. Die
Argument-Tabelle muss jedes Werkzeug abdecken — ein neues Werkzeug ohne Eintrag
lässt `test_argumenttabelle_deckt_jedes_werkzeug_ab` fehlschlagen, damit die
Prüfung nicht still verrottet.
"""

import json
import subprocess

import pytest
from jsonschema import Draft202012Validator

from app.mcp_server import TOOLS, _dispatch
from app.models import User

_SCHEMAS = {tool["name"]: tool for tool in TOOLS}

_KNOWLEDGE_TOOLS = [
    "knowledge_status",
    "knowledge_search",
    "knowledge_reindex",
    "entity_details",
    "entity_neighbors",
    "fact_timeline",
    "explain_relation",
]

# Ein gültiger Aufruf je Werkzeug. Die Argumente werden gegen das inputSchema
# geprüft, damit die Tabelle nicht unbemerkt von den Werkzeugen wegdriftet.
_TOOL_ARGS: dict[str, dict] = {
    "ai_summarize": {"path": "notes/a.md"},
    "append_file": {"path": "notes/a.md", "content": "mehr"},
    "backlinks": {"path": "notes/b.md"},
    "batch_tag": {"files": ["notes/a.md"], "tags": ["neu"]},
    "build_index": {},
    "chunked_write": {
        "path": "notes/c.md", "chunk": "Inhalt", "chunk_index": 0,
        "total_chunks": 1, "finalize": True,
    },
    "create_note": {"title": "Neu", "content": "Text"},
    "dead_link_check": {},
    "delete_file": {"path": "notes/a.md"},
    "diff": {},
    "duplicate_check": {},
    "edit": {"path": "notes/a.md", "new_str": "Hi", "old_str": "Hallo"},
    "entity_details": {"entity_id": "doc:notes/a.md"},
    "entity_neighbors": {"entity_id": "doc:notes/a.md"},
    "explain_relation": {"relation_id": "rel-1"},
    "export": {},
    "fact_timeline": {"entity_id": "doc:notes/a.md"},
    "fetch": {"id": "notes/a.md"},
    "file_history": {"path": "notes/a.md"},
    "file_info": {"path": "notes/a.md"},
    "find": {"pattern": "*.md"},
    "git_commit": {"message": "test"},
    "grep": {"pattern": "Hallo"},
    "grep_status": {"job_id": "unbekannt"},
    "knowledge_reindex": {},
    "knowledge_search": {"query": "Hallo"},
    "knowledge_status": {},
    "link_graph": {},
    "list_all_files": {},
    "list_files": {},
    "move_file": {"src": "notes/a.md", "dst": "notes/z.md"},
    "move_folder": {"src": "notes", "dst": "archiv"},
    "preview_edit": {"path": "notes/a.md", "new_str": "Hi", "old_str": "Hallo"},
    "read_file": {"path": "notes/a.md"},
    "read_index": {},
    "read_lines": {"path": "notes/a.md", "start": 1, "end": 3},
    "read_many": {"paths": ["notes/a.md", "notes/b.md"]},
    "recent_files": {},
    "reindex_all": {},
    "related_files": {"path": "notes/a.md"},
    "rename": {"old_path": "notes/a.md", "new_path": "notes/umbenannt.md"},
    "replace_many": {
        "paths": ["notes/a.md"],
        "replacements": [{"old_str": "Hallo", "new_str": "Hi"}],
    },
    "search": {"query": "Hallo"},
    "search_history": {},
    "search_status": {},
    "sort": {"moves": [{"src": "notes/a.md", "dst": "archiv/a.md"}]},
    "statistics": {},
    "tag_index": {},
    "template": {"template_type": "meeting", "title": "Standup"},
    "update_frontmatter": {"path": "notes/a.md", "updates": {"tags": ["q"]}},
    "upsert_note": {"title": "Upsert", "content": "Text"},
    "validate_links": {},
    "validate_wiki": {},
    "whoami": {},
    "write_file": {"path": "notes/w.md", "content": "---\ntitle: W\n---\n\nText"},
    "write_many": {"files": [{"path": "notes/m.md", "content": "Z"}]},
}

# Bewusst unbequemes, aber voellig gueltiges YAML: `title: 2026` ist ein int,
# `tags: python` ein Skalar statt einer Liste, die Datumsangaben sind unquotiert
# (PyYAML liest sie als datetime.date) und `.nan` ist in JSON gar nicht
# darstellbar. Genau solche Notizen haben die Werkzeuge reihenweise aus ihren
# Schemas fallen lassen — der Sweep muss sie deshalb als Normalfall behandeln.
_NOTE_A = (
    "---\ntitle: 2026\ntags: python\ncreated: 2026-01-01\nupdated: 2026-01-02\nscore: .nan\n---\n\n"
    "# A\n\nHallo Welt [[notes/b]]\n"
)
_NOTE_B = '---\ntitle: B\ntags: [python]\n---\n\n# B\n\nZweite Notiz\n'


def _git(root, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _seed_workspace(root) -> None:
    """Workspace mit Notizen, Verlinkung und Git-Historie aufbauen.

    `diff` braucht zwei Commits, `git_commit` eine noch nicht committete
    Änderung — deshalb bleibt notes/b.md am Ende bewusst schmutzig.
    """
    (root / "notes").mkdir(parents=True, exist_ok=True)
    (root / "notes" / "a.md").write_text(_NOTE_A, encoding="utf-8")
    (root / "notes" / "b.md").write_text(_NOTE_B, encoding="utf-8")
    (root / "index.md").write_text("# Index\n", encoding="utf-8")
    (root / "AGENTS.md").write_text("# Agents\n", encoding="utf-8")

    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "eins")
    (root / "notes" / "a.md").write_text(_NOTE_A + "\nErgaenzung\n", encoding="utf-8")
    _git(root, "commit", "-qam", "zwei")
    (root / "notes" / "b.md").write_text(_NOTE_B + "\nNoch nicht committet\n", encoding="utf-8")


async def _aufrufen_und_pruefen(name: str) -> None:
    user = User(username="alice", api_key="test-key", role="admin")
    schema = _SCHEMAS[name]["outputSchema"]

    text = await _dispatch(name, _TOOL_ARGS[name], user)

    def _kein_json_literal(constant):
        raise AssertionError(
            f"{name} liefert {constant}; RFC 8259 kennt weder NaN noch Infinity, "
            "strikte Client-Parser scheitern an der gesamten Antwort"
        )

    payload = json.loads(text, parse_constant=_kein_json_literal)
    assert isinstance(payload, dict), (
        f"{name} liefert {type(payload).__name__}; structuredContent muss ein Objekt sein"
    )
    fehler = sorted(Draft202012Validator(schema).iter_errors(payload), key=lambda e: list(e.path))
    assert not fehler, "\n".join(f"{name}: {list(e.path)} — {e.message}" for e in fehler[:5])


def test_argumenttabelle_deckt_jedes_werkzeug_ab():
    """Ein neues Werkzeug ohne Eintrag darf nicht stillschweigend ungeprüft bleiben."""
    assert set(_TOOL_ARGS) == set(_SCHEMAS)


def test_schemas_sind_gueltiges_jsonschema():
    problems = []
    for tool in TOOLS:
        for key in ("inputSchema", "outputSchema"):
            try:
                Draft202012Validator.check_schema(tool[key])
            except Exception as exc:
                problems.append(f"{tool['name']}.{key}: {exc}")
    assert not problems, "\n".join(problems)


def test_required_ist_teilmenge_von_properties():
    """Ein required-Feld ohne properties-Eintrag ist nie erfüllbar."""
    problems = []
    for tool in TOOLS:
        for key in ("inputSchema", "outputSchema"):
            schema = tool[key]
            props, required = set(schema.get("properties", {})), set(schema.get("required", []))
            if props and not required <= props:
                problems.append(f"{tool['name']}.{key}: {sorted(required - props)}")
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("name", sorted(_TOOL_ARGS))
def test_testargumente_erfuellen_das_input_schema(name):
    """Sonst prüft der Konformanztest einen Aufruf, den kein Client so senden würde."""
    fehler = list(Draft202012Validator(_SCHEMAS[name]["inputSchema"]).iter_errors(_TOOL_ARGS[name]))
    assert not fehler, "\n".join(f"{list(e.path)} — {e.message}" for e in fehler[:5])


@pytest.mark.parametrize("name", sorted(_TOOL_ARGS))
async def test_ergebnis_haelt_das_output_schema_ein(active_user, name):
    _seed_workspace(active_user)
    await _aufrufen_und_pruefen(name)


@pytest.mark.parametrize("name", _KNOWLEDGE_TOOLS)
async def test_wissensmaschine_haelt_ihr_schema_auch_aktiviert_ein(active_user, monkeypatch, name):
    """Aktiviert liefern diese Werkzeuge andere Felder als im Ruhezustand."""
    monkeypatch.setenv("KIWIKI_KNOWLEDGE_ENABLED", "true")
    _seed_workspace(active_user)
    await _aufrufen_und_pruefen(name)
