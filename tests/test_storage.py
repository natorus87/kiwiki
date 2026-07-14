"""Tests fuer app/storage.py: safe_path(), CRUD-Operationen, Path-Traversal-Schutz."""

import threading
import time

import pytest

import app.storage as storage_mod

from app.storage import (
    _fm_cache,
    _read_frontmatter_only,
    append_file,
    create_folder,
    delete_folder,
    delete_file,
    create_note,
    edit_file,
    list_all_files,
    list_files,
    move_file,
    read_file,
    safe_path,
    update_frontmatter,
    write_file,
)


class TestSafePath:
    """safe_path() — Pfad-Validierung innerhalb des User-Namespace."""

    def test_leerer_pfad(self, active_user):
        with pytest.raises(ValueError, match="Empty path"):
            safe_path("")

    def test_normaler_pfad(self, tmp_path, active_user):
        p = safe_path("notes/test.md")
        assert p.is_relative_to(tmp_path)

    def test_path_traversal_parent(self, tmp_path, active_user):
        """../ in Pfad darf nicht aus dem Namespace entkommen."""
        with pytest.raises(ValueError, match="Path traversal"):
            safe_path("../secret.md")

    def test_path_traversal_tief_geschachtelt(self, tmp_path, active_user):
        """Mehrere ../ Ebenen."""
        with pytest.raises(ValueError, match="Path traversal"):
            safe_path("../../etc/passwd")

    def test_leading_slash_wird_ignoriert(self, tmp_path, active_user):
        """Fuehrender Slash wird entfernt."""
        p = safe_path("/notes/test.md")
        assert p.is_relative_to(tmp_path)


class TestReadFile:
    """read_file() — Markdown-Datei mit Frontmatter lesen."""

    def test_gueltige_datei(self, tmp_file, active_user):
        rel = tmp_file("notes/test.md")
        fc = read_file(rel)
        assert fc.path == rel
        assert "Body" in fc.content
        assert fc.frontmatter["title"] == "Test"

    def test_nicht_existierende_datei(self, active_user):
        with pytest.raises(FileNotFoundError):
            read_file("notes/nicht_da.md")

    def test_verzeichnis_als_datei(self, tmp_path, active_user):
        (tmp_path / "notes").mkdir(exist_ok=True)
        with pytest.raises(ValueError, match="Not a file"):
            read_file("notes")


