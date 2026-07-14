"""Server-seitiger Session-Store mit Datei-Persistenz.

Trennt das HTTP-Cookie-Token vom API-Key:
- Login erzeugt ein zufaelliges, nicht erratbares Session-Token
- Persistiert wird nur SHA-256(Token) -> (username, role, expires_at)
- Cookie enthaelt NUR das Token — bei Leak kann der Server-Admin das
  Token widerrufen, ohne den API-Key zu rotieren.
- Tokens haben eine TTL mit Sliding Expiration (wird bei jedem Zugriff
  erneuert). Abgelaufene werden beim Lookup entfernt.
- Sessions werden in eine JSON-Datei geschrieben und ueberleben
  Container-Restarts.
"""
from __future__ import annotations

import json
import hashlib
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass
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
    expires_at: float


# In-Memory-Cache (Quelle der Wahrheit laeuft, Datei ist Backup)
_sessions: dict[str, SessionRecord] = {}
_loaded = False


def _token_hash(token: str) -> str:
    """Nicht umkehrbarer Lookup-Key fuer Session-Tokens."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


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
        needs_migration = (_SESSION_FILE.stat().st_mode & 0o777) != 0o600
        for stored_key, rec in data.items():
            token = str(rec.get("token", ""))
            stored_key_is_hash = len(stored_key) == 64 and all(
                char in "0123456789abcdef" for char in stored_key.lower()
            )
            needs_migration = (
                needs_migration
                or bool(token)
                or "api_key" in rec
                or not stored_key_is_hash
                or rec.get("expires_at", 0) <= now
            )
            if rec.get("expires_at", 0) > now:
                # Alte Dateien verwendeten das Roh-Token als Key und legten
                # Token/API-Key nochmals im Record ab. Beim Laden werden sie
                # unmittelbar auf das sichere Format reduziert.
                lookup_key = stored_key if stored_key_is_hash else _token_hash(token or stored_key)
                _sessions[lookup_key] = SessionRecord(
                    token=token,
                    username=str(rec["username"]),
                    role=str(rec["role"]),
                    expires_at=float(rec["expires_at"]),
                )
                loaded += 1
        if needs_migration:
            _save_to_disk()
        logger.info("sessions: %d active sessions loaded from disk", loaded)
    except Exception:
        logger.warning("sessions: could not load %s, starting fresh", _SESSION_FILE, exc_info=True)


def _save_to_disk() -> None:
    """Schreibt alle aktiven Sessions in die JSON-Datei."""
    tmp_path: Path | None = None
    try:
        _SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = {
            token_hash: {
                "username": record.username,
                "role": record.role,
                "expires_at": record.expires_at,
            }
            for token_hash, record in _sessions.items()
        }
        import tempfile

        fd, tmp_name = tempfile.mkstemp(
            prefix=".sessions-", suffix=".json", dir=str(_SESSION_FILE.parent)
        )
        tmp_path = Path(tmp_name)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data, indent=2))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, _SESSION_FILE)
        _SESSION_FILE.chmod(0o600)
    except Exception:
        logger.warning("sessions: could not persist to %s", _SESSION_FILE, exc_info=True)
    finally:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink()


def _prune_expired(now: float | None = None) -> None:
    """Entfernt abgelaufene Sessions."""
    now = now if now is not None else _now()
    expired = [t for t, r in _sessions.items() if r.expires_at < now]
    for t in expired:
        del _sessions[t]
    if expired:
        _save_to_disk()


def create_session(username: str, role: str, _api_key: str = "") -> SessionRecord:
    """Erzeugt ein neues Session-Token und speichert es."""
    _load_from_disk()
    with _lock:
        _prune_expired()
        token = secrets.token_urlsafe(32)
        record = SessionRecord(
            token=token,
            username=username,
            role=role,
            expires_at=_now() + _SESSION_TTL_SECONDS,
        )
        _sessions[_token_hash(token)] = record
        _save_to_disk()
    return record


def lookup_session(token: str) -> SessionRecord | None:
    """Liefert die Session zu einem Token, oder None (auch bei Ablauf).
    Erneuert den Ablaufzeitpunkt bei jedem Zugriff (Sliding Expiration)."""
    _load_from_disk()
    if not token:
        return None
    with _lock:
        lookup_key = _token_hash(token)
        record = _sessions.get(lookup_key)
        if record is None:
            return None
        now = _now()
        if record.expires_at < now:
            del _sessions[lookup_key]
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
        lookup_key = _token_hash(token)
        if lookup_key in _sessions:
            del _sessions[lookup_key]
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
