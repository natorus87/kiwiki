"""Der Lint-Regelsatz ist festgeschrieben, nicht dem Default überlassen.

ruff 0.16 hat den Default-Regelsatz von 59 auf 413 Regeln erweitert. Ohne
`[tool.ruff.lint] select` hätte ein reines Werkzeug-Update 197 Befunde
ausgelöst, von denen 51 `BLE001` waren — also „blind except", genau die
Fälle, die dieses Projekt in den Audit-Pfaden bewusst mit Logging versehen
hat.

Der Test hält fest, *dass* ein Regelsatz benannt ist. Welche Regeln drin
sind, ist eine Projektentscheidung und wird hier nicht festgenagelt —
sonst wäre jede bewusste Erweiterung ein Testbruch.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _lint_config() -> dict:
    with open(ROOT / "pyproject.toml", "rb") as handle:
        return tomllib.load(handle)["tool"]["ruff"]["lint"]


def test_regelsatz_ist_explizit_benannt():
    """Ohne `select` folgt ruff dem Default — und der Default hat sich geändert.

    Genau das ist der Vorfall: 0.15 meldete mit demselben Aufruf nichts,
    0.16 meldete 197 Befunde, ohne dass sich am Code etwas geändert hatte.
    """
    config = _lint_config()

    assert "select" in config, (
        "pyproject.toml benennt keinen Regelensatz. ruff folgt dann seinem "
        "Default, und der hat sich zwischen 0.15 (59 Regeln) und 0.16 "
        "(413 Regeln) geaendert — ein Werkzeug-Update waere dann ein "
        "Stilumbau von 197 Stellen."
    )
    assert config["select"], "select ist leer, damit ist es dasselbe wie kein select"


def test_ruff_version_ist_gepinnt_und_geprueft():
    """Ein Werkzeug-Sprung darf keine ungeprueften Befunde ausloesen.

    Das passiert gerade in PR #30: der Bump auf 0.16.8 macht die Suite rot,
    ohne dass ein Produktionscode fehlerhaft waere.
    """
    dev_requirements = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
    match = re.search(r"^ruff==(\S+)$", dev_requirements, re.M)

    assert match, "ruff ist nicht gepinnt"
    assert "==" in match.group(0), "ruff muss exakt gepinnt sein"


def test_zeilenlaenge_wird_durch_ruff_steuerbar_geregelt():
    """E501 ist ignoriert, damit `line-length` die Grenze setzt.

    Beides gleichzeitig zu tun wäre redundant; E501 allein zu ignorieren
    hieße, gar keine Grenze zu haben. Der Test haelt die Begruendung fest,
    damit das Ignorieren nicht zum blinden Aus-Schalten wird.
    """
    config = _lint_config()

    assert "E501" in config.get("ignore", []), (
        "E501 muss ignoriert bleiben — ruff wuerde sonst die Zeilenlaenge "
        "zusaetzlich zur line-length-Pruefung melden"
    )

    with open(ROOT / "pyproject.toml", encoding="utf-8") as handle:
        text = handle.read()
    assert "line-length = 120" in text, "die Grenze muss irgendwo gesetzt sein"
