"""Gemeinsame Fixtures und Konfiguration fuer alle Tests.

Stellt eine isolierte temporaeere Daten-Root zur Verfuegung und mockt
die tenancy-ContextVar pro Test, damit Storage- und Search-Funktionen
immer im richtigen User-Namespace arbeiten.
"""

import sys
from pathlib import Path

import pytest

# Stellt app/ sicher im Python-Path, wenn tests/ ausserhalb von app/ liegt.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def _isolated_data_dir(monkeypatch, tmp_path: Path):
    """Setzt KIWIKI_DATA_DIR auf ein temporaeeres Verzeichnis pro Test.

    Wird automatisch vor jedem Test ausgefuehrt (autouse=True).
    """
    monkeypatch.setenv("KIWIKI_DATA_DIR", str(tmp_path))
    from app.tenancy import CURRENT_USER_NS

    CURRENT_USER_NS.set("")
    # Leert den parse_users-Cache, damit Tests keine alten User-Daten sehen.
    # Setzt das globale Diagnose-Flag zurueck.
    import app.auth as auth_mod

    auth_mod._PARSE_DIAG_LOGGED = False
    import app.user_store as user_store_mod

    user_store_mod._PARSE_DIAG_LOGGED = False
    user_store_mod._LOCAL_DIAG_LOGGED = False
    user_store_mod._MERGE_DIAG_LOGGED = False
    yield tmp_path


@pytest.fixture
def users_map(monkeypatch):
    """Setzt KIWIKI_USERS auf ein definierter Set und gibt die geparste Map zurueck."""

    def _make(*entries):
        """entries: Tupel von (username, key, role)."""
        value = ",".join(f"{u}:{k}:{r}" for u, k, r in entries)
        monkeypatch.setenv("KIWIKI_USERS", value)
        from app.auth import parse_users

        return parse_users()

    return _make


@pytest.fixture
def active_user(monkeypatch, tmp_path: Path):
    """Setzt einen aktuellen User-Namespace fuer Storage-/Search-Tests.

    Gibt den User-Namen zurueck.
    """

    def _make(username: str = "alice"):
        from app.tenancy import set_user_ns

        # Sorgt dafuer, dass der Workspace existiert.
        from app.tenancy import ensure_user_workspace

        root = ensure_user_workspace(username)
        set_user_ns(username)
        (root / ".kiwiki").mkdir(parents=True, exist_ok=True)
        for name in ("notes", "projects", "decisions", ".kiwiki", "index.md", "AGENTS.md"):
            link = tmp_path / name
            target = root / name
            if not link.exists() and target.exists():
                link.symlink_to(target, target_is_directory=target.is_dir())
        return root

    return _make()


@pytest.fixture
def tmp_file(tmp_path: Path):
    """Erstellt eine temporaeare .md-Datei und gibt den relativen Pfad zurueck."""

    def _make(name: str, content: str = "---\ntitle: Test\ntype: note\n---\n\nBody") -> str:
        from app.tenancy import user_root

        file_path = user_root() / name
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        return name

    return _make
