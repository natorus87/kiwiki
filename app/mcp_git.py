"""Abgesicherte Git-Helfer fuer die MCP-History-Werkzeuge."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .storage import safe_path


GIT_TIMEOUT_SECONDS = 10
_GIT_REVISION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@{}+~^-]{0,127}$")


def validate_git_revision(value: str) -> str:
    revision = str(value).strip()
    if not _GIT_REVISION_RE.fullmatch(revision) or ".." in revision:
        raise ValueError("Invalid git revision")
    return revision


def validate_git_path(value: str) -> str:
    path = str(value).strip()
    if not path or path.startswith("-") or Path(path).is_absolute() or "\x00" in path:
        raise ValueError("Invalid git path")
    safe_path(path)
    return path.replace("\\", "/")


def run_git(root: Path, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_SECONDS,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "git command failed").strip()
        raise ValueError(detail[:300])
    return result
