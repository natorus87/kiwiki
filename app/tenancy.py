"""Multi-Tenancy: pro User ein isolierter Daten-Namespace unter /data/<username>/.

Funktioniert über einen ContextVar, der pro asyncio-Task (= pro Request)
gesetzt wird. Storage- und Search-Funktionen lesen den Namespace aus diesem
Var, statt einen globalen DATA_DIR zu verwenden.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from contextvars import ContextVar
from pathlib import Path

logger = logging.getLogger("kiwiki.tenancy")

BASE_DATA_DIR = Path(os.getenv("KIWIKI_DATA_DIR", "/data"))

CURRENT_USER_NS: ContextVar[str] = ContextVar("kiwiki_user_ns", default="")

_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

_LEGACY_TOPLEVEL_DIRS = ("notes", "projects", "decisions", "shared", "users")
_LEGACY_TOPLEVEL_FILES = ("index.md", "AGENTS.md")

DEFAULT_USER_FOLDERS = ("notes", "projects", "decisions")


def is_valid_username(username: str) -> bool:
    return bool(username) and bool(_USERNAME_RE.match(username))


def base_data_dir() -> Path:
    """Return the current base data directory.

    Tests and embedded deployments may set KIWIKI_DATA_DIR after import time, so
    runtime code must not rely on the module-level compatibility constant.
    """
    return Path(os.getenv("KIWIKI_DATA_DIR", str(BASE_DATA_DIR)))


def set_user_ns(username: str) -> None:
    if not is_valid_username(username):
        raise ValueError(f"Invalid username for namespace: {username!r}")
    CURRENT_USER_NS.set(username)


def current_user_ns() -> str:
    ns = CURRENT_USER_NS.get()
    if not ns:
        raise RuntimeError(
            "No user namespace set for this request — auth missing or middleware "
            "skipped this code path."
        )
    return ns


def user_root(username: str | None = None) -> Path:
    """Return the data root for the given user (or current request user)."""
    ns = username if username is not None else current_user_ns()
    if not is_valid_username(ns):
        raise ValueError(f"Invalid username: {ns!r}")
    return base_data_dir() / ns


def ensure_user_workspace(username: str) -> Path:
    """Create user root, default folders and seed files (idempotent)."""
    root = user_root(username)
    root.mkdir(parents=True, exist_ok=True)
    for sub in DEFAULT_USER_FOLDERS:
        (root / sub).mkdir(parents=True, exist_ok=True)
    agents = root / "AGENTS.md"
    if not agents.exists():
        agents.write_text(_DEFAULT_AGENTS_MD, encoding="utf-8")
    index = root / "index.md"
    if not index.exists():
        index.write_text(_default_index_md(username), encoding="utf-8")
    return root


def migrate_legacy_data_dir() -> bool:
    """Move pre-multi-tenant /data layout into /data/admin/. Idempotent."""
    base = base_data_dir()
    base.mkdir(parents=True, exist_ok=True)
    admin_root = base / "admin"
    if admin_root.exists():
        return False

    legacy_items = [
        base / name
        for name in _LEGACY_TOPLEVEL_DIRS + _LEGACY_TOPLEVEL_FILES
        if (base / name).exists()
    ]
    if not legacy_items:
        return False

    admin_root.mkdir(parents=True, exist_ok=False)
    for src in legacy_items:
        dst = admin_root / src.name
        shutil.move(str(src), str(dst))
        logger.info("Migrated %s → %s", src, dst)

    legacy_db = base / ".kiwiki"
    if legacy_db.exists() and not (admin_root / ".kiwiki").exists():
        shutil.move(str(legacy_db), str(admin_root / ".kiwiki"))
        logger.info("Migrated legacy search index into admin namespace")

    logger.warning(
        "Multi-tenant migration complete: %d items moved into %s",
        len(legacy_items), admin_root,
    )
    return True


_DEFAULT_AGENTS_MD = """---
title: "KI-Wiki Arbeitsanweisung"
type: "system"
created: "2026-05-21"
updated: "2026-05-21"
tags: []
owner: "system"
---

# KI-Wiki Arbeitsanweisung

Dies ist dein persönlicher Wissensspeicher.

## Regeln

1. Lies zuerst `index.md` und diese Datei.
2. Markdown-Dateien sind die Wahrheit.
3. Nutze bestehende Dateien und Ordner, bevor du neue anlegst.
4. Schreibe kurze, klare Markdown-Dateien.
5. Verwende Frontmatter.
6. Lösche keine Inhalte.
7. Ergänze bestehende Dateien, wenn möglich.
8. Neue Entscheidungen kommen nach `/decisions`.
9. Projektwissen kommt nach `/projects`.
10. Allgemeine Notizen kommen nach `/notes`.

## Frontmatter-Format

```
---
title: "Titel"
type: "note"
created: "YYYY-MM-DD"
updated: "YYYY-MM-DD"
tags: []
owner: "username"
---
```
"""


def _default_index_md(username: str) -> str:
    return f"""---
title: "kiwiki Index ({username})"
type: "index"
created: "2026-05-21"
updated: "2026-05-21"
tags: []
owner: "{username}"
---

# kiwiki Wissensindex

Persönlicher Wissensspeicher für **{username}**.
Lies zuerst `AGENTS.md` für KI-Arbeitsanweisungen.

## Struktur

- `/notes` — Allgemeine Notizen
- `/projects` — Projektwissen
- `/decisions` — Architekturentscheidungen
"""
