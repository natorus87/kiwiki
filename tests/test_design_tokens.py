"""Haelt die Typo- und Abstandsskala geschlossen.

Vor dieser Runde trugen die Stylesheets 37 verschiedene Schriftgroessen und
58 Abstandswerte — 71 % davon lagen zwischen den Stufen, weil rem-Brueche wie
`0.45rem` krumme Pixel ergeben. Ohne diesen Test waechst das sofort wieder:
ein einzelnes `font-size: 0.82rem` faellt beim Review nicht auf, hundert davon
sind ein kaputtes System.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STYLESHEETS = [
    ROOT / "app/static/kiwiki.css",
    ROOT / "app/static/kiwiki-polish.css",
    ROOT / "app/static/knowledge-graph.css",
]
TEMPLATES = sorted((ROOT / "app/templates").rglob("*.html"))

# 16px auf Eingabefeldern ist der iOS-Schwellenwert, unterhalb dessen Safari
# beim Fokussieren hineinzoomt — ein funktionaler Wert, keine Typo-Rolle.
ALLOWED_RAW_FONT_SIZES = {"16px"}
SPACING_PROPS = (
    "padding", "margin", "gap", "row-gap", "column-gap",
    "padding-block", "padding-inline", "margin-block", "margin-inline",
    "padding-top", "padding-bottom", "padding-left", "padding-right",
    "margin-top", "margin-bottom", "margin-left", "margin-right",
)


def _sources():
    for path in STYLESHEETS + TEMPLATES:
        yield path, path.read_text(encoding="utf-8")


def _offenders(pattern, extract):
    found = []
    for path, text in _sources():
        for number, line in enumerate(text.split("\n"), 1):
            if "--text-" in line and ":root" not in line and line.strip().startswith("--"):
                continue
            for match in pattern.finditer(line):
                bad = extract(match)
                if bad:
                    found.append(f"{path.relative_to(ROOT)}:{number}  {bad}")
    return found


def test_keine_rohen_schriftgroessen():
    pattern = re.compile(r"font-size:\s*([^;}\n]+)")

    def extract(match):
        value = match.group(1).strip()
        if value in ALLOWED_RAW_FONT_SIZES or "var(--text-" in value or value == "inherit":
            return None
        return f"font-size: {value}"

    offenders = [o for o in _offenders(pattern, extract) if "--text-" not in o]
    assert not offenders, "Schriftgroessen ausserhalb der Rollen:\n" + "\n".join(offenders)


def test_keine_rohen_abstaende():
    pattern = re.compile(
        r"\b(" + "|".join(SPACING_PROPS) + r")\s*:\s*([^;}\n]+)"
    )

    def extract(match):
        prop, value = match.group(1), match.group(2).strip()
        if any(token in value for token in ("var(--space", "calc", "env(", "auto", "inherit")):
            return None
        for part in value.split():
            if re.fullmatch(r"-?(\d+(\.\d+)?)(rem|em|px)", part) and not part.startswith("-"):
                if part not in ("0px", "0rem"):
                    return f"{prop}: {value}"
        return None

    offenders = [o for o in _offenders(pattern, extract) if "--space" not in o]
    assert not offenders, "Abstaende ausserhalb der Skala:\n" + "\n".join(offenders)


def test_nur_vier_schriftgewichte():
    """400/500/600/700. Werte wie 620 oder 650 waehlt niemand bewusst."""
    allowed = {"400", "500", "600", "700", "bold", "normal", "inherit"}
    offenders = []
    for path, text in _sources():
        for number, line in enumerate(text.split("\n"), 1):
            for match in re.finditer(r"font-weight:\s*([^;}\n]+)", line):
                value = match.group(1).strip()
                if value not in allowed and "var(" not in value:
                    offenders.append(f"{path.relative_to(ROOT)}:{number}  font-weight: {value}")
            for match in re.finditer(r"\bfont:\s*(\d{3})\b", line):
                if match.group(1) not in allowed:
                    offenders.append(f"{path.relative_to(ROOT)}:{number}  font: {match.group(1)}")
    assert not offenders, "Schriftgewichte ausserhalb der Rollen:\n" + "\n".join(offenders)


@pytest.mark.parametrize(
    "token",
    ["--text-xs", "--text-sm", "--text-md", "--text-lg", "--text-xl",
     "--text-2xl", "--text-3xl", "--text-display",
     "--space-0-5", "--space-1", "--space-2", "--space-3", "--space-4",
     "--space-6", "--space-8", "--space-12", "--space-16"],
)
def test_jede_rolle_ist_definiert(token):
    """Die Rollen liegen in kiwiki-polish.css, nicht in kiwiki.css.

    login.html laedt kiwiki.css nicht — es bringt seine Farben selbst mit und
    bindet nur das Polish-Stylesheet ein. Liegen die Tokens in kiwiki.css,
    faellt die Loginseite auf Browser-Defaults zurueck.
    """
    polish = (ROOT / "app/static/kiwiki-polish.css").read_text(encoding="utf-8")
    assert re.search(rf"^\s*{re.escape(token)}:\s*\S", polish, re.M), f"{token} fehlt in :root"


def test_jede_seite_laedt_das_stylesheet_mit_den_rollen():
    for name in ("layout.html", "login.html"):
        markup = (ROOT / "app/templates" / name).read_text(encoding="utf-8")
        assert "kiwiki-polish.css" in markup, f"{name} laedt die Design-Rollen nicht"
