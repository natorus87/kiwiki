"""Tests fuer app/search.py: FTS5-Initialisierung, Indexierung, Suche."""

from app.search import (
    _db_file,
    _sanitize_fts,
    deindex_file,
    get_db,
    index_file,
    init_db,
    search,
    reindex_all,
)


class TestDbInit:
    """init_db() — FTS5-Tabelle erstellen."""

    def test_tabelle_wird_erstellt(self, tmp_path, active_user):
        init_db()
        db = tmp_path / ".kiwiki" / "index.sqlite"
        assert db.exists()

    def test_idempotent(self, tmp_path, active_user):
        init_db()
        db1 = _db_file()
        init_db()
        db2 = _db_file()
        assert db1 == db2  # Gleiche Datei, kein Fehler.


class TestIndexFile:
    """index_file() — Einzelne Datei indizieren."""

    def test_gueltige_datei_indizieren(self, tmp_file, active_user, tmp_path):
        rel = tmp_file("notes/test.md")
        init_db()
        index_file(rel)
        conn = get_db()
        rows = conn.execute("SELECT path, title, content FROM files WHERE path = ?", (rel,)).fetchall()
        conn.close()
        assert len(rows) == 1
        assert rows[0]["title"] == "Test"
        assert "Body" in rows[0]["content"]

    def test_nicht_existierende_datei(self, active_user):
        init_db()
        index_file("notes/da.md")  # Sollte keinen Fehler werfen.

    def test_frontmatter_fields(self, tmp_file, active_user, tmp_path):
        content = "---\ntitle: Suchtest\ntype: note\ntags: [python, test]\nupdated: 2026-06-01\nowner: testuser\n---\n\nKörper"
        rel = tmp_file("notes/suchtest.md", content)
        init_db()
        index_file(rel)
        conn = get_db()
        row = conn.execute("SELECT tags, updated_at, owner FROM files WHERE path = ?", (rel,)).fetchone()
        conn.close()
        assert row["tags"] == "python,test"
        assert row["updated_at"] == "2026-06-01"
        assert row["owner"] == "testuser"


class TestDeindexFile:
    """deindex_file() — Datei aus Index entfernen."""

    def test_entfernen(self, tmp_file, active_user, tmp_path):
        rel = tmp_file("notes/test.md")
        init_db()
        index_file(rel)
        deindex_file(rel)
        conn = get_db()
        rows = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        conn.close()
        assert rows == 0


class TestSearch:
    """search() — Volltextsuche mit FTS5."""

    def test_einfache_suche(self, tmp_file, active_user):
        tmp_file("notes/python.md", "---\ntitle: Python\n---\n\nPython ist eine Sprache.")
        tmp_file("notes/ruby.md", "---\ntitle: Ruby\n---\n\nRuby ist auch eine Sprache.")
        init_db()
        index_file("notes/python.md")
        index_file("notes/ruby.md")
        results = search("Python")
        assert len(results) >= 1
        paths = [r.path for r in results]
        assert "notes/python.md" in paths

    def test_kein_ergebnis(self, active_user):
        init_db()
        results = search("nichtvorhandenxyz")
        assert results == []

    def test_suche_ohne_index(self, active_user):
        """Suche auf leeren Index gibt leere Liste."""
        results = search("irgendwas")
        assert results == []


class TestSanitizeFts:
    """_sanitize_fts() — Query-Normalisierung."""

    def test_column_prefix_wird_entfernt(self):
        assert _sanitize_fts("filename:test") == "test"

    def test_special_chars_entfernt(self):
        result = _sanitize_fts("hello.world:test-value")
        assert "." not in result.split()
        assert "-" not in result.split()

    def test_leere_query(self):
        assert _sanitize_fts("") == ""

    def test_normal_query(self):
        assert _sanitize_fts("hallo welt") == "hallo welt"


class TestReindexAll:
    """reindex_all() — Alle Dateien neu indizieren."""

    def test_zaelen(self, tmp_file, active_user):
        tmp_file("notes/a.md")
        tmp_file("notes/b.md")
        tmp_file("notes/python/c.md")
        init_db()
        count = reindex_all()
        assert count == 3

    def test_leerer_wiki(self, active_user):
        init_db()
        count = reindex_all()
        assert count == 0
