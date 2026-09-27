"""Regressionstests fuer die SQLite-Timeouts.

Die Knowledge-DB hatte ein Busy-Timeout von 1,0 s, das unter Last nicht
reichte: der Knowledge-Test fiel dadurch in CI mit `sqlite3.OperationalError:
database is locked` um, ohne dass ein Codefehler vorlag. Der Wartende
bekam schlicht keine Gelegenheit, den Lock zu bekommen.

Der FTS-Index bleibt dagegen bewusst bei 250 ms — er hat eine eigene
Retry-Logik (`deindex_files` versucht dreimal mit Backoff 20/40/80 ms),
und ein langes Warten wuerde sie aushebeln.

Ein Timeout ist hier kein Komfortparameter: SQLite in WAL-Modus kann
gleichzeitig schreiben und lesen, aber nur *ein* Schreiber zur Zeit. Wer
den Lock nicht bekommt, muss warten — sonst wird aus normaler
Nebenläufigkeit ein 500er.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from app.knowledge import db as knowledge_db
from app.search import _get_pooled_conn, close_pool


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "alice"
    (root / ".kiwiki").mkdir(parents=True)
    return root


# ── Die Timeouts selbst ────────────────────────────────────────────────────

def test_knowledge_db_timeout_ist_nicht_unter_einer_sekunde(workspace):
    """1,0 s war der Ausloeser. Der Test haelt die 10 s fest, damit der
    Wert nicht wieder herunteroptimiert wird."""
    connection = knowledge_db.open_database(workspace)
    try:
        timeout_ms = connection.execute("PRAGMA busy_timeout").fetchone()[0]
    finally:
        connection.close()

    assert timeout_ms >= 10000, (
        f"busy_timeout ist {timeout_ms} ms — unter CI-Last reichte das nicht, "
        "der Test fiel mit 'database is locked' um"
    )


def test_fts_timeout_bleibt_kurz_weil_der_pfad_retryt(tmp_path):
    """Der FTS-Index hat eine eigene Retry-Logik — kurzes Warten ist Absicht.

    `deindex_files` versucht dreimal mit Backoff 20/40/80 ms
    (`test_sqlite_lock_wird_begrenzt_wiederholt`). Ein langes Warten hier
    wuerde diese Logik aushebeln: der Aufrufer wartete dann Sekunden
    statt dreimal kurz zu probieren, und die Zeit wandert aus dem Aufrufer
    in den Lock. `test_search.py::test_sqlite_busy_timeout_ist_kurz` haelt
    den Wert fest; dieser Test nennt den Grund.
    """
    db_path = tmp_path / "index.sqlite"
    connection = _get_pooled_conn(str(db_path))
    try:
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] <= 250
    finally:
        close_pool()


def test_nur_die_knowledge_db_wartet_lang(tmp_path):
    """Die beiden Timeouts sind verschieden — mit Absicht, nicht aus Versehen.

    Die Knowledge-DB hat keinen Retry-Pfad, dort war 1,0 s zu knapp. Der
    FTS-Index hat einen und bleibt deshalb kurz. Beide Seiten werden zur
    Laufzeit ueber PRAGMA geprüft, nicht per Quelltext-Suche — ein Reformat
    (Leerzeichen, Konstante) darf diesen Test nicht brechen.
    """
    db_path = tmp_path / "index.sqlite"
    connection = _get_pooled_conn(str(db_path))
    try:
        fts_ms = connection.execute("PRAGMA busy_timeout").fetchone()[0]
    finally:
        close_pool()

    assert fts_ms <= 250, (
        f"der FTS-Timeout ist {fts_ms} ms — er muss kurz bleiben, weil "
        "deindex_files selbst mit Backoff retryt"
    )


# ── Der eigentliche Nachweis: paralleles Schreiben ────────────────────────

def _lege_job_an(verbindung: sqlite3.Connection, pfad: str) -> None:
    """Schreibt in knowledge_jobs — die kleinste Tabelle ohne Fremdschluessel."""
    verbindung.execute(
        "INSERT INTO knowledge_jobs(path, operation, updated_at) VALUES (?, 'reconcile', ?)",
        (pfad, time.time()),
    )


def test_paralleles_schreiben_schlaegt_nicht_mit_locked(workspace):
    """Zwei Schreiber auf derselben DB: der zweite muss warten, nicht scheitern.

    Der Test haelt einen exklusiven Lock so lange, dass 1,0 s nicht
    ausreichen. Mit dem alten Timeout schlug der Versuch mit
    `OperationalError` fehl; mit 10 s gelingt er.
    """
    blocker = knowledge_db.open_database(workspace)
    schreiber = knowledge_db.open_database(workspace)

    lock_gehalten = threading.Event()
    loslassen = threading.Event()
    ergebnis: dict = {}

    def _halte_lock():
        blocker.execute("BEGIN IMMEDIATE")
        _lege_job_an(blocker, "blocker.md")
        lock_gehalten.set()
        loslassen.wait(2.0)
        blocker.commit()

    def _schreibe():
        lock_gehalten.wait(2.0)
        try:
            _lege_job_an(schreiber, "wartender.md")
            schreiber.commit()
            ergebnis["ok"] = True
        except sqlite3.OperationalError as exc:
            ergebnis["fehler"] = str(exc)
        finally:
            loslassen.set()

    halter = threading.Thread(target=_halte_lock)
    waiter = threading.Thread(target=_schreibe)
    try:
        halter.start()
        waiter.start()
        waiter.join(timeout=8.0)
        assert not waiter.is_alive(), "der Wartende haengt — busy_timeout greift nicht"
    finally:
        loslassen.set()
        halter.join(timeout=5.0)
        for verbindung in (blocker, schreiber):
            try:
                verbindung.close()
            except sqlite3.Error:
                pass

    assert "fehler" not in ergebnis, (
        f"paralleles Schreiben schlug fehl: {ergebnis.get('fehler')}"
    )
    assert ergebnis.get("ok") is True


def test_das_schreiben_dauert_nicht_zehn_sekunden(workspace):
    """Der Timeout darf kein Grund werden, Anfragen auszubremsen.

    Ohne Konkurrenz schreibt SQLite sofort. Das prüft, dass die 10 s nur
    im Konfliktfall greifen.
    """
    verbindung = knowledge_db.open_database(workspace)
    try:
        start = time.monotonic()
        _lege_job_an(verbindung, "allein.md")
        verbindung.commit()
        dauer = time.monotonic() - start
    finally:
        verbindung.close()

    assert dauer < 1.0, f"ein Schreibvorgang ohne Konkurrenz dauerte {dauer:.2f} s"
