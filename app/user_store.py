from __future__ import annotations

import logging
import os
import secrets
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

from .tenancy import base_data_dir, is_valid_username


Role = Literal["read", "write", "admin"]
UserSource = Literal["builtin", "local"]

ROLE_HIERARCHY = {"read": 0, "write": 1, "admin": 2}

logger = logging.getLogger("kiwiki.auth")

_PARSE_DIAG_LOGGED = False
_LOCAL_DIAG_LOGGED = False
_MERGE_DIAG_LOGGED = False


@dataclass(frozen=True)
class UserRecord:
    username: str
    key: str
    role: Role
    source: UserSource


def _users_file() -> Path:
    return base_data_dir() / ".kiwiki" / "users.yaml"


def _parse_entries(raw: str | None, source: UserSource) -> tuple[dict[str, UserRecord], list[str]]:
    result: dict[str, UserRecord] = {}
    diagnostics: list[str] = []
    if not raw:
        return result, diagnostics

    for idx, raw_entry in enumerate(raw.split(",")):
        entry = raw_entry.strip()
        if not entry:
            continue
        parts = [p.strip() for p in entry.split(":")]
        if len(parts) != 3:
            diagnostics.append(
                f"entry #{idx + 1}: expected 3 colon-separated fields, got {len(parts)} "
                f"(check for stray ':' inside the API key or a missing role)"
            )
            continue
        username, key, role = parts
        if not username or not key or not role:
            diagnostics.append(f"entry #{idx + 1}: empty username, key or role")
            continue
        if role not in ROLE_HIERARCHY:
            diagnostics.append(
                f"entry #{idx + 1} (user={username!r}): unknown role {role!r}; "
                f"allowed: {sorted(ROLE_HIERARCHY)}"
            )
            continue
        if not is_valid_username(username):
            diagnostics.append(
                f"entry #{idx + 1} (user={username!r}): username must match "
                f"[a-zA-Z0-9_-]{{1,64}} to be safe as a directory name"
            )
            continue
        if key in result:
            prev_user = result[key].username
            diagnostics.append(
                f"entry #{idx + 1} (user={username!r}): duplicate API key, "
                f"overrides previous user {prev_user!r}"
            )
        result[key] = UserRecord(username=username, key=key, role=role, source=source)  # type: ignore[arg-type]
    return result, diagnostics


def builtin_users_by_key() -> dict[str, UserRecord]:
    global _PARSE_DIAG_LOGGED
    users_str = os.getenv("KIWIKI_USERS")
    if not users_str:
        logger.error(
            "KIWIKI_USERS: No builtin users configured — production deployments MUST set "
            "KIWIKI_USERS. The container will run but nobody can log in unless local users exist."
        )
        return {}

    result, diagnostics = _parse_entries(users_str, "builtin")
    if not _PARSE_DIAG_LOGGED:
        for msg in diagnostics:
            logger.warning("KIWIKI_USERS: %s", msg)
        if not result:
            logger.error(
                "KIWIKI_USERS: no valid builtin user entries parsed. "
                "Check the env variable format: user:key:role,user2:key2:role2"
            )
        else:
            summary = ", ".join(f"{u.username}({u.role})" for u in result.values())
            logger.info("KIWIKI_USERS: %d builtin user(s) loaded — %s", len(result), summary)
        _PARSE_DIAG_LOGGED = True
    return result


def local_users_by_key() -> dict[str, UserRecord]:
    global _LOCAL_DIAG_LOGGED
    path = _users_file()
    if not path.exists():
        return {}

    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        if not _LOCAL_DIAG_LOGGED:
            logger.error("Local users: cannot read %s: %s", path, exc)
            _LOCAL_DIAG_LOGGED = True
        return {}

    raw_users = payload.get("users", [])
    if not isinstance(raw_users, list):
        if not _LOCAL_DIAG_LOGGED:
            logger.error("Local users: %s must contain a top-level users list", path)
            _LOCAL_DIAG_LOGGED = True
        return {}

    result: dict[str, UserRecord] = {}
    diagnostics: list[str] = []
    for idx, item in enumerate(raw_users):
        if not isinstance(item, dict):
            diagnostics.append(f"entry #{idx + 1}: expected mapping")
            continue
        username = str(item.get("username", "")).strip()
        key = str(item.get("key", "")).strip()
        role = str(item.get("role", "")).strip()
        if not username or not key or not role:
            diagnostics.append(f"entry #{idx + 1}: empty username, key or role")
            continue
        if role not in ROLE_HIERARCHY:
            diagnostics.append(f"entry #{idx + 1} (user={username!r}): unknown role {role!r}")
            continue
        if not is_valid_username(username):
            diagnostics.append(f"entry #{idx + 1} (user={username!r}): invalid username")
            continue
        if key in result:
            diagnostics.append(f"entry #{idx + 1} (user={username!r}): duplicate local API key")
            continue
        result[key] = UserRecord(username=username, key=key, role=role, source="local")  # type: ignore[arg-type]

    if diagnostics and not _LOCAL_DIAG_LOGGED:
        for msg in diagnostics:
            logger.warning("Local users: %s", msg)
        _LOCAL_DIAG_LOGGED = True
    return result


