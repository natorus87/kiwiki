"""Regressions-Tests fuer die Feinschliff-Runde der Web-UI.

Jeder Test hier reproduziert einen Fehler, der im Browser sichtbar war:

* Die Live-Suche hat nie gefeuert, weil ``hx-trigger`` auf dem ``<form>``
  den Modifier ``changed`` trug. htmx vergleicht dafuer ``elt.value``, und
  ein Formular hat keines — der Vergleich war immer ``undefined ===
  undefined`` und hat jedes Eingabe-Event verworfen.
* Die Tag-Uebersicht las nur das Wurzelverzeichnis und meldete "keine Tags",
  sobald Notizen in Ordnern lagen.
* Zeitstempel standen roh als ``2026-09-19T17:42:00`` in den Listen.
* Der Dateibaum klappte bei jedem Seitenwechsel wieder zu.
"""

from fastapi.testclient import TestClient

from app.i18n import format_stamp, stamp_title
from app.main import app
from app.search import _clean_snippet


def _client(monkeypatch, users: str = "admin:adminkey:admin") -> TestClient:
    monkeypatch.setenv("KIWIKI_USERS", users)
    from app import auth as auth_mod
    from app import user_store as user_store_mod

    auth_mod._PARSE_DIAG_LOGGED = False
    user_store_mod._PARSE_DIAG_LOGGED = False
    client = TestClient(app)
    client.post("/login", data={"api_key": "adminkey"}, follow_redirects=False)
    return client


# ── Live-Suche ──────────────────────────────────────────────────────────────

def test_suchformular_hat_keinen_changed_modifier():
    """"changed" auf einem <form> deaktiviert die Live-Suche vollstaendig."""
    layout = (
        __import__("pathlib").Path(__file__).resolve().parents[1]
        / "app/templates/layout.html"
    ).read_text(encoding="utf-8")
    trigger = layout.split('hx-trigger="', 1)[1].split('"', 1)[0]

    assert "input" in trigger
    assert "submit" in trigger
    assert "changed" not in trigger


# ── Tag-Uebersicht ──────────────────────────────────────────────────────────

def test_tag_uebersicht_findet_tags_in_unterordnern(monkeypatch):
    from app.tenancy import ensure_user_workspace

    monkeypatch.setenv("KIWIKI_USERS", "admin:adminkey:admin")
    ws = ensure_user_workspace("admin")
    (ws / "projekte").mkdir(parents=True, exist_ok=True)
    (ws / "projekte" / "migration.md").write_text(
        "---\ntitle: Migration\ntags: [kubernetes, infra]\n---\n\nInhalt\n",
        encoding="utf-8",
    )

    body = _client(monkeypatch).get("/ui/tags").text

    assert "kubernetes" in body
    assert "infra" in body


# ── Zeitstempel ─────────────────────────────────────────────────────────────

def test_zeitstempel_werden_lesbar_formatiert():
    from datetime import date, datetime, timedelta

    now = datetime.now()
    assert format_stamp(now.replace(hour=9, minute=5)).startswith("Heute, 09:05")
    assert format_stamp(now - timedelta(days=1), "en").startswith("Yesterday")
    assert format_stamp(now - timedelta(days=3)) == "vor 3 Tagen"
    assert format_stamp(date(2021, 3, 7)) == "7. März 2021"
    assert format_stamp(date(2021, 3, 7), "en") == "Mar 7, 2021"


def test_zeitstempel_mit_zone_wird_in_ortszeit_gezeigt():
    """Ein "...Z" aus einem MCP-Client darf nicht als Ortszeit durchgereicht werden."""
    from datetime import datetime, timezone

    utc_noon = datetime(2021, 3, 7, 12, 0, tzinfo=timezone.utc)
    expected = utc_noon.astimezone().replace(tzinfo=None)

    assert format_stamp("2021-03-07T12:00:00Z") == format_stamp(expected)
    assert stamp_title("2021-03-07T12:00:00Z") == expected.strftime("%d.%m.%Y, %H:%M")


def test_unparsbare_zeitstempel_bleiben_unveraendert():
    """Frontmatter darf beliebigen Text enthalten — der geht nicht verloren."""
    assert format_stamp("irgendwann") == "irgendwann"
    assert stamp_title("irgendwann") == "irgendwann"
    assert format_stamp(None) == ""


