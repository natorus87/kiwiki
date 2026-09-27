"""Gemeinsames Fehlversuchs-Budget fuer die API-Key-Pruefung.

`/login` und `/oauth/authorize` pruefen denselben API-Key. Vorher hatte jedes
Formular sein eigenes Budget, sodass nach fuenf Fehlversuchen im Login-Tier im
oauth-Tier (20/min) noch zwanzig weitere folgten. Die Grenze galt damit pro
Formular statt pro Schluessel.

Hier liegt ein Budget, das beide Einstiegspunkte teilen, und zwar zweifach
abgesichert:

* **pro Quelle** (`KIWIKI_KEY_ATTEMPT_LIMIT`, Standard 5/min) — schuetzt ein
  einzelnes Netzwerk bzw. einen einzelnen Nutzer
* **global** (`KIWIKI_KEY_ATTEMPT_GLOBAL_LIMIT`, Standard 30/min) — schuetzt
  das System gegen verteiltes Durchprobieren, bei dem jede Quelle ihr eigenes
  Budget verauscht. Ohne diese Grenze waeren hinter NAT, VPN oder Tor fünf
  Versuche pro ausgehendem Socket erlaubt.

Der Speicher liegt hinter einem schmalen Interface. Standard ist der
prozesslokale Speicher, damit sich am Verhalten eines single-replica-Deploys
nichts aendert. Fuer mehrere Replikas muss `KIWIKI_RATE_LIMIT_STORE=sqlite` auf
einem RWX-Volume gesetzt werden, sonst zaehlt jeder Pod fuer sich und die
Drosselung wird schwaecher statt staerker. Genau das verhindert
`assert_shared_store_for_replicas()`.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from pathlib import Path

logger = logging.getLogger("kiwiki.rate_limiter")

_KEY_ATTEMPT_LIMIT: int = int(os.getenv("KIWIKI_KEY_ATTEMPT_LIMIT", "5"))
_KEY_ATTEMPT_GLOBAL_LIMIT: int = int(os.getenv("KIWIKI_KEY_ATTEMPT_GLOBAL_LIMIT", "30"))
_KEY_ATTEMPT_WINDOW: int = int(os.getenv("KIWIKI_KEY_ATTEMPT_WINDOW_SECONDS", "60"))

_GLOBAL_SCOPE = "*"

# Zaehler werden pro Thread-Worker gehalten; das haelt die In-Memory-Variante
# ohne Lock-beschwerten Hot Path, der Store selbst bleibt thread-sicher.
_local = threading.local()


class AttemptStore:
    """Schnittstelle fuer Fehlversuchs-Zaehler.

    Zwei Operationen genuegen: buchen und pruefen. `window` ist die Groesse des
    gleitenden Fensters in Sekunden, `scope` ist eine beliebige Quellenkennung
    bzw. `_GLOBAL_SCOPE` fuer das systemweite Budget.
    """

    def record(self, scope: str) -> int:
        """Bucht einen Fehlversuch und liefert die Zahl im aktuellen Fenster."""
        raise NotImplementedError

    def count(self, scope: str) -> int:
        """Liefert die Zahl im aktuellen Fenster, ohne zu buchen."""
        raise NotImplementedError

    def reset(self, scope: str) -> None:
        """Leert den Zaehler einer Quelle (nach erfolgreicher Authentifizierung)."""
        raise NotImplementedError

    def prune(self) -> None:
        """Entfernt verwaiste Eintraege."""
        raise NotImplementedError


class MemoryAttemptStore(AttemptStore):
    """Prozesslokaler Speicher. Richtig fuer eine einzelne Replika."""

    def __init__(self, window: int, cleanup_threshold: int = 100) -> None:
        self._window = window
        self._buckets: dict[str, list[float]] = {}
        self._lock = threading.Lock()
        self._cleanup_threshold = cleanup_threshold

    def _prune_locked(self, now: float) -> None:
        cutoff = now - self._window
        for scope in [s for s, values in self._buckets.items() if not any(v > cutoff for v in values)]:
            del self._buckets[scope]

    def record(self, scope: str) -> int:
        now = time.monotonic()
        cutoff = now - self._window
        with self._lock:
            recent = [value for value in self._buckets.get(scope, []) if value > cutoff]
            recent.append(now)
            self._buckets[scope] = recent
            if len(self._buckets) > self._cleanup_threshold:
                self._prune_locked(now)
            return len(recent)

    def count(self, scope: str) -> int:
        now = time.monotonic()
        cutoff = now - self._window
        with self._lock:
            return sum(1 for value in self._buckets.get(scope, []) if value > cutoff)

    def reset(self, scope: str) -> None:
        with self._lock:
            self._buckets.pop(scope, None)

    def prune(self) -> None:
        with self._lock:
            self._prune_locked(time.monotonic())


class SqliteAttemptStore(AttemptStore):
    """Gemeinsamer Speicher auf einem RWX-Volume.

    Traegt mehrere Replikas: jede schreibt in dieselbe Datei, deshalb WAL und
    ein ``busy_timeout``. Das Volumen pro Minute ist winzig (nur Fehlversuche),
    ein Netzwerk-Filesystem wie NFS traegt das problemlos.
    """

    def __init__(self, path: Path, window: int) -> None:
        self._path = Path(path)
        self._window = window
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=5.0, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS key_attempts ("
                "  scope TEXT NOT NULL,"
                "  ts REAL NOT NULL,"
                "  PRIMARY KEY (scope, ts)"
                ")"
            )

    def record(self, scope: str) -> int:
        now = time.time()
        cutoff = now - self._window
        with self._lock, self._connect() as conn:
            conn.execute("INSERT INTO key_attempts (scope, ts) VALUES (?, ?)", (scope, now))
            conn.execute("DELETE FROM key_attempts WHERE ts <= ?", (cutoff,))
            row = conn.execute(
                "SELECT COUNT(*) FROM key_attempts WHERE scope = ? AND ts > ?", (scope, cutoff)
            ).fetchone()
        return int(row[0])

    def count(self, scope: str) -> int:
        cutoff = time.time() - self._window
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM key_attempts WHERE scope = ? AND ts > ?", (scope, cutoff)
            ).fetchone()
        return int(row[0])

    def reset(self, scope: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM key_attempts WHERE scope = ?", (scope,))

    def prune(self) -> None:
        cutoff = time.time() - self._window
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM key_attempts WHERE ts <= ?", (cutoff,))


_store: AttemptStore | None = None


def get_store() -> AttemptStore:
    """Den aktiven Speicher liefern, beim ersten Zugriff erzeugen."""
    global _store
    if _store is None:
        backend = os.getenv("KIWIKI_RATE_LIMIT_STORE", "memory").strip().lower()
        window = _KEY_ATTEMPT_WINDOW
        if backend == "sqlite":
            path = Path(os.getenv("KIWIKI_RATE_LIMIT_STORE_PATH", "/data/.kiwiki/ratelimit.sqlite"))
            _store = SqliteAttemptStore(path, window)
            logger.info("Rate limit store: sqlite at %s", path)
        elif backend == "memory":
            _store = MemoryAttemptStore(window)
        else:
            raise ValueError(f"Unknown KIWIKI_RATE_LIMIT_STORE: {backend!r} (expected 'memory' or 'sqlite')")
    return _store


def set_store(store: AttemptStore | None) -> None:
    """Aktiven Speicher setzen (Tests). `None` erzwingt Neuerzeugung."""
    global _store
    _store = store


def store_is_shared() -> bool:
    """Teilt der aktive Speicher den Zaehler zwischen mehreren Prozessen?"""
    return isinstance(get_store(), SqliteAttemptStore)


def assert_shared_store_for_replicas(replica_count: int) -> None:
    """Mehrere Replikas brauchen einen geteilten Speicher.

    Sonst zaehlt jeder Pod fuer sich: die Installation startet, meldet sich
    gesund und die Drosselung wirkt um den Faktor der Replikas schwaecher,
    ohne ein einziges Log zu schreiben.
    """
    if replica_count > 1 and not store_is_shared():
        raise ValueError(
            f"replicaCount={replica_count} requires a shared rate-limit store. "
            "Set KIWIKI_RATE_LIMIT_STORE=sqlite with a ReadWriteMany volume, "
            "or keep replicaCount=1. Per-process counters make the limits weaker, not stronger."
        )


def register_failed_key_attempt(source: str) -> bool:
    """Fehlversuch buchen und melden, ob die Quelle ausgesperrt werden soll.

    Gesperrt wird, wenn das globale Budget erschoepft ist oder das der Quelle.
    Der Vergleich ist bewusst ``>``: bei genau ``limit`` Versuchen ist das
    Budget noch nicht ueberzogen. Genau der Limit-te Versuch ist also der
    letzte erlaubte — ab dem naechsten ist 401/429 die Antwort.
    """
    store = get_store()
    global_count = store.record(_GLOBAL_SCOPE)
    source_count = store.record(source)
    blocked = global_count > _KEY_ATTEMPT_GLOBAL_LIMIT or source_count > _KEY_ATTEMPT_LIMIT
    if blocked:
        logger.warning(
            "API key attempt limit exceeded: source=%s source_count=%d global_count=%d",
            source,
            source_count,
            global_count,
        )
    return blocked


def reset_failed_key_attempts(source: str) -> None:
    """Zaehler einer Quelle nach erfolgreicher Authentifizierung leeren."""
    get_store().reset(source)