def users_by_key() -> dict[str, UserRecord]:
    global _MERGE_DIAG_LOGGED
    users = builtin_users_by_key()
    local = local_users_by_key()
    builtin_names = {record.username for record in users.values()}
    merge_diagnostics: list[str] = []
    for key, record in local.items():
        if key in users:
            merge_diagnostics.append(
                f"user {record.username!r} ignored because API key collides with builtin user"
            )
            continue
        if record.username in builtin_names:
            merge_diagnostics.append(
                f"user {record.username!r} ignored because username is builtin"
            )
            continue
        users[key] = record
    if merge_diagnostics and not _MERGE_DIAG_LOGGED:
        for msg in merge_diagnostics:
            logger.warning("Local users: %s", msg)
        _MERGE_DIAG_LOGGED = True
    return users


def list_users() -> list[UserRecord]:
    return sorted(users_by_key().values(), key=lambda u: (u.source != "builtin", u.username.lower()))


def generate_api_key() -> str:
    existing_keys = set(users_by_key())
    for _ in range(10):
        key = secrets.token_urlsafe(32)
        if key not in existing_keys:
            return key
    raise RuntimeError("Could not generate unique API key")


def _write_local_users(records: list[UserRecord]) -> None:
    path = _users_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "users": [
            {"username": r.username, "key": r.key, "role": r.role}
            for r in sorted(records, key=lambda u: u.username.lower())
        ]
    }
    data = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".users-", suffix=".yaml", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
        os.replace(tmp_name, path)
        path.chmod(0o600)
    finally:
        tmp = Path(tmp_name)
        if tmp.exists():
            tmp.unlink()


def create_local_user(username: str, key: str, role: str) -> UserRecord:
    username = username.strip()
    key = key.strip()
    role = role.strip()
    if not is_valid_username(username):
        raise ValueError("Benutzername muss [a-zA-Z0-9_-]{1,64} entsprechen")
    if not key:
        raise ValueError("API-Key darf nicht leer sein")
    if ":" in key or "," in key:
        raise ValueError("API-Key darf keine Kommas oder Doppelpunkte enthalten")
    if role not in ROLE_HIERARCHY:
        raise ValueError("Unbekannte Rolle")

    all_users = users_by_key()
    if key in all_users:
        raise ValueError("API-Key wird bereits verwendet")
    if any(user.username == username for user in all_users.values()):
        raise ValueError("Benutzername existiert bereits")

    local = list(local_users_by_key().values())
    record = UserRecord(username=username, key=key, role=role, source="local")  # type: ignore[arg-type]
    local.append(record)
    _write_local_users(local)
    return record


def remove_workspace_for_user(username: str) -> None:
    """Loescht den Daten-Workspace eines Users (Workaround fuer atomare
    User-Erstellung, falls die Persistierung nach dem Workspace-Init
    fehlschlaegt)."""
    import shutil

    if not is_valid_username(username):
        raise ValueError("Invalid username for workspace removal")
    from .tenancy import base_data_dir

    user_dir = base_data_dir() / username
    if user_dir.exists():
        shutil.rmtree(user_dir, ignore_errors=True)


def delete_local_user(username: str) -> None:
    username = username.strip()
    if not is_valid_username(username):
        raise ValueError("Ungültiger Benutzername")
    if any(user.username == username for user in builtin_users_by_key().values()):
        raise ValueError("Builtin-Benutzer können nur über KIWIKI_USERS geändert werden")

    local = list(local_users_by_key().values())
    kept = [user for user in local if user.username != username]
    if len(kept) == len(local):
        raise FileNotFoundError("Lokaler Benutzer nicht gefunden")
    _write_local_users(kept)
