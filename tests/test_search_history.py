"""Regressionstests fuer die Such-Historie.

Die Historie ist das einzige UI-Feature, das seinen Fehlerzustand nicht zeigt:
`record_search` und die Historie-Schreibpfade in `search()` verschlucken jeden
Fehler stillschweigend. Ein Nutzer sieht dann eine leere Historie und hat keinen
Anhaltspunkt, ob die Suche kaputt ist oder nur nichts gefunden wurde.
"""

from __future__ import annotations

import contextlib
import sqlite3

from app import search as search_mod


class _FailingExecute:
    """Proxy, der nur SQL mit `needle` durch `sqlite3.OperationalError` ersetzt."""

    def __init__(self, conn: sqlite3.Connection, needle: str) -> None:
        self._conn = conn
        self._needle = needle

    def execute(self, sql, *args, **kwargs):
        if self._needle in sql:
            raise sqlite3.OperationalError("database is locked")
        return self._conn.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._conn, name)


@contextlib.contextmanager
def _history_writes_failing(monkeypatch, needle: str):
    """Laesst jeden execute()-Aufruf scheitern, dessen SQL `needle` enthaelt."""
    real_get_db = search_mod.get_db

    def _patched_get_db():
        with real_get_db() as conn:
            yield _FailingExecute(conn, needle)

    monkeypatch.setattr(search_mod, "get_db", contextlib.contextmanager(_patched_get_db))
    try:
        yield
    finally:
        monkeypatch.setattr(search_mod, "get_db", real_get_db)


def test_record_search_persists_the_query(active_user):
    search_mod.record_search("  release notes  ", 7)

    history = search_mod.get_search_history(10)
    assert [entry["query"] for entry in history] == ["release notes"]
    assert history[0]["result_count"] == 7


def test_record_search_logs_instead_of_swallowing_a_write_error(active_user, caplog, monkeypatch):
    """Ein DB-Fehler darf nicht still verschwinden."""
    with _history_writes_failing(monkeypatch, "INSERT INTO search_history"):
        with caplog.at_level("ERROR"):
            search_mod.record_search("release notes", 1)

    assert any("release notes" in record.getMessage() for record in caplog.records), (
        "record_search muss den fehlgeschlagenen Query loggen, nicht schlucken"
    )


def test_search_logs_when_pruning_the_history_fails(active_user, caplog, monkeypatch):
    """Der Prune-Schritt am Ende von search() darf keinen Fehler verschlucken."""
    search_mod.record_search("bekannt", 1)
    with _history_writes_failing(monkeypatch, "DELETE FROM search_history"):
        with caplog.at_level("ERROR"):
            results = search_mod.search("bekannt")

    assert isinstance(results, list), "die Suche selbst muss trotz Prune-Fehler noch antworten"
    messages = [record.getMessage() for record in caplog.records]
    assert any("cannot record query" in message for message in messages), (
        f"der fehlgeschlagene Historie-Schreibvorgang muss geloggt werden, war: {messages}"
    )
