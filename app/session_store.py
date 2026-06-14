"""Server-seitiger Session-Store.

Trennt das HTTP-Cookie-Token vom API-Key:
- Login erzeugt ein zufaelliges, nicht erratbares Session-Token
- Token wird hier gespeichert (Token -> (username, role, api_key, expires_at))
- Cookie enthaelt NUR das Token — bei Leak kann der Server-Admin das
  Token widerrufen, ohne den API-Key zu rotieren.
- Tokens haben eine TTL; abgelaufene werden beim Lookup entfernt.
"""
from __future__ import annotations

import logging
import os
import secrets
import time
from dataclasses import dataclass

logger = logging.getLogger("kiwiki.session")

# Default-TTL: 12 Stunden. Ueber KIWIKI_SESSION_TTL_SECONDS ueberschreibbar.
_SESSION_TTL_SECONDS = int(os.getenv("KIWIKI_SESSION_TTL_SECONDS", str(12 * 3600)))


@dataclass(frozen=True)
class SessionRecord:
    token: str
    username: str
    role: str
    api_key: str  # wird z.B. fuer Logout/Audit gebraucht
    expires_at: float


# In-Memory-Store. In einer Multi-Worker-Umgebung sollte das gegen
# eine externe Store (Redis o.ae.) getauscht werden — derzeit ist das
# die Quelle der Wahrheit pro Prozess.
_sessions: dict[str, SessionRecord] = {}


def _now() -> float:
    return time.monotonic()


def _prune_expired(now: float | None = None) -> None:
    """Entfernt abgelaufene Sessions; wird automatisch von Lookup/Logout
    aufgerufen, plus in groesseren Abstaenden via set_session_ttl-Pruning."""
    now = now if now is not None else _now()
    expired = [t for t, r in _sessions.items() if r.expires_at < now]
    for t in expired:
        del _sessions[t]


def create_session(username: str, role: str, api_key: str) -> SessionRecord:
    """Erzeugt ein neues Session-Token und speichert es."""
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
    return record


def lookup_session(token: str) -> SessionRecord | None:
    """Liefert die Session zu einem Token, oder None (auch bei Ablauf)."""
    if not token:
        return None
    record = _sessions.get(token)
    if record is None:
        return None
    if record.expires_at < _now():
        del _sessions[token]
        return None
    return record


def revoke_session(token: str) -> bool:
    """Loescht eine Session (Logout)."""
    if token in _sessions:
        del _sessions[token]
        return True
    return False


def revoke_all_for_user(username: str) -> int:
    """Loescht alle Sessions eines Users (z.B. bei Passwort-Rotation)."""
    to_delete = [t for t, r in _sessions.items() if r.username == username]
    for t in to_delete:
        del _sessions[t]
    return len(to_delete)


def active_session_count() -> int:
    """Fuer Diagnose/Tests."""
    _prune_expired()
    return len(_sessions)


def session_ttl_seconds() -> int:
    return _SESSION_TTL_SECONDS
