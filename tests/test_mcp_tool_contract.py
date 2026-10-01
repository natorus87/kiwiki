"""Der Werkzeugssatz ist Teil des MCP-Protokolls, nicht des Codes.

Die Namen und ihre `outputSchema` stehen in der Liste von `_handle_message`
und werden von Clients gecacht. Beim Aufteilen von `mcp_server.py` darf
dadurch kein Werkzeug verschwinden, umbenannt werden oder seinen
`readOnlyHint`/`destructiveHint` verlieren — sonst faellt es erst beim
Nutzer auf, nicht bei uns.

Dieser Test piniert den Satz gegen eine Vorher-Liste. Wer ein Werkzeug
bewusst entfernt, muss die Liste hier anpassen — dann ist es eine
Entscheidung und kein Versehen.
"""

from __future__ import annotations

import asyncio

from app import mcp_server

# Stand vor der Aufteilung von app/mcp_server.py (2026-09-27).
ERWARTETE_WERKZEUGE = frozenset(
    {
        "read_index", "list_files", "read_file", "fetch", "write_file",
        "append_file", "write_many", "chunked_write", "search", "create_note",
        "delete_file", "move_file", "edit", "update_frontmatter", "read_many",
        "build_index", "sort", "list_all_files", "grep", "find", "file_info",
        "read_lines", "recent_files", "backlinks", "move_folder", "preview_edit",
        "replace_many", "validate_wiki", "upsert_note", "related_files",
        "tag_index", "reindex_all", "search_status", "whoami", "git_commit",
        "file_history", "diff", "statistics", "template", "validate_links",
        "link_graph", "rename", "batch_tag", "export", "duplicate_check",
        "ai_summarize", "search_history", "dead_link_check", "grep_status",
        "knowledge_search", "entity_details", "entity_neighbors", "fact_timeline",
        "explain_relation", "knowledge_status", "knowledge_reindex",
    }
)


def _deklarierte_werkzeuge() -> set[str]:
    """Alle Werkzeuge, die der Server in tools/list ausliefert."""
    ergebnis = asyncio.run(mcp_server._handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, None))
    return {werkzeug["name"] for werkzeug in ergebnis["result"]["tools"]}


def _schreiber_aus_registry() -> set[str]:
    """Schreiber sind Handler mit need_write-Guard — abgeleitet, nicht geraten.

    Die hartcodierte Liste in test_lesehinweise pinnt bekannte Schreiber;
    dieser Test faengt neue ein: wer need_write in seinen Handler schreibt,
    ohne die Annotation zu aendern, faellt hier auf (und umgekehrt).
    """
    import inspect

    from app.mcp_tools.common import HANDLERS

    return {name for name, fn in HANDLERS.items()
            if "need_write" in inspect.getsource(fn)}


def test_guards_und_lesehinweise_stimmen_ueberein():
    """Guard und Annotation muessen dasselbe sagen — sonst fragt der Client
    falsch (oder gar nicht).

    Der ChatGPT-Befund: haeufige Nachfragen kommen von korrekten
    Nicht-read-only-Flags plus Sitzungsverhalten des Clients, nicht von
    falschen Flags. Dieser Test haelt den korrekten Stand fest: jeder
    need_write-Handler ist nicht-read-only, jeder andere ist read-only.
    """
    schreiber = _schreiber_aus_registry()
    lesend = set(mcp_server._READ_ONLY_TOOLS)

    assert not (schreiber & lesend), (
        f"schreibend, aber als read-only annotiert: {sorted(schreiber & lesend)}"
    )
    assert not (set(mcp_server._HANDLERS) - schreiber - lesend), (
        "lesend, aber nicht als read-only annotiert: "
        f"{sorted(set(mcp_server._HANDLERS) - schreiber - lesend)}"
    )


def test_idempotent_enthaelt_keine_schreiber():
    """Idempotent heisst: frei wiederholbar. Das gilt nur ohne
    Schreibseiteneffekt — build_index, reindex_all und knowledge_reindex
    standen faelschlich drin (teure Schreibvorgaenge)."""
    schreiber = _schreiber_aus_registry()

    idempotent = {name for name in mcp_server._HANDLERS
                  if mcp_server._tool_annotations(name)["idempotentHint"]}

    assert not (schreiber & idempotent), (
        f"schreibend, aber als idempotent annotiert: {sorted(schreiber & idempotent)}"
    )
    assert idempotent <= set(mcp_server._READ_ONLY_TOOLS), (
        "idempotent ausserhalb von read-only: "
        f"{sorted(idempotent - set(mcp_server._READ_ONLY_TOOLS))}"
    )


