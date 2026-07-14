"""Regressionstests fuer Frontend-Sicherheit, Navigation und Accessibility."""

from pathlib import Path

from app.main import templates


ROOT = Path(__file__).resolve().parent.parent


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_breadcrumb_pfade_werden_nicht_in_inline_javascript_eingebettet():
    """Ein Dateipfad darf keinen JavaScript-Attributkontext kontrollieren."""
    body = templates.get_template("partials/file_view.html").render(
        title="XSS-Test",
        path="notes/x');alert(1);//.md",
        updated=None,
        owner=None,
        tags=[],
        rendered="<p>sicher</p>",
        user=None,
        can_delete=False,
        svg_edit="",
        svg_trash="",
    )

    assert "onclick=\"toggleFolderByName" not in body
    assert 'data-folder-path="notes"' in body


def test_layout_erlaubt_zoom_und_laesst_editor_assets_aus_normalen_seiten():
    layout = _read("app/templates/layout.html")
    editor = _read("app/templates/editor.html")
    login = _read("app/templates/login.html")

    assert "user-scalable=no" not in layout
    assert "maximum-scale=1.0" not in layout
    assert "toastui-editor.min.css" not in layout
    assert "toastui-editor-all.min.js" not in layout
    assert "/static/vendor/toastui-editor-3.2.2.min.css" in editor
    assert "/static/vendor/toastui-editor-3.2.2.min.js" in editor
    assert "defer" in editor
    assert "/static/vendor/htmx-1.9.12.min.js" in layout
    assert "/static/vendor/toastui-editor-3.2.2.min.js" in editor
    for external_host in ("unpkg.com", "uicdn.toast.com", "fonts.googleapis.com", "fonts.gstatic.com"):
        assert external_host not in layout
        assert external_host not in editor
        assert external_host not in login
    assert "cdn.jsdelivr.net/npm/geist" not in layout
    assert "cdn.jsdelivr.net/npm/geist" not in login
    assert 'rel="icon"' in layout
    assert 'rel="icon"' in login


def test_geschlossene_sidebar_ist_initial_fuer_a11y_versteckt():
    for template_name in ("app/templates/index.html", "app/templates/editor.html"):
        template = _read(template_name)
        assert '<aside class="sidebar collapsed" aria-label="Dateien" aria-hidden="true" inert>' in template

    script = _read("app/static/kiwiki.js")
    assert "function kwSetSidebarAccessibility" in script
    assert "s.inert = isClosed" in script


def test_hamburger_hat_sichtbaren_tastaturfokus():
    css = _read("app/static/kiwiki.css")
    focus_rule = css.split(".hamburger:focus-visible", 1)[1].split("}", 1)[0]

    assert "outline: none" not in focus_rule
    assert "outline:" in focus_rule


def test_mobile_settings_grid_erzeugt_keine_implizite_zweite_spalte():
    settings = _read("app/templates/settings.html")

    assert ".settings-form > *" in settings
    assert ".settings-form > .btn-primary" in settings
    assert "grid-column: 1 / -1" in settings


def test_tags_und_suchverlauf_zielen_auf_main_content_mit_get_fallback():
    tags = _read("app/templates/partials/tags_overview.html")
    history = _read("app/templates/partials/search_history.html")

    assert "#content" not in tags
    assert "#content" not in history
    assert 'href="/?search=' in tags
    assert 'href="/?file=' in tags
    assert 'href="/?search=' in history
    assert 'data-search-query=' in tags
    assert 'data-search-query=' in history


def test_notizen_verwenden_stabile_deep_links_und_history_navigation():
    script = _read("app/static/kiwiki.js")
    linked_templates = (
        "app/templates/partials/file_tree.html",
        "app/templates/partials/search_results.html",
        "app/templates/partials/recent_created.html",
        "app/templates/partials/recent_edited.html",
        "app/templates/partials/recent_files.html",
    )

    assert "window.history.pushState" in script
    assert "window.addEventListener('popstate'" in script
    assert "document.title =" in script
    assert "replaceState(null, '', '/')" not in script
    for template_name in linked_templates:
        template = _read(template_name)
        assert 'href="/?file=' in template
        assert "onclick=\"loadFile(" not in template


def test_dateibaum_nutzt_native_navigation_statt_unvollstaendigem_aria_tree():
    index = _read("app/templates/index.html")
    tree = _read("app/templates/partials/file_tree.html")

    assert '<nav class="file-tree" id="file-tree"' in index
    assert 'role="tree"' not in index
    assert 'role="treeitem"' not in tree
    assert 'role="group"' not in tree


def test_editor_controls_erhalten_zugaengliche_namen():
    editor = _read("app/templates/editor.html")

    assert "function labelEditorControls" in editor
    assert "setAttribute('aria-label'" in editor
