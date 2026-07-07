"""Server-seitiger Session-Store mit Datei-Persistenz.

Trennt das HTTP-Cookie-Token vom API-Key:
- Login erzeugt ein zufaelliges, nicht erratbares Session-Token
- Token wird hier gespeichert (Token -> (username, role, api_key, expires_at))
- Cookie enthaelt NUR das Token — bei Leak kann der Server-Admin das
  Token widerrufen, ohne den API-Key zu rotieren.
- Tokens haben eine TTL mit Sliding Expiration (wird bei jedem Zugriff
  erneuert). Abgelaufene werden beim Lookup entfernt.
- Sessions werden in eine JSON-Datei geschrieben und ueberleben
  Container-Restarts.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

logger = logging.getLogger("kiwiki.session")

# Default-TTL: 12 Stunden. Ueber KIWIKI_SESSION_TTL_SECONDS ueberschreibbar.
_SESSION_TTL_SECONDS = int(os.getenv("KIWIKI_SESSION_TTL_SECONDS", str(12 * 3600)))

# Sliding-Expiration-Renewal wird nur auf Disk persistiert, wenn sich
# expires_at um mehr als diesen Wert verschiebt — sonst wuerde jeder
# authentifizierte Request einen vollen JSON-Dump der Session-Datei ausloesen.
_SESSION_SAVE_DEBOUNCE_SECONDS = 60

# Persistenz-Datei im Datenverzeichnis
_DATA_DIR = Path(os.getenv("KIWIKI_DATA_DIR", "/data"))
_SESSION_FILE = _DATA_DIR / "sessions.json"

# Schutz gegen parallele Schreibzugriffe
_lock = threading.Lock()


@dataclass
class SessionRecord:
    token: str
    username: str
    role: str
    api_key: str  # wird z.B. fuer Logout/Audit gebraucht
    expires_at: float


# In-Memory-Cache (Quelle der Wahrheit laeuft, Datei ist Backup)
_sessions: dict[str, SessionRecord] = {}
_loaded = False


def _now() -> float:
    return time.time()


def _load_from_disk() -> None:
    """Laedt Sessions aus der JSON-Datei in den Memory-Cache."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    if not _SESSION_FILE.exists():
        return
    try:
        data = json.loads(_SESSION_FILE.read_text())
        now = _now()
        loaded = 0
        for token, rec in data.items():
            if rec.get("expires_at", 0) > now:
                _sessions[token] = SessionRecord(**rec)
                loaded += 1
        logger.info("sessions: %d active sessions loaded from disk", loaded)
    except Exception:
        logger.warning("sessions: could not load %s, starting fresh", _SESSION_FILE, exc_info=True)


def _save_to_disk() -> None:
    """Schreibt alle aktiven Sessions in die JSON-Datei."""
    try:
        _SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = {t: asdict(r) for t, r in _sessions.items()}
        _SESSION_FILE.write_text(json.dumps(data, indent=2))
    except Exception:
        logger.warning("sessions: could not persist to %s", _SESSION_FILE, exc_info=True)


def _prune_expired(now: float | None = None) -> None:
    """Entfernt abgelaufene Sessions."""
    now = now if now is not None else _now()
    expired = [t for t, r in _sessions.items() if r.expires_at < now]
    for t in expired:
        del _sessions[t]
    if expired:
        _save_to_disk()


def create_session(username: str, role: str, api_key: str) -> SessionRecord:
    """Erzeugt ein neues Session-Token und speichert es."""
    _load_from_disk()
    with _lock:
        _prune_expired()
        token = secrets.token_urlsafe(32)
        record = SessionRecord(
            token=token,
            username=username,
            role=role,
            api_key=api_key,
            expires_at=_now() + _SESSION_TTL_SECONDS,
        )
        _sessions[token] = record
        _save_to_disk()
    return record


def lookup_session(token: str) -> SessionRecord | None:
    """Liefert die Session zu einem Token, oder None (auch bei Ablauf).
    Erneuert den Ablaufzeitpunkt bei jedem Zugriff (Sliding Expiration)."""
    _load_from_disk()
    if not token:
        return None
    with _lock:
        record = _sessions.get(token)
        if record is None:
            return None
        now = _now()
        if record.expires_at < now:
            del _sessions[token]
            _save_to_disk()
            return None
        # Sliding Expiration: Ablaufzeit bei jedem Zugriff erneuern, aber
        # nur bei spuerbarer Verschiebung auch auf Disk persistieren.
        new_expires_at = now + _SESSION_TTL_SECONDS
        should_persist = new_expires_at - record.expires_at > _SESSION_SAVE_DEBOUNCE_SECONDS
        record.expires_at = new_expires_at
        if should_persist:
            _save_to_disk()
    return record


def revoke_session(token: str) -> bool:
    """Loescht eine Session (Logout)."""
    _load_from_disk()
    with _lock:
        if token in _sessions:
            del _sessions[token]
            _save_to_disk()
            return True
        return False


def revoke_all_for_user(username: str) -> int:
    """Loescht alle Sessions eines Users (z.B. bei Passwort-Rotation)."""
    _load_from_disk()
    with _lock:
        to_delete = [t for t, r in _sessions.items() if r.username == username]
        for t in to_delete:
            del _sessions[t]
        if to_delete:
            _save_to_disk()
        return len(to_delete)


def active_session_count() -> int:
    """Fuer Diagnose/Tests."""
    _load_from_disk()
    with _lock:
        _prune_expired()
        return len(_sessions)


def session_ttl_seconds() -> int:
    return _SESSION_TTL_SECONDS
