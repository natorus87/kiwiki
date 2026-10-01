"""Regressionstests fuer die Befunde aus dem UI-Review (creative-web-director).

Browser-Verhalten (Editor-Vorschau, Tab-Reihenfolge, Escape) prueft
tests/browser_smoke.py; hier stehen die serverseitigen Vertraege. Die
Kanban-ID steht jeweils im Abschnittskommentar.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]


def _client(monkeypatch, lang: str = "de") -> TestClient:
    monkeypatch.setenv("KIWIKI_USERS", "admin:adminkey:admin")
    monkeypatch.setenv("KIWIKI_RATE_LIMIT_ENABLED", "false")
    from app.main import app

    client = TestClient(app)
    client.post(f"/login?lang={lang}", data={"api_key": "adminkey"}, follow_redirects=False)
    return client


def _note(name: str, text: str) -> None:
    from app.tenancy import ensure_user_workspace

    workspace = ensure_user_workspace("admin")
    target = workspace / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _file_view(client: TestClient, path: str) -> str:
    return client.get(f"/ui/file?path={path}", headers={"HX-Request": "true"}).text


# ── t_3a677a55: Editor-Vorschau rendert ueber context.origin() ─────────────

def test_editor_vorschau_faellt_auf_den_default_renderer_zurueck():
    template = (ROOT / "app/templates/editor.html").read_text("utf-8")
    renderer = template.split("customHTMLRenderer:", 1)[1].split("toolbarItems", 1)[0]
    assert "text: function(node, context)" in renderer
    code_lines = [line for line in renderer.splitlines() if not line.strip().startswith("//")]
    assert not any("return undefined" in line for line in code_lines)
    assert renderer.count("return context.origin()") == 2


# ── t_f0dc2f21: genau eine h1 in der Notizansicht ──────────────────────────

def test_keine_css_regel_versteckt_die_erste_body_h1():
    css = (ROOT / "app/static/kiwiki.css").read_text("utf-8")
    assert "markdown-content > h1:first-child" not in css


def test_redundanter_titel_laesst_genau_die_body_h1(monkeypatch):
    _note("notes/gleich.md", "---\ntitle: Gleich\n---\n\n# Gleich\n\nText\n")
    body = _file_view(_client(monkeypatch), "notes/gleich.md")
    assert body.count("<h1") == 1
    assert 'class="file-title"' not in body


def test_abweichender_titel_stuft_body_h1_herab(monkeypatch):
    _note("notes/anders.md", "---\ntitle: Kurz\n---\n\n# Lange Ueberschrift\n\nText\n")
    body = _file_view(_client(monkeypatch), "notes/anders.md")
    assert body.count("<h1") == 1
    assert '<h1 class="file-title">Kurz</h1>' in body
    assert "<h2>Lange Ueberschrift</h2>" in body


# ── t_578152ff: versteckte Tree-Checkboxen sind keine Tab-Stopps ───────────

def test_tree_checkboxen_starten_ohne_tabstopp(monkeypatch):
    _note("notes/x.md", "x")
    tree = _client(monkeypatch).get("/ui/files?path=.").text
    checkboxes = [part for part in tree.split("<input")[1:] if "tree-checkbox" in part]
    assert checkboxes
    assert all('tabindex="-1"' in part.split(">", 1)[0] for part in checkboxes)


def test_auswahlmodus_schaltet_tabstopps_um():
    script = (ROOT / "app/static/kiwiki.js").read_text("utf-8")
    toggle = script.split("function kwToggleSelectMode()", 1)[1].split("\n}\n", 1)[0]
    assert "kwSyncTreeCheckboxFocus(tree)" in toggle


# ── t_80383626: Escape schliesst zuerst die Suchergebnisse ─────────────────

def test_escape_prueft_suchergebnisse_vor_der_sidebar():
    script = (ROOT / "app/static/kiwiki.js").read_text("utf-8")
    handler = script.split("if (e.key === 'Escape') {\n    kwCloseAccountMenu();", 1)[1].split("\n  }\n});", 1)[0]
    assert handler.index("search-results") < handler.index("closeSidebar()")
    assert "kwIsMobileSidebar() && s && s.classList.contains('open')" in handler
    assert "collapsed" not in handler


# ── t_269ef737: Exzerpte ohne Wikilink-Rohsyntax und ohne Titel-Dopplung ───

@pytest.mark.parametrize("markdown,expected", [
    ("Siehe [[architektur]].", "Siehe architektur."),
    ("Siehe [[../../decisions/adr-001|ADR 001]].", "Siehe ADR 001."),
    ("Siehe [[ordner/ziel.md]].", "Siehe ziel."),
])
def test_exzerpt_ersetzt_wikilinks_durch_lesbaren_text(markdown, expected):
    from app.search import content_excerpt

    assert content_excerpt(markdown) == expected


def test_exzerpt_ueberspringt_die_titel_ueberschrift():
    from app.search import content_excerpt

    assert content_excerpt("# Roadmap Q4\n\nZiele", title="Roadmap Q4") == "Ziele"
    assert content_excerpt("# **Roadmap** Q4\n\nZiele", title="roadmap q4") == "Ziele"
    assert content_excerpt("# Anderes\n\nZiele", title="Roadmap Q4") == "Anderes Ziele"


def test_suchtreffer_snippet_ohne_wikilink_klammern(active_user):
    from app import search, storage

    storage.write_file("notes/s.md", "Text mit [[ziel|Anzeige]] und Suchwort.")
    search.init_db()
    search.reindex_all()
    snippet = search.search("Suchwort")[0].snippet
    assert "[[" not in snippet
    assert "Anzeige" in snippet


def test_dashboard_exzerpt_ohne_titel_dopplung(monkeypatch):
    _note("notes/r.md", "---\ntitle: Roadmap Q4\n---\n\n# Roadmap Q4\n\nZiele fuer [[q4|das Quartal]].\n")
    body = _client(monkeypatch).get("/ui/recent-edited").text
    excerpt = body.split('class="recent-excerpt">', 1)[1].split("<", 1)[0]
    assert excerpt == "Ziele fuer das Quartal."


# ── t_ab62b92f: Aufgabenlisten als deaktivierte Checkboxen ─────────────────

def test_aufgabenlisten_werden_zu_checkboxen():
    from app.main import _render_markdown_safe

    html = _render_markdown_safe("- [ ] offen\n- [x] fertig\n- normal\n")
    assert '<li class="task-list-item"><input type="checkbox" disabled> offen</li>' in html
    assert '<li class="task-list-item"><input type="checkbox" disabled checked> fertig</li>' in html
    assert "<li>normal</li>" in html


def test_notizinhalt_kann_kein_input_einschleusen():
    from app.main import _render_markdown_safe

    html = _render_markdown_safe('<input type="checkbox" checked onfocus="x()">\n\n- [ ] <input autofocus>\n')
    assert html.count("<input") == 1
    assert "onfocus" not in html and "autofocus" not in html


def test_aufgabensyntax_in_code_bleibt_woertlich():
    from app.main import _render_markdown_safe

    html = _render_markdown_safe("```\n- [ ] kein Kasten\n```")
    assert "<input" not in html
    assert "- [ ] kein Kasten" in html


# ── t_645b6252: Startseite ─────────────────────────────────────────────────

@pytest.mark.parametrize("lang,title,tab_edited,tab_created", [
    ("de", "Weiterarbeiten", "Zuletzt bearbeitet", "Zuletzt erstellt"),
    ("en", "Pick up where you left off", "Recently edited", "Recently created"),
])
def test_gefuellter_workspace_zeigt_kompakten_kopf_und_eine_liste(monkeypatch, lang, title, tab_edited, tab_created):
    _note("notes/a.md", "---\ntitle: A\ntags: [x]\n---\n\nText\n")
    page = _client(monkeypatch, lang).get("/").text
    assert f'<h1 class="home-title">{title}</h1>' in page
    assert 'class="hero-title"' not in page
    assert page.count('id="recent-list"') == 1
    assert "recent-edited\" class=" not in page and 'id="recent-created"' not in page
    assert f'aria-pressed="true" aria-controls="recent-list"\n                data-recent-endpoint="/ui/recent-edited">{tab_edited}' in page
    assert f'data-recent-endpoint="/ui/recent-created">{tab_created}' in page
    # Tags stehen im selben Raster wie die Liste, also im ersten Viewport.
    grid = page.split('class="dashboard home-grid"', 1)[1].split("</section>", 1)[0]
    assert "/ui/tags?compact=1" in grid


@pytest.mark.parametrize("lang,intro", [
    ("de", "Dein persönlicher Wissensspeicher"),
    ("en", "Your personal knowledge store"),
])
def test_leerer_workspace_behaelt_die_einfuehrung(monkeypatch, lang, intro):
    from app.tenancy import ensure_user_workspace

    workspace = ensure_user_workspace("admin")
    for note in workspace.rglob("*.md"):
        if note.name not in ("index.md", "AGENTS.md"):
            note.unlink()
    page = _client(monkeypatch, lang).get("/").text
    assert 'class="hero-title"' in page
    assert intro in page


def test_recent_umschalter_ist_verdrahtet():
    script = (ROOT / "app/static/kiwiki.js").read_text("utf-8")
    assert "closest('.recent-tab')" in script
    assert "target: '#recent-list'" in script


# ── t_0ad246e7: Feinschliff ────────────────────────────────────────────────

@pytest.mark.parametrize("lang,heading", [("de", "Tags"), ("en", "Tags")])
def test_tags_ansicht_ist_dichte_liste_mit_h1(monkeypatch, lang, heading):
    _note("notes/a.md", "---\ntitle: A\ntags: [alpha]\n---\n\nText\n")
    body = _client(monkeypatch, lang).get("/ui/tags").text
    assert f'<h1 class="subview-title">{heading}</h1>' in body
    assert 'class="tag-index"' in body
    assert "tag-files" not in body and "<details" not in body
    assert ">a</a>" in body  # Dateiname ohne .md direkt sichtbar


@pytest.mark.parametrize("lang,count_label", [("de", "1 Datei"), ("en", "1 file")])
def test_tag_anzahl_hat_lokalisierte_beschriftung(monkeypatch, lang, count_label):
    _note("notes/a.md", "---\ntitle: A\ntags: [alpha]\n---\n\nText\n")
    body = _client(monkeypatch, lang).get("/ui/tags").text
    assert f'<span class="sr-only">{count_label}</span>' in body


def test_suchverlauf_hat_h1(monkeypatch):
    body = _client(monkeypatch).get("/ui/search-history").text
    assert '<h1 class="subview-title">' in body


@pytest.mark.parametrize("lang,heading", [
    ("de", "Notiz bearbeiten: notes/a.md"),
    ("en", "Edit note: notes/a.md"),
])
def test_editor_hat_h1(monkeypatch, lang, heading):
    _note("notes/a.md", "Text")
    page = _client(monkeypatch, lang).get("/editor?path=notes/a.md").text
    assert f'<h1 class="sr-only">{heading}</h1>' in page


@pytest.mark.parametrize("lang,heading", [("de", "Neue Notiz"), ("en", "New note")])
def test_editor_ohne_pfad_hat_h1(monkeypatch, lang, heading):
    page = _client(monkeypatch, lang).get("/editor").text
    assert f'<h1 class="sr-only">{heading}</h1>' in page


def test_einstellungen_laden_den_dateibaum(monkeypatch):
    page = _client(monkeypatch).get("/settings").text
    sidebar = page.split('<aside class="sidebar', 1)[1].split("</aside>", 1)[0]
    assert 'id="file-tree"' in sidebar
    assert 'hx-get="/ui/files?path=."' in sidebar


@pytest.mark.parametrize("lang,title", [
    ("de", "Knowledge Engine ist ausgeschaltet"),
    ("en", "Knowledge Engine is turned off"),
])
def test_knowledge_seite_bei_ausgeschalteter_engine(monkeypatch, lang, title):
    monkeypatch.setenv("KIWIKI_KNOWLEDGE_ENABLED", "false")
    page = _client(monkeypatch, lang).get("/knowledge").text
    assert title in page
    assert "KIWIKI_KNOWLEDGE_ENABLED=true" in page
    assert 'id="knowledge-reindex"' not in page
    assert 'class="knowledge-stats" aria-live="polite" hidden' in page


def test_knowledge_seite_bei_eingeschalteter_engine(monkeypatch):
    monkeypatch.setenv("KIWIKI_KNOWLEDGE_ENABLED", "true")
    page = _client(monkeypatch).get("/knowledge").text
    assert 'id="knowledge-reindex"' in page
    assert "Knowledge Engine ist ausgeschaltet" not in page
    assert 'class="knowledge-stats" aria-live="polite">' in page


@pytest.mark.parametrize("lang,more", [("de", "Weitere Aktionen"), ("en", "More actions")])
def test_notizaktionen_haben_mobiles_ueberlaufmenue(monkeypatch, lang, more):
    _note("notes/a.md", "Text")
    body = _file_view(_client(monkeypatch, lang), "notes/a.md")
    menu = body.split('class="quick-more file-actions-more"', 1)[1].split("</details>", 1)[0]
    assert f'aria-label="{more}"' in menu
    assert "kwExportFile(" in menu and "deleteFile(" in menu
    css = (ROOT / "app/static/kiwiki-polish.css").read_text("utf-8")
    assert ".file-actions:has(.btn-primary) > .file-action-secondary { display: none; }" in css


@pytest.mark.parametrize("lang,hint", [
    ("de", "Den API-Key bekommst du von der Person, die dieses kiwiki betreibt."),
    ("en", "You get your API key from whoever runs this kiwiki."),
])
def test_login_zeigt_nutzerhilfe_statt_betreiberformat(monkeypatch, lang, hint):
    monkeypatch.setenv("KIWIKI_USERS", "admin:adminkey:admin")
    from app.main import app

    page = TestClient(app).get(f"/login?lang={lang}").text
    assert hint in page
    assert "user:key:role" not in page
    assert "KIWIKI_USERS" not in page


def test_kopier_icon_hat_mindestens_24px_ziel():
    css = (ROOT / "app/static/kiwiki-polish.css").read_text("utf-8")
    assert ".btn-copy-path { min-width: 24px; min-height: 24px; }" in css


def test_segment_daumen_wird_auf_das_aktive_segment_vermessen():
    """Unterschiedlich lange Labels: Daumen = exakte Breite des aktiven Buttons, keine Luecke."""
    script = (ROOT / "app/static/kiwiki-recall.js").read_text("utf-8")
    css = (ROOT / "app/static/kiwiki-polish.css").read_text("utf-8")
    assert "--seg-w" in script and "--seg-x" in script
    assert "width: var(--seg-w, 0px);" in css
    assert "--seg-count" not in css


def test_sidebar_oeffnen_spielt_einlauf_animation():
    script = (ROOT / "app/static/kiwiki.js").read_text("utf-8")
    opener = script.split("function openSidebar() {", 1)[1].split("\n}\n", 1)[0]
    assert "kwPlaySidebarOpening(s);" in opener
    css = (ROOT / "app/static/kiwiki-polish.css").read_text("utf-8")
    assert ".sidebar.kw-opening .file-tree > .tree-row" in css