def test_dashboard_rendert_keine_iso_stempel(monkeypatch):
    from app.tenancy import ensure_user_workspace

    monkeypatch.setenv("KIWIKI_USERS", "admin:adminkey:admin")
    ws = ensure_user_workspace("admin")
    (ws / "notes").mkdir(parents=True, exist_ok=True)
    (ws / "notes" / "stamped.md").write_text(
        "---\ntitle: Stamped\nupdated: 2021-03-07T17:42:00\n---\n\nInhalt\n",
        encoding="utf-8",
    )

    body = _client(monkeypatch).get("/ui/recent-edited").text

    assert "2021-03-07T17:42:00" not in body
    assert "7. März 2021" in body
    assert "<time" in body


# ── Sidebar-Zustand ─────────────────────────────────────────────────────────

def test_dateibaum_startet_offen_und_folgt_dem_cookie(monkeypatch):
    client = _client(monkeypatch)

    default_page = client.get("/").text
    assert 'class="sidebar"' in default_page
    assert 'aria-hidden="false"' in default_page

    client.cookies.set("kiwiki_sidebar", "closed")
    closed_page = client.get("/").text
    assert 'class="sidebar collapsed"' in closed_page
    assert "inert" in closed_page.split('<aside class="sidebar', 1)[1].split(">", 1)[0]


# ── Suchtreffer-Snippet ─────────────────────────────────────────────────────

def test_snippet_zeigt_die_trefferstelle_ohne_markdown_rauschen():
    content = (
        "---\ntitle: Notiz\ntags: [a]\n---\n\n"
        "# Ueberschrift\n\n"
        + "Vorgeplauder. " * 30
        + "\n\n- **Talos** ersetzt kubeadm\n"
    )

    snippet = _clean_snippet(content, "Talos")

    assert "Talos" in snippet
    assert "---" not in snippet
    assert "**" not in snippet
    assert "# " not in snippet
    assert "title:" not in snippet


def test_snippet_bleibt_bei_leerer_query_am_dateianfang():
    snippet = _clean_snippet("# Titel\n\nErster Satz der Notiz.", "")

    assert snippet.startswith("Titel")
    assert not snippet.startswith("…")


# ── Erststart ───────────────────────────────────────────────────────────────

def test_onboarding_steht_offen_solange_der_workspace_leer_ist(monkeypatch):
    """Ein frischer Workspace zeigt sonst nur zwei leere Panels.

    Das eingeklappte <details> spart einem eingearbeiteten Nutzer Platz, nimmt
    einem neuen aber genau die Hilfe weg, die er gerade braucht.
    """
    from app.tenancy import ensure_user_workspace

    monkeypatch.setenv("KIWIKI_USERS", "admin:adminkey:admin")
    workspace = ensure_user_workspace("admin")
    for note in workspace.rglob("*.md"):
        if note.name not in ("index.md", "AGENTS.md"):
            note.unlink()
    client = _client(monkeypatch)

    assert '<details class="home-help" open>' in client.get("/").text

    # Ueber die API, nicht per write_text: nur dieser Weg leert den
    # Verzeichnis-Cache in storage.py, und genau den geht die Anwendung auch.
    created = client.put("/api/file", json={"path": "notes/erste.md", "content": "# Erste"})
    assert created.status_code == 200, created.text

    assert '<details class="home-help">' in client.get("/").text


def test_die_beiden_dashboard_panels_haben_eigene_leertexte(monkeypatch):
    from app.tenancy import ensure_user_workspace

    monkeypatch.setenv("KIWIKI_USERS", "admin:adminkey:admin")
    workspace = ensure_user_workspace("admin")
    for note in workspace.rglob("*.md"):
        if note.name not in ("index.md", "AGENTS.md"):
            note.unlink()
    client = _client(monkeypatch)

    edited = client.get("/ui/recent-edited").text
    created = client.get("/ui/recent-created").text

    assert "Noch nichts bearbeitet." in edited
    assert "Noch keine Notiz angelegt." in created
    assert edited != created


def test_die_hero_groessen_stehen_dort_wo_sie_auch_gewinnen():
    """index.html laedt sein <style> nach kiwiki-polish.css.

    Hero-Regeln im Polish-Stylesheet waeren bei gleicher Spezifitaet
    wirkungslos — genau das ist einmal passiert und liess den Titel bei
    3.8rem stehen.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    index = (root / "app/templates/index.html").read_text(encoding="utf-8")
    polish = (root / "app/static/kiwiki-polish.css").read_text(encoding="utf-8")

    assert "font-size: var(--text-display);" in index
    assert "grid-template-columns: minmax(0, 1fr);" in index
    assert "font-size" not in polish.split(".hero-title", 1)[1].split("}", 1)[0]
