"""CHANGELOG.md is English only (rule in CONTRIBUTING.md, "Changelog and Release Notes").

German UI strings may appear as quoted interface text ("…", „…", `…`); everything
else must be English. The check strips quoted spans and inline code, then looks
for umlauts and common German function words.
"""

from __future__ import annotations

import re
from pathlib import Path

CHANGELOG = Path(__file__).resolve().parents[1] / "CHANGELOG.md"

_QUOTED = re.compile(r"`[^`]*`|\"[^\"\n]*\"|„[^“\"\n]*[“\"]|'[^'\n]*'")
_GERMAN = re.compile(
    r"[äöüÄÖÜß]|\b(und|der|die|das|nicht|jetzt|wird|werden|wurde|für|fuer|mit|auf|bei|ist|sind|noch|auch|statt)\b",
    re.IGNORECASE,
)


def test_changelog_is_english_only():
    offending = []
    for number, line in enumerate(CHANGELOG.read_text(encoding="utf-8").splitlines(), start=1):
        if line.startswith("[") and "]: http" in line:
            continue
        prose = _QUOTED.sub("", line)
        match = _GERMAN.search(prose)
        if match:
            offending.append(f"{number}: {match.group(0)!r} in {line.strip()[:100]}")
    assert not offending, "German text in CHANGELOG.md:\n" + "\n".join(offending[:20])


def test_changelog_declares_the_language_rule():
    assert "This changelog is written in English only." in CHANGELOG.read_text(encoding="utf-8")