def test_werkzeugssatz_ist_unveraendert():
    """Der Satz darf nur wachsen, wenn eine Liste hier mitwaechst."""
    aktuell = _deklarierte_werkzeuge()

    fehlend = ERWARTETE_WERKZEUGE - aktuell
    assert not fehlend, f"Werkzeuge verschwunden: {sorted(fehlend)}"

    neu = aktuell - ERWARTETE_WERKZEUGE
    assert not neu, f"neue Werkzeuge ohne Eintrag in ERWARTETE_WERKZEUGE: {sorted(neu)}"


def test_jedes_werkzeug_hat_einen_registry_handler():
    """Ein Werkzeug ohne Handler in _HANDLERS waere tot — tools/list wuerde es
    anbieten, der Aufruf wuerde aber mit 'unbekanntes Werkzeug' enden.

    Der Test pruefte frueher per Quelltext-Suche nach `name == \"...\"` in
    _dispatch. Seit dem Registry-Umbau steht die Verzweigung als
    Registereintrag — der Schutz ist derselbe, nur der Ort hat sich
    geaendert.
    """
    ohne_handler = [name for name in ERWARTETE_WERKZEUGE if name not in mcp_server._HANDLERS]

    assert not ohne_handler, (
        f"in _HANDLERS nicht registriert: {sorted(ohne_handler)} — "
        "die Werkzeugliste waere dann groesser als die Registry"
    )


def test_registry_enthaelt_genau_den_erwarteten_werkzeugsatz():
    """Kein Handler ohne Werkzeuglisteneintrag — und umgekehrt.

    Ein Ueberhang in eine Richtung waere ein totes Angebot (listet, laeuft
    nicht) oder eine Schattenfunktion (laeuft, wird nicht angeboten und hat
    kein Schema).
    """
    assert set(mcp_server._HANDLERS) == ERWARTETE_WERKZEUGE, (
        f"Registry driftet: extra={sorted(set(mcp_server._HANDLERS) - ERWARTETE_WERKZEUGE)} "
        f"fehlend={sorted(ERWARTETE_WERKZEUGE - set(mcp_server._HANDLERS))}"
    )


def test_lesehinweise_bleiben_konsistent_zur_dispatch_menge():
    """Ein Werkzeug darf nicht als read-only gelistet sein und trotzdem schreiben.

    Die Annotation steuert, ob ein MCP-Client den Aufruf ohne Rueckfrage
    ausfuehrt. Eine falsche readOnlyHint ist eine Rechteausweitung im
    Client-Verhalten, nicht nur eine falsche Beschriftung.
    """
    lesend = set(mcp_server._READ_ONLY_TOOLS)
    zerstoerend = set(mcp_server._DESTRUCTIVE_TOOLS)

    # Schreibende Werkzeuge duerfen nicht als read-only gelten.
    schreiber = {"write_file", "append_file", "write_many", "chunked_write",
                 "create_note", "delete_file", "move_file", "edit",
                 "update_frontmatter", "sort", "move_folder", "replace_many",
                 "build_index", "reindex_all", "knowledge_reindex", "rename",
                 "batch_tag", "upsert_note", "git_commit"}
    fehlerhaft = lesend & schreiber
    assert not fehlerhaft, f"als read-only annotiert, aber schreibend: {sorted(fehlerhaft)}"

    # Die Annotationen muessen echte Werkzeuge sein.
    unbekannt = (lesend | zerstoerend) - ERWARTETE_WERKZEUGE
    assert not unbekannt, f"Annotation fuer unbekanntes Werkzeug: {sorted(unbekannt)}"


def test_zerstoerende_werkzeuge_bleiben_als_solche_annotiert():
    """Loeschen, Verschieben und Ersetzen muessen als destruktiv gelten.

    Das ist die Annotation, an der ein Client erkennt, dass er nachfragen
    soll. Faellt sie weg, loescht ein Agent im Namen des Nutzers, ohne zu
    fragen.
    """
    pflicht = {"delete_file", "move_file", "move_folder", "sort", "replace_many", "rename"}
    fehlend = pflicht - set(mcp_server._DESTRUCTIVE_TOOLS)
    assert not fehlend, f"nicht mehr als destruktiv annotiert: {sorted(fehlend)}"


