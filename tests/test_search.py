"""Tests fuer app/search.py: FTS5-Initialisierung, Indexierung, Suche."""

import threading

from app.search import (
    _db_file,
    _sanitize_fts,
    _get_pooled_conn,
    close_pool,
    deindex_file,
    get_db,
    index_file,
    init_db,
    search,
    reindex_all,
    reindex_changed,
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
        with get_db() as conn:
            rows = conn.execute("SELECT path, title, content FROM files WHERE path = ?", (rel,)).fetchall()
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
        with get_db() as conn:
            row = conn.execute("SELECT tags, updated_at, owner FROM files WHERE path = ?", (rel,)).fetchone()
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
        with get_db() as conn:
            rows = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
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

    def test_leere_suche_gibt_leere_liste(self, active_user):
        assert search("   ") == []

    def test_tag_suche_trifft_nur_exakten_tag(self, tmp_file, active_user):
        tmp_file("notes/python.md", "---\ntitle: Python\ntags: [python]\n---\n\nA")
        tmp_file("notes/pythonista.md", "---\ntitle: Pythonista\ntags: [pythonista]\n---\n\nB")
        init_db()
        index_file("notes/python.md")
        index_file("notes/pythonista.md")

        assert [result.path for result in search("tag:python")] == ["notes/python.md"]


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

    def test_reindex_changed_entfernt_geloeschte_dateien(self, tmp_file, active_user):
        rel = tmp_file("notes/ghost.md", "---\ntitle: Ghost\n---\n\nEinzigartigerGeist")
        init_db()
        reindex_changed()
        from app.storage import safe_path

        safe_path(rel).unlink()
        reindex_changed()

        assert search("EinzigartigerGeist") == []


def test_connection_pool_verwendet_pro_thread_eigene_connection(tmp_path, active_user):
    db_path = str(_db_file())
    barrier = threading.Barrier(2)
    connections = []

    def get_connection():
        barrier.wait()
        connections.append(_get_pooled_conn(db_path))

    threads = [threading.Thread(target=get_connection) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    try:
        assert len(connections) == 2
        assert connections[0] is not connections[1]
    finally:
        close_pool()
