"""Tests fuer das gemeinsame Fehlversuchs-Budget (Issue #24).

Drei Luecken, eine Ursache — die Drosselung zaehlte pro Formular, pro IP und
pro Prozess:

1. `/login` und `/oauth/authorize` pruefen denselben API-Key, hatten aber
   getrennte Budgets. Nach fuenf Versuchen im Login-Tier waren ueber OAuth
   noch zwanzig weitere moeglich.
2. Alle Limits hingen an der Client-IP. Verteilt ueber viele Quellen war das
   Limit beliebig oft neu fuellbar.
3. Die Zaehler lagen im Prozessspeicher, also zaehlte eine zweite Replika fuer
   sich und die Drosselung wurde schwaecher statt staerker.
"""

from __future__ import annotations

import time

import pytest

from app import rate_limit_store as store_mod
from app.rate_limit_store import (
    MemoryAttemptStore,
    SqliteAttemptStore,
    assert_shared_store_for_replicas,
    register_failed_key_attempt,
    reset_failed_key_attempts,
)


@pytest.fixture(autouse=True)
def memory_store(monkeypatch):
    """Jeder Test bekommt ein frisches, isoliertes Budget."""
    store = MemoryAttemptStore(window=60)
    store_mod.set_store(store)
    monkeypatch.setattr(store_mod, "_KEY_ATTEMPT_LIMIT", 5)
    monkeypatch.setattr(store_mod, "_KEY_ATTEMPT_GLOBAL_LIMIT", 30)
    yield store
    store_mod.set_store(None)


# ── Luecke 1: ein Budget fuer beide Formulare ──────────────────────────────

def test_quelle_wird_nach_fuenf_fehlversuchen_gesperrt(memory_store):
    blocked = [register_failed_key_attempt("10.0.0.1") for _ in range(7)]

    assert blocked[:5] == [False] * 5
    assert all(blocked[5:])


def test_erfolg_setzt_das_budget_der_quelle_zurueck(memory_store):
    for _ in range(4):
        register_failed_key_attempt("10.0.0.2")

    reset_failed_key_attempts("10.0.0.2")

    assert register_failed_key_attempt("10.0.0.2") is False


# ── Luecke 2: globale Obergrenze ueber alle Quellen ───────────────────────

def test_globales_budget_sperrt_bei_vielen_quellen(memory_store):
    """Jede Quelle hat ihr eigenes Budget, das globale ist gemeinsam.

    Ohne die Obergrenze waeren 6 x 5 = 30 Fehlversuche moeglich, weil jede
    Quelle nur ihr eigenes Budget verbraucht. Mit der Obergrenze ist nach dem
    globalen Budget Schluss, obwohl keine Quelle ihr eigenes Limit erreicht.

    Wichtig: Das Quellenlimit (5) sperrt erst ab dem 6. Versuch derselben
    Quelle. Um das globale Budget ohne Quellenlimit zu erreichen, braucht es
    mehr als 6 Quellen — sonst faellt man vorher in das Quellenlimit.
    """
    blocked = [register_failed_key_attempt(f"10.1.{i // 10}.{i % 10}") for i in range(10) for _ in range(5)]

    # Erste Quelle: alle 5 unter dem Quellenlimit und unter dem globalen.
    assert blocked[:5] == [False] * 5

    # Das globale Budget ist inklusive: Versuch 31 (global_count > 30) ist der
    # erste, der durchfaellt. 5 Quellen x 5 = 25 weitere kommen noch durch.
    allowed = sum(1 for entry in blocked if not entry)
    assert allowed == 30, f"genau 30 Versuche muessen durchkommen, waren: {allowed}"
    assert blocked[30] is True, "ab dem 31. Versuch muss das globale Budget durchschlagen"
    assert all(blocked[30:]), "danach bleibt es gesperrt"


def test_globales_budget_zaehlt_ueber_alle_typen_von_quellen(memory_store):
    """Auch eine Quelle, die ihr eigenes Limit erreicht, zaehlt global mit.

    Hier sperrt jede Quelle nach 5 Versuchen, das globale Budget laeuft aber
    schon vorher voll: die siebte Quelle wird abgewiesen, obwohl ihre eigene
    Bilanz erst bei 1 steht.
    """
    for source in ("10.2.0.1", "10.2.0.2", "10.2.0.3", "10.2.0.4", "10.2.0.5", "10.2.0.6"):
        for _ in range(5):
            register_failed_key_attempt(source)

    seventh = register_failed_key_attempt("10.2.0.7")

    assert seventh is True, "eine siebte Quelle muss am globalen Budget scheitern"