def test_werkzeugliste_und_schemata_sind_kopplungsvoll():
    """Jedes Werkzeug braucht ein outputSchema — und umgekehrt.

    Beim Aufteilen der Datei ist das die Stelle, an der sich ein Fehler
    zuerst zeigt: `_OUTPUT_SCHEMAS` wird ueber den Werkzeugnamen indiziert,
    also bricht schon der Import, wenn ein Name nur auf einer Seite
    auftaucht.

    Der Test greift trotzdem zuerst, weil er die Schemata ueber den
    Quelltext zaehlt statt sie zu importieren: ein Importfehler wuerde sonst
    als pytest-Collection-Error erscheinen und nicht als Befund mit einer
    lesbaren Meldung.

    Das Muster deckt beide Schreibweisen der Tabelle ab: der Schluessel steht
    entweder allein auf der Zeile (dann folgt ein Schemakoerper) oder mit
    einer Referenz auf eine Konstante (`"write_file": _STATUS_SCHEMA,`).

    Gescannt wird nur der Block der Tabelle selbst. Ohne diese Begrenzung
    zaehlten die Feldnamen verschachtelter JSON-Schemata mit
    ("properties", "required", "type") — die sind keine Werkzeugnamen.
    """
    import re

    with open(mcp_server.__file__, encoding="utf-8") as handle:
        text = handle.read()

    start = text.index("\n_OUTPUT_SCHEMAS = {")
    # Die Tabelle endet, wenn ein Eintrag nicht mehr mit 4 Spaces eingerueckt ist.
    ende = re.search(r"\n\}\n", text[start:])
    assert ende, "die Schema-Tabelle wurde nicht gefunden — Struktur hat sich geaendert"
    block = text[start : start + ende.start()]

    deklariert = _deklarierte_werkzeuge()
    schema_namen = set(re.findall(r'^    "([a-z_]+)":', block, re.M))

    assert schema_namen, "die Schema-Tabelle wurde nicht gefunden — Struktur hat sich geaendert"

    ohne_schema = deklariert - schema_namen
    assert not ohne_schema, f"Werkzeug ohne outputSchema: {sorted(ohne_schema)}"

    ohne_werkzeug = schema_namen - deklariert
    assert not ohne_werkzeug, f"Schema ohne Werkzeug (toter Eintrag): {sorted(ohne_werkzeug)}"


def test_die_kopplung_bricht_verschaerft_statt_still_aus():
    """Beim Aufteilen ist die Reihenfolge entscheidend.

    `_OUTPUT_SCHEMAS` wird ueber den Werkzeugnamen indiziert, und
    `_handle_message` schreibt jedes Ergebnis zurueck. Ein Werkzeug ohne
    Schema laesst deshalb schon den Import scheitern, nicht erst den
    Aufruf. Das ist richtig — aber es heisst: wer die Datei aufteilt, muss
    die Werkzeugliste und die Schemata gemeinsam verschieben, sonst
    verliert er nicht eine Faehigkeit, sondern das ganze Modul.

    Dieser Test haelt die Kopplung fest, damit das eine bewusste
    Entscheidung bleibt und nicht beim naechsten Umbau still veraendert wird.
    """
    with open(mcp_server.__file__, encoding="utf-8") as handle:
        text = handle.read()

    # Beide Zugriffe muessen ueber denselben Namen gehen.
    assert '_tool["outputSchema"] = _OUTPUT_SCHEMAS[_tool["name"]]' in text, (
        "die Zuordnung Werkzeug -> Schema darf nicht indirekt werden, "
        "sonst verliert diese Kopplung ihre Striktheit"
    )


def test_server_version_stimmt_mit_app_constants():
    """serverInfo kommt aus APP_VERSION; ein zweiter hartkodierter Wert
    wuerde bei der Aufteilung auseinanderlaufen."""
    from app.constants import APP_VERSION

    assert APP_VERSION == "4.3.0"
    assert mcp_server.APP_VERSION == APP_VERSION
