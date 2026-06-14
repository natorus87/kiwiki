"""Tests fuer app/storage.py: safe_path(), CRUD-Operationen, Path-Traversal-Schutz."""

import pytest

from app.storage import (
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

    def test_folder_loeschen(self, tmp_file, active_user, tmp_path):
        (tmp_path / "notes/python").mkdir(parents=True, exist_ok=True)
        (tmp_path / "notes/python/file.md").write_text("---\nt: f\n---\nx", encoding="utf-8")
        delete_folder("notes/python")
        assert not (tmp_path / "notes/python").exists()

    def test_data_root_nicht_loeschbar(self, active_user):
        with pytest.raises(ValueError, match="Cannot delete the data root"):
            delete_folder("")