class TestWriteFile:
    """write_file() — Markdown-Datei schreiben."""

    def test_neue_datei(self, tmp_path, active_user):
        fc = write_file("notes/neu.md", "---\ntitle: Neu\ntype: note\n---\n\nHallo")
        assert fc.path == "notes/neu.md"
        assert (tmp_path / "notes/neu.md").exists()
        assert "updated" in fc.frontmatter

    def test_verzeichnis_wird_automatisch_erstellt(self, tmp_path, active_user):
        write_file("notes/python/asyncio.md", "---\ntitle: AsyncIO\n---\n\nCode")
        assert (tmp_path / "notes/python/asyncio.md").exists()

    def test_schreiben_in_systempfad_wird_zentral_abgelehnt(self, active_user):
        with pytest.raises(ValueError, match=".kiwiki"):
            write_file("notes/../.kiwiki/users.md", "secret")

    def test_datei_wird_atomar_ersetzt(self, monkeypatch, active_user):
        calls = []
        real_replace = storage_mod.os.replace

        def tracked_replace(src, dst):
            calls.append((src, dst))
            return real_replace(src, dst)

        monkeypatch.setattr(storage_mod.os, "replace", tracked_replace)
        write_file("notes/atomic.md", "Body")
        assert len(calls) == 1

    def test_veraltete_revision_verhindert_stilles_ueberschreiben(self, tmp_path, active_user):
        write_file("notes/conflict.md", "Version eins")
        revision = (tmp_path / "notes/conflict.md").stat().st_mtime_ns
        write_file("notes/conflict.md", "Version zwei")

        with pytest.raises(ValueError, match="conflict"):
            write_file("notes/conflict.md", "Veralteter Agent", expected_revision=revision)

    def test_parallele_appends_verlieren_keine_aenderung(self, monkeypatch, active_user):
        write_file("notes/concurrent.md", "Start")
        real_load = storage_mod.frontmatter.load

        def slow_load(*args, **kwargs):
            post = real_load(*args, **kwargs)
            time.sleep(0.02)
            return post

        monkeypatch.setattr(storage_mod.frontmatter, "load", slow_load)
        start = threading.Barrier(8)
        errors = []

        def append(index):
            from app.tenancy import set_user_ns

            try:
                set_user_ns("alice")
                start.wait()
                edit_file("notes/concurrent.md", f"Agent-{index}")
            except Exception as exc:  # pragma: no cover - assertion below reports it
                errors.append(exc)

        threads = [threading.Thread(target=append, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        assert not errors
        content = read_file("notes/concurrent.md").content
        assert all(f"Agent-{index}" in content for index in range(8))

    def test_tenant_dateianzahl_ist_begrenzt(self, monkeypatch, active_user):
        # Zwei Seed-Dateien (AGENTS.md/index.md) gehoeren bereits zum Workspace.
        monkeypatch.setattr(storage_mod, "_MAX_TENANT_FILES", 3, raising=False)
        write_file("notes/first.md", "Erste Datei")

        with pytest.raises(ValueError, match="file quota"):
            write_file("notes/second.md", "Zweite Datei")

    def test_tenant_speicherplatz_ist_begrenzt(self, monkeypatch, active_user):
        monkeypatch.setattr(storage_mod, "_MAX_TENANT_BYTES", 128, raising=False)

        with pytest.raises(ValueError, match="storage quota"):
            write_file("notes/large.md", "x" * 1024)

    def test_tenant_quota_gilt_auch_bei_parallelen_writes(self, monkeypatch, active_user):
        monkeypatch.setattr(storage_mod, "_MAX_TENANT_FILES", 3)
        real_replace = storage_mod.os.replace

        def slow_replace(*args, **kwargs):
            time.sleep(0.03)
            return real_replace(*args, **kwargs)

        monkeypatch.setattr(storage_mod.os, "replace", slow_replace)
        start = threading.Barrier(2)
        results = []

        def write(name):
            from app.tenancy import set_user_ns

            set_user_ns("alice")
            start.wait()
            try:
                write_file(f"notes/{name}.md", name)
                results.append("ok")
            except ValueError as exc:
                results.append(str(exc))

        threads = [threading.Thread(target=write, args=(name,)) for name in ("one", "two")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        assert results.count("ok") == 1
        assert sum("file quota" in result for result in results) == 1


class TestAppendFile:
    """append_file() — Inhalt an bestehende Datei anhengen."""

    def test_anhaengen(self, tmp_file, active_user):
        rel = tmp_file("notes/test.md")
        append_file(rel, "Zusatztext")
        fc = read_file(rel)
        assert "Zusatztext" in fc.content

    def test_nicht_existierende_datei(self, active_user):
        with pytest.raises(FileNotFoundError):
            append_file("notes/da.md", "text")


class TestDeleteFile:
    """delete_file() — Markdown-Datei loeschen."""

    def test_loeschen(self, tmp_file, active_user, tmp_path):
        rel = tmp_file("notes/test.md")
        delete_file(rel)
        assert not (tmp_path / rel).exists()

    def test_nur_md_dateien(self, tmp_path, active_user):
        (tmp_path / "notes/readme.txt").write_text("text", encoding="utf-8")
        with pytest.raises(ValueError, match="Only .md files"):
            delete_file("notes/readme.txt")

    def test_nicht_existierend(self, active_user):
        with pytest.raises(FileNotFoundError):
            delete_file("notes/da.md")


class TestCreateNote:
    """create_note() — Neue Notiz mit Frontmatter anlegen."""

    def test_notiz_erstellen(self, active_user, tmp_path):
        path = create_note("Test Notiz", "Inhalt", ["python"], "testuser", "notes")
        assert path == "notes/test-notiz.md"
        file_path = tmp_path / path
        assert file_path.exists()
        import frontmatter

        post = frontmatter.load(open(file_path, encoding="utf-8"))
        assert post.metadata["title"] == "Test Notiz"
        assert post.metadata["tags"] == ["python"]
        assert post.metadata["owner"] == "testuser"

    def test_kollision_vermeidung(self, active_user, tmp_path):
        create_note("Test Notiz", "Erste", [], "testuser")
        path2 = create_note("Test Notiz", "Zweite", [], "testuser")
        assert path2 == "notes/test-notiz-2.md"

    def test_leerer_slug_wird_abgelehnt(self, active_user):
        with pytest.raises(ValueError, match="slug"):
            create_note("!!!", "Inhalt", [], "testuser")

    def test_systemordner_wird_abgelehnt(self, active_user):
        with pytest.raises(ValueError, match=".kiwiki"):
            create_note("Geheim", "Inhalt", [], "testuser", ".kiwiki")

    def test_parallele_notizen_erhalten_eindeutige_pfade(self, monkeypatch, active_user):
        real_atomic_write = storage_mod._atomic_write_text

        def slow_atomic_write(*args, **kwargs):
            time.sleep(0.02)
            return real_atomic_write(*args, **kwargs)

        monkeypatch.setattr(storage_mod, "_atomic_write_text", slow_atomic_write)
        start = threading.Barrier(6)
        paths = []

        def create(index):
            from app.tenancy import set_user_ns

            set_user_ns("alice")
            start.wait()
            paths.append(create_note("Parallel", f"Agent {index}", [], "alice"))

        threads = [threading.Thread(target=create, args=(index,)) for index in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        assert len(paths) == 6
        assert len(set(paths)) == 6


class TestEditFile:
    """edit_file() — Datei-Inhalt bearbeiten (Find & Replace / Append)."""

    def test_find_replace(self, tmp_file, active_user):
        rel = tmp_file("notes/test.md", "---\ntitle: Test\n---\n\nAlter Text hier.")
        edit_file(rel, "Neuer Text", "Alter Text")
        fc = read_file(rel)
        assert "Neuer Text" in fc.content
        assert "Alter Text" not in fc.content

    def test_find_replace_nicht_gefunden(self, tmp_file, active_user):
        rel = tmp_file("notes/test.md")
        with pytest.raises(ValueError, match="String not found"):
            edit_file(rel, "neu", "nicht_da")

    def test_anhaengen_ohne_old_str(self, tmp_file, active_user):
        rel = tmp_file("notes/test.md")
        edit_file(rel, "Anghang")
        fc = read_file(rel)
        assert "Anghang" in fc.content


class TestUpdateFrontmatter:
    """update_frontmatter() — Nur Frontmatter aendern."""

    def test_tags_erweitern(self, tmp_file, active_user):
        rel = tmp_file("notes/test.md")
        update_frontmatter(rel, {"tags": ["python", "async"]})
        fc = read_file(rel)
        assert fc.frontmatter["tags"] == ["python", "async"]

    def test_nicht_existierend(self, active_user):
        with pytest.raises(FileNotFoundError):
            update_frontmatter("notes/da.md", {"tags": []})


class TestReadFrontmatterOnlyCache:
    """_read_frontmatter_only() — mtime-basierter Cache (A3)."""

    def test_zweiter_read_nutzt_cache(self, tmp_file, active_user):
        rel = tmp_file("notes/test.md")
        cache_len_vorher = len(_fm_cache)
        first = _read_frontmatter_only(rel)
        cache_len_nachher = len(_fm_cache)
        assert cache_len_nachher == cache_len_vorher + 1
        second = _read_frontmatter_only(rel)
        assert second == first
        assert len(_fm_cache) == cache_len_nachher

    def test_write_invalidiert_cache(self, tmp_file, active_user):
        rel = tmp_file("notes/test.md")
        _read_frontmatter_only(rel)
        assert len(_fm_cache) > 0
        write_file(rel, "---\ntitle: Neu\n---\n\nGeaendert")
        assert len(_fm_cache) == 0
        assert _read_frontmatter_only(rel)["title"] == "Neu"


class TestMoveFile:
    """move_file() — Datei verschieben/umbenennen."""

    def test_umbenennen(self, tmp_file, active_user, tmp_path):
        rel = tmp_file("notes/alt.md")
        fc = move_file(rel, "notes/neu.md")
        assert not (tmp_path / rel).exists()
        assert (tmp_path / "notes/neu.md").exists()
        assert fc.path == "notes/neu.md"

    def test_in_unterordner(self, tmp_file, active_user, tmp_path):
        rel = tmp_file("notes/test.md")
        move_file(rel, "notes/python/test.md")
        assert (tmp_path / "notes/python/test.md").exists()

    def test_nur_md_dateien(self, tmp_path, active_user):
        (tmp_path / "notes/readme.txt").write_text("x", encoding="utf-8")
        with pytest.raises(ValueError, match="Only .md files"):
            move_file("notes/readme.txt", "notes/readme2.txt")


class TestListFiles:
    """list_files() — Verzeichnisinhalt auflisten."""

    def test_leeres_verzeichnis(self, active_user):
        result = list_files("notes")
        assert isinstance(result, list)

    def test_dateien_und_ordner(self, tmp_file, active_user, tmp_path):
        tmp_file("notes/test.md")
        (tmp_path / "notes/projects").mkdir(exist_ok=True)
        result = list_files("notes")
        names = [r.name for r in result]
        assert "projects" in names

    def test_liste_ist_begrenzt(self, monkeypatch, tmp_file, active_user):
        monkeypatch.setattr(storage_mod, "_MAX_LIST_ITEMS", 2, raising=False)
        tmp_file("notes/a.md")
        tmp_file("notes/b.md")
        tmp_file("notes/c.md")

        assert len(list_files("notes")) == 2


class TestListAllFiles:
    """list_all_files() — Rekursiv alle Markdown-Dateien."""

    def test_rekursiv(self, tmp_file, active_user, tmp_path):
        tmp_file("notes/test.md")
        tmp_file("notes/python/asyncio.md")
        tmp_file("projects/wiki.md")
        result = list_all_files(".")
        paths = [r["path"] for r in result]
        assert "notes/test.md" in paths
        assert "notes/python/asyncio.md" in paths
        assert "projects/wiki.md" in paths


class TestFolderOps:
    """create_folder() und delete_folder()."""

    def test_folder_erstellen(self, tmp_path, active_user):
        create_folder("notes/python")
        assert (tmp_path / "notes/python").is_dir()

    def test_systemfolder_erstellen_wird_zentral_abgelehnt(self, active_user):
        with pytest.raises(ValueError, match=".kiwiki"):
            create_folder(".kiwiki/import")

    def test_folder_loeschen(self, tmp_file, active_user, tmp_path):
        (tmp_path / "notes/python").mkdir(parents=True, exist_ok=True)
        (tmp_path / "notes/python/file.md").write_text("---\nt: f\n---\nx", encoding="utf-8")
        delete_folder("notes/python")
        assert not (tmp_path / "notes/python").exists()

    def test_data_root_nicht_loeschbar(self, active_user):
        with pytest.raises(ValueError, match="Cannot delete the data root"):
            delete_folder("")
