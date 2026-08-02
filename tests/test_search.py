"""Tests fuer app/search.py: FTS5-Initialisierung, Indexierung, Suche."""

import sqlite3
import threading
from contextlib import contextmanager

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

    def test_schema_migration_invalidiert_inkrementellen_reindex(self, active_user, tmp_file):
        import app.search as search_mod

        rel = tmp_file("notes/migration.md", "---\ntitle: Migration\n---\n\nAltbestand")
        db_path = _db_file()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        close_pool()
        connection = sqlite3.connect(db_path)
        connection.execute(
            "CREATE VIRTUAL TABLE files USING fts5("
            "path, title, tags, content, updated_at, owner, tokenize='porter unicode61')"
        )
        connection.commit()
        connection.close()
        timestamp_file = db_path.parent / ".last_reindex"
        timestamp_file.write_text("99999999999", encoding="utf-8")
        search_mod._initialized_dbs.discard(str(db_path))

        init_db()
        count = reindex_changed()

        assert count == 1
        with get_db() as connection:
            indexed = connection.execute("SELECT path FROM files WHERE path = ?", (rel,)).fetchone()
        assert indexed["path"] == rel


class TestIndexFile:
    """index_file() — Einzelne Datei indizieren."""

    def test_gueltige_datei_indizieren(self, tmp_file, active_user, tmp_path):
        rel = tmp_file("notes/test.md")
        init_db()
        index_file(rel)
        with get_db() as conn:
            rows = conn.execute("SELECT path, title, content, revision FROM files WHERE path = ?", (rel,)).fetchall()
        assert len(rows) == 1
        assert rows[0]["title"] == "Test"
        assert "Body" in rows[0]["content"]
        from app.storage import safe_path

        assert int(rows[0]["revision"]) == safe_path(rel).stat().st_mtime_ns

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

    def test_inhalt_und_revision_werden_unter_demselben_pfad_lock_erfasst(
        self,
        monkeypatch,
        active_user,
    ):
        import app.storage as storage_mod
        from app.tenancy import set_user_ns

        rel = "notes/race-index.md"
        storage_mod.write_file(rel, "# GeheimesAltesSuchwort")
        init_db()
        original_read_file = storage_mod.read_file
        writer_finished = threading.Event()
        writer_threads = []
        writer_was_blocked = []

        def write_new_content():
            set_user_ns("alice")
            storage_mod.write_file(rel, "# Harmloser neuer Inhalt")
            writer_finished.set()

        def racing_read_file(path):
            content = original_read_file(path)
            writer = threading.Thread(target=write_new_content)
            writer_threads.append(writer)
            writer.start()
            writer_was_blocked.append(not writer_finished.wait(0.1))
            return content

        monkeypatch.setattr(storage_mod, "read_file", racing_read_file)

        index_file(rel)
        for writer in writer_threads:
            writer.join(timeout=1)

        assert writer_was_blocked == [True]
        assert writer_finished.is_set()
        assert search("GeheimesAltesSuchwort") == []


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

    def test_sqlite_lock_wird_begrenzt_wiederholt(self, monkeypatch):
        import app.search as search_mod

        class FlakyConnection:
            attempts = 0
            commits = 0
            rollbacks = 0

            def executemany(self, _query, _params):
                self.attempts += 1
                if self.attempts < 3:
                    raise sqlite3.OperationalError("database is locked")

            def commit(self):
                self.commits += 1

            def rollback(self):
                self.rollbacks += 1

        connection = FlakyConnection()

        @contextmanager
        def fake_get_db():
            yield connection

        monkeypatch.setattr(search_mod, "get_db", fake_get_db)
        monkeypatch.setattr(search_mod.time, "sleep", lambda _delay: None)

        deindex_file("notes/test.md")

        assert connection.attempts == 3
        assert connection.rollbacks == 2
        assert connection.commits == 1

    def test_mehrere_pfade_werden_in_einer_transaktion_entfernt(self, monkeypatch):
        import app.search as search_mod

        class RecordingConnection:
            calls = []
            commits = 0

            def executemany(self, query, params):
                self.calls.append((query, list(params)))

            def commit(self):
                self.commits += 1

        connection = RecordingConnection()

        @contextmanager
        def fake_get_db():
            yield connection

        monkeypatch.setattr(search_mod, "get_db", fake_get_db)

        search_mod.deindex_files(["notes/a.md", "notes/b.md"])

        assert connection.calls == [
            (
                "DELETE FROM files WHERE path = ?",
                [("notes/a.md",), ("notes/b.md",)],
            )
        ]
        assert connection.commits == 1

    def test_sqlite_busy_timeout_ist_kurz(self, active_user):
        connection = _get_pooled_conn(str(_db_file()))

        busy_timeout_ms = connection.execute("PRAGMA busy_timeout").fetchone()[0]

        assert busy_timeout_ms <= 250


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
