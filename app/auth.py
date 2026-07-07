import logging
import secrets
from fastapi import HTTPException, status
from fastapi.requests import Request
from .models import User
from .tenancy import set_user_ns, is_valid_username
from . import user_store


ROLE_HIERARCHY = user_store.ROLE_HIERARCHY

logger = logging.getLogger("kiwiki.auth")

_PARSE_DIAG_LOGGED = False


def parse_users() -> dict[str, tuple[str, str]]:
    """
    Parse all configured users.
    Builtin users come from KIWIKI_USERS, local users from /data/.kiwiki/users.yaml.
    Returns: {key: (username, role), ...}
    """
    return {key: (record.username, record.role) for key, record in user_store.users_by_key().items()}


def _lookup_api_key(users_map: dict[str, tuple[str, str]], candidate: str) -> tuple[str, str] | None:
    """Constant-time membership check against configured API keys.

    A plain ``candidate in users_map`` dict lookup short-circuits on the
    first mismatching key it hits in the hash bucket, which can leak
    timing information about whether an attacker-supplied key is close to
    a valid one. This always compares against every configured key.
    """
    found = None
    for key, value in users_map.items():
        if secrets.compare_digest(candidate.encode("utf-8"), key.encode("utf-8")):
            found = value
    return found


async def get_current_user(request: Request) -> User:
    """
    Extract user from Authorization header (Bearer) or session cookie.

    Reihenfolge:
    1. Authorization: Bearer <api_key>           → API-Key direkt
    2. kiwiki_session-Cookie                     → zuerst als Session-Token
       pruefen (siehe session_store); falls kein Token-Treffer, Fallback
       auf den alten Pfad: Cookie-Wert wird als API-Key interpretiert
       (Backwards-Compat fuer externe Clients, die den API-Key direkt
       ins Cookie setzen).
    3. Sonst 401.

    Must be ``async`` so the ContextVar set via :func:`set_user_ns` lives in the
    same asyncio task as the calling endpoint — sync dependencies would run in
    a threadpool and the namespace wouldn't propagate.
    """
    from . import session_store  # lokaler Import, um zirkulaere Importe zu vermeiden

    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        api_key = auth_header[7:]
        if not api_key:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Empty Bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        users_map = parse_users()
        match = _lookup_api_key(users_map, api_key)
        if match is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid API key",
                headers={"WWW-Authenticate": "Bearer"},
            )
        username, role = match
        if not is_valid_username(username):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Username contains characters not allowed as namespace",
            )
        set_user_ns(username)
        return User(username=username, role=role)

    # Cookie-basierte Auth: zuerst Session-Token pruefen
    cookie_val = request.cookies.get("kiwiki_session", "")
    if cookie_val:
        record = session_store.lookup_session(cookie_val)
        if record is not None:
            if not is_valid_username(record.username):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Username contains characters not allowed as namespace",
                )
            set_user_ns(record.username)
            return User(username=record.username, role=record.role)
        # Kein Session-Token-Treffer — Backwards-Compat: vielleicht ist
        # der Cookie-Wert der API-Key (fuer direkte API-Clients, die den
        # API-Key ins Cookie legen).
        users_map = parse_users()
        match = _lookup_api_key(users_map, cookie_val)
        if match is not None:
            username, role = match
            if not is_valid_username(username):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Username contains characters not allowed as namespace",
                )
            set_user_ns(username)
            return User(username=username, role=role)

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Missing or invalid Authorization header",
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_role(min_role: str):
    """
    FastAPI dependency factory that enforces a minimum role.
    min_role: "read", "write", or "admin"
    """
    from fastapi import Depends

    async def check_role(user: User = Depends(get_current_user)) -> User:
        if ROLE_HIERARCHY.get(user.role, -1) < ROLE_HIERARCHY.get(min_role, 999):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{min_role}' required, got '{user.role}'",
            )
        return user

    return check_role
