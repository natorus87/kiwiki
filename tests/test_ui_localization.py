"""Vertragstests fuer die globale deutsch-englische Produktoberflaeche."""

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app


def _login(client: TestClient, key: str = "ui-language-key") -> None:
    response = client.post("/login", data={"api_key": key}, follow_redirects=False)
    assert response.status_code == 303


def test_english_language_choice_applies_to_all_core_pages_and_fragments(monkeypatch):
    monkeypatch.setenv("KIWIKI_USERS", "alice:ui-language-key:admin")

    with TestClient(app) as client:
        login = client.get("/login?lang=en")
        assert login.status_code == 200
        assert '<html lang="en">' in login.text
        assert "Sign in" in login.text
        assert "kiwiki_language=en" in login.headers["set-cookie"]

        _login(client)

        home = client.get("/")
        editor = client.get("/editor")
        settings = client.get("/settings")
        recent = client.get("/ui/recent-edited")
        missing_history = client.get("/ui/history")
        missing_file = client.get("/ui/file?path=missing.md")

    assert '<html lang="en">' in home.text
    assert "New note" in home.text
    assert "Recently edited" in home.text
    assert 'window.KIWIKI_I18N' in home.text
    assert '"deleteFile": "Delete file"' in home.text
    assert '<html lang="en">' in editor.text
    assert "Save" in editor.text
    assert '<html lang="en">' in settings.text
    assert "Add local user" in settings.text
    assert "No files yet." in recent.text
    assert "No file path provided" in missing_history.text
    assert "File not found" in missing_file.text


def test_accept_language_is_used_until_an_explicit_choice_is_persisted(monkeypatch):
    monkeypatch.setenv("KIWIKI_USERS", "alice:ui-language-key:admin")

    with TestClient(app) as client:
        inferred = client.get("/login", headers={"Accept-Language": "en-US,en;q=0.9"})
        selected = client.get("/login?lang=de", headers={"Accept-Language": "en-US,en;q=0.9"})
        persisted = client.get("/login", headers={"Accept-Language": "en-US,en;q=0.9"})

    assert '<html lang="en">' in inferred.text
    assert "Sign in" in inferred.text
    assert '<html lang="de">' in selected.text
    assert "Anmelden" in selected.text
    assert "kiwiki_language=de" in selected.headers["set-cookie"]
    assert '<html lang="de">' in persisted.text


def test_login_errors_are_localized_in_both_languages(monkeypatch):
    monkeypatch.setenv("KIWIKI_USERS", "alice:ui-language-key:admin")

    with TestClient(app) as client:
        english = client.post("/login?lang=en", data={"api_key": "wrong"})
        german = client.post("/login?lang=de", data={"api_key": "wrong"})

    assert "Invalid API key" in english.text
    assert "Ungültiger API-Key" in german.text


def test_german_and_english_translation_catalogs_have_identical_keys():
    from app.i18n import UI_TRANSLATIONS

    assert set(UI_TRANSLATIONS) == {"de", "en"}
    assert set(UI_TRANSLATIONS["de"]) == set(UI_TRANSLATIONS["en"])
    assert set(UI_TRANSLATIONS["de"]["js"]) == set(UI_TRANSLATIONS["en"]["js"])


def test_browser_copy_uses_the_shared_translation_catalog():
    script = Path("app/static/kiwiki.js").read_text(encoding="utf-8")

    assert "function kwText(" in script
    assert "label: 'Löschen'" not in script
    assert "kwToast('Unerwarteter Fehler" not in script
    assert "aria-label=\"Benachrichtigungen\"" not in script
