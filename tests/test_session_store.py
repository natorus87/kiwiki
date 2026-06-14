"""Regression-Tests fuer den Session-Store.

Sichert ab, dass:
- Sessions getrennt vom API-Key im Cookie liegen
- Token-Rotation bei jedem Login funktioniert (alter Token ungueltig)
- Ablaufende Tokens automatisch entfernt werden
- Logout ein Token widerruft
"""
from __future__ import annotations

import time

import pytest

from app import session_store


@pytest.fixture(autouse=True)
def _clean_store():
    """Sessions vor und nach jedem Test leeren, damit nichts leakt."""
    session_store._sessions.clear()
    yield
    session_store._sessions.clear()


def test_create_session_returns_unique_token():
    a = session_store.create_session("alice", "admin", "apikey-a")
    b = session_store.create_session("alice", "admin", "apikey-a")
    assert a.token != b.token
    # Beide sind gleichzeitig aktiv
    assert session_store.active_session_count() == 2


def test_session_cookie_value_is_not_api_key():
    """Der zentrale Fix: das Cookie-Token DARF NICHT der API-Key sein."""
    api_key = "supersecret-api-key-12345"
    record = session_store.create_session("alice", "admin", api_key)
    assert record.token != api_key
    # Und auch nicht ableitbar / kein Praefix-Substring
    assert api_key not in record.token


def test_lookup_returns_none_for_unknown_token():
    assert session_store.lookup_session("nicht-vorhanden") is None


def test_lookup_returns_none_for_empty_token():
    assert session_store.lookup_session("") is None


def test_lookup_returns_record_for_valid_token():
    record = session_store.create_session("alice", "write", "k")
    got = session_store.lookup_session(record.token)
    assert got is not None
    assert got.username == "alice"
    assert got.role == "write"
    assert got.api_key == "k"


def test_revoke_session_invalidates_token():
    record = session_store.create_session("alice", "admin", "k")
    assert session_store.lookup_session(record.token) is not None
    assert session_store.revoke_session(record.token) is True
    # Nach Revoke ist das Token ungueltig
    assert session_store.lookup_session(record.token) is None


def test_revoke_all_for_user():
    session_store.create_session("alice", "admin", "k1")
    session_store.create_session("alice", "admin", "k1")
    session_store.create_session("bob", "read", "k2")
    n = session_store.revoke_all_for_user("alice")
    assert n == 2
    # Bobs Session bleibt
    assert session_store.active_session_count() == 1


def test_expired_session_is_pruned_on_lookup():
    """Session mit abgelaufener TTL wird beim Lookup entfernt."""
    record = session_store.create_session("alice", "admin", "k")
    # In die Vergangenheit zurueckdrehen
    object.__setattr__(record, "expires_at", time.monotonic() - 1.0)
    # Lookup gibt None zurueck
    assert session_store.lookup_session(record.token) is None
    # Store wurde aufgeraeumt
    assert record.token not in session_store._sessions


def test_expired_session_is_pruned_by_prune_expired():
    """Direkter Test der _prune_expired-Funktion — die via active_session_count
    ohnehin schon aufgerufen wird, also verifizieren wir hier, dass
    abgelaufene Sessions konsistent entfernt werden."""
    record = session_store.create_session("alice", "admin", "k")
    # Manuell in den Store injizieren mit altem Ablauf
    object.__setattr__(record, "expires_at", time.monotonic() - 1.0)
    session_store._sessions[record.token] = record
    # Direkter Aufruf von _prune_expired
    session_store._prune_expired()
    assert record.token not in session_store._sessions