def test_erfolg_einer_quelle_gibt_nur_ihr_budget_zurueck(memory_store):
    for source in ("10.3.0.1", "10.3.0.2"):
        for _ in range(5):
            register_failed_key_attempt(source)

    reset_failed_key_attempts("10.3.0.1")

    assert register_failed_key_attempt("10.3.0.2") is True, (
        "der Erfolg einer anderen Quelle darf deren Budget nicht leeren"
    )


# ── Luecke 3: geteilter Speicher fuer mehrere Replikas ─────────────────────

def test_mehrere_replikas_ohne_geteilten_speicher_werden_abgelehnt(memory_store):
    """Sonst startet die Installation gesund und drosselt um N-fach schwaecher."""
    with pytest.raises(ValueError, match="shared rate-limit store"):
        assert_shared_store_for_replicas(2)


def test_eine_replika_braucht_nichts_geteiltes(memory_store):
    assert_shared_store_for_replicas(1)


def test_sqlite_speicher_gilt_als_geteilt(tmp_path, monkeypatch):
    store = SqliteAttemptStore(tmp_path / "rl.sqlite", window=60)
    monkeypatch.setattr(store_mod, "_store", store)

    assert store_mod.store_is_shared() is True
    assert_shared_store_for_replicas(3)


def test_sqlite_und_memory_zaehlen_gleich(tmp_path):
    """Beide Backends muessen dieselbe Semantik haben, sonst haengt die
    Absicherung an der Wahl des Betreibers."""
    memory = MemoryAttemptStore(window=60)
    sqlite = SqliteAttemptStore(tmp_path / "rl.sqlite", window=60)

    for index in range(7):
        memory.record("shared")
        sqlite.record("shared")

    assert memory.count("shared") == sqlite.count("shared") == 7
    memory.reset("shared")
    sqlite.reset("shared")
    assert memory.count("shared") == sqlite.count("shared") == 0


def test_sqlite_ueberlebt_einen_neuen_store(tmp_path):
    """Der Punkt von SQLite: ein zweiter Prozess sieht denselben Stand."""
    path = tmp_path / "rl.sqlite"
    first = SqliteAttemptStore(path, window=60)
    for _ in range(3):
        first.record("10.9.9.9")

    second = SqliteAttemptStore(path, window=60)

    assert second.count("10.9.9.9") == 3, "eine zweite Instanz muss den Zaehler sehen"


def test_verfall_aeltere_versuche(monkeypatch):
    """Nach dem Fenster gehoeren alte Versuche nicht mehr zum Budget."""
    store = MemoryAttemptStore(window=60)
    for _ in range(5):
        store.record("10.8.0.1")

    assert store.count("10.8.0.1") == 5

    # 120 s weiter: alle fuenf Versuche liegen ausserhalb des 60-s-Fensters.
    shifted = time.monotonic() + 120
    monkeypatch.setattr(store_mod.time, "monotonic", lambda: shifted)
    assert store.count("10.8.0.1") == 0


# ── Luecke 1, Ende zu Ende: beide Formulare teilen das Budget ──────────────

def test_login_und_oauth_teilen_das_budget(memory_store):
    """/login und /oauth/authorize pruefen denselben Key — und muessen sich
    dasselbe Budget teilen. Vorher hatte jedes Formular ein eigenes, sodass
    nach 5 Versuchen im Login-Tier noch 20 ueber OAuth folgten."""
    from app.rate_limiter import register_failed_key_attempt as book

    class _Req:
        def __init__(self, path: str) -> None:
            self.url = type("U", (), {"path": path})()
            self.method = "POST"
            self.client = type("C", (), {"host": "10.5.0.1"})()
            self.headers = {}

    # 5 Fehlversuche ueber das Login-Formular sind noch erlaubt.
    for attempt in range(5):
        assert book(_Req("/login")) is False, f"Login-Versuch {attempt + 1} sollte noch gehen"

    # Der naechste Fehlversuch — egal ueber welches Formular — muss sperren.
    assert book(_Req("/oauth/authorize")) is True, (
        "das OAuth-Formular muss das Budget des Login-Formulars mitzaehlen"
    )
