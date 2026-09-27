"""Regressionstests fuer die Deployment-Konfiguration (Issue: Reproduzierbarkeit).

Drei Befunde, die dieselbe Ursache hatten: die Testumgebung war nicht eindeutig
definiert, also pruefte kein Gate das, was lokal laeuft.

* `readOnlyRootFilesystem: true` wurde nur als gesetztes Flag geprueft, nicht
  als Verhalten. Faellt der `/tmp`-Mount, schlaegt der Pod zur Laufzeit fehl.
* Die Python-Matrix und `requires-python` duerfen nicht auseinanderlaufen.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "charts/kiwiki"
WORKFLOW = ROOT / ".github/workflows/ci.yml"


def _only_deployment(manifest: str) -> dict:
    """Das Deployment-Dokument aus einem gerenderten Manifest herausgreifen."""
    for dokument in yaml.safe_load_all(manifest):
        if dokument and dokument.get("kind") == "Deployment":
            return dokument
    pytest.fail("kein Deployment im gerenderten Manifest gefunden")


def _rendered_deployment(**overrides) -> str:
    """Deployment-Manifest mit Default-Werten, damit required=true greift."""
    values = {
        "secretEnv": {
            "KIWIKI_USERS": "admin:testkey1234567890:admin",
            "KIWIKI_OAUTH_TOKEN_SECRET": "testoauthsecret1234567890",
        },
    }
    values.update(overrides)
    import subprocess
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as handle:
        yaml.safe_dump(values, handle)
        path = handle.name
    try:
        result = subprocess.run(
            ["helm", "template", "kiwiki", str(CHART), "-f", path],
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError:
        pytest.skip("helm ist nicht installiert")
    except subprocess.CalledProcessError as error:
        pytest.fail(f"helm template fehlgeschlagen: {error.stderr}")
    finally:
        Path(path).unlink(missing_ok=True)
    return result.stdout


# ── readOnlyRootFilesystem braucht ein beschreibbares /tmp ─────────────────

def test_read_only_rootfs_mountet_tmp():
    """readOnlyRootFilesystem: true macht das Root-Dateisystem unbeschreibbar.

    Ohne beschreibbares /tmp startet der Pod nicht sauber — die App schreibt
    dort temporaere Dateien. Der Chart mountet deshalb ein emptyDir; faellt das
    weg, schlaegt der Pod zur Laufzeit fehl statt beim Rendern.
    """
    manifest = _rendered_deployment()
    deployment = _only_deployment(manifest)

    assert deployment["spec"]["template"]["spec"]["containers"][0]["securityContext"][
        "readOnlyRootFilesystem"
    ] is True

    mounts = {m["name"]: m["mountPath"] for m in deployment["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]}
    assert "/tmp" in mounts.values(), (
        "readOnlyRootFilesystem ohne beschreibbares /tmp startet nicht sauber"
    )

    volumes = {v["name"]: v for v in deployment["spec"]["template"]["spec"]["volumes"]}
    tmp_volume_name = next(name for name, path in mounts.items() if path == "/tmp")
    assert "emptyDir" in volumes[tmp_volume_name], (
        "/tmp muss ein emptyDir sein — ein PVC waere hier der falsche Typ"
    )


def test_alle_security_kontexte_bleiben_zusammen():
    """Ein Regressionsschutz: alle vier Härtungen müssen gleichzeitig stehen."""
    deployment = _only_deployment(_rendered_deployment())
    pod = deployment["spec"]["template"]["spec"]
    container = pod["containers"][0]["securityContext"]

    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
    assert container["allowPrivilegeEscalation"] is False
    assert container["capabilities"]["drop"] == ["ALL"]
    assert pod["automountServiceAccountToken"] is False


def test_data_volume_ist_kein_tmp_mount():
    """Der Datenpfad und der Temp-Pfad duerfen nicht verwechselt sein."""
    deployment = _only_deployment(_rendered_deployment())
    spec = deployment["spec"]["template"]["spec"]
    mounts = {m["name"]: m["mountPath"] for m in spec["containers"][0]["volumeMounts"]}

    assert "/data" in mounts.values(), "die Wiki-Daten brauchen ein Volume"
    assert mounts.get("data") == "/data"
    assert mounts.get("tmp") == "/tmp"


# ── Python-Matrix und requires-python ──────────────────────────────────────

def _ci_python_versions() -> list[str]:
    text = WORKFLOW.read_text(encoding="utf-8")
    block = re.search(r"python-version:\s*\[([^\]]+)\]", text)
    assert block, "die CI-Liste der Python-Versionen wurde nicht gefunden"
    return re.findall(r'"([\d.]+)"', block.group(1))


def test_ci_matrix_enthaelt_die_referenzversion():
    assert "3.12" in _ci_python_versions(), (
        "3.12 ist die untere Grenze aus requires-python und muss getestet werden"
    )


def test_ci_matrix_testet_mindestens_zwei_versionen():
    assert len(_ci_python_versions()) >= 2, (
        "eine einzelne Version im CI ist kein Schutz gegen Interpreter-Abweichungen"
    )


def test_requires_python_und_ci_matrix_stimmen_ueberein():
    """Die Obergrenze in requires-python muss der CI-Matrix folgen.

    Die Grenze existiert genau deshalb, weil 3.14 nicht getestet wird. Sobald
    3.14 in der Matrix landet, muss die Grenze nach oben wandern — sonst
    behauptet das Projekt Unterstuetzung fuer eine Version, die kein Gate
    sieht. Und umgekehrt: testet die Matrix eine Version, die die Grenze
    ausschliesst, ist die Matrix das Problem.
    """
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'requires-python\s*=\s*"([^"]+)"', text)
    assert match, "requires-python fehlt"
    spec = match.group(1)
    assert ">=3.12" in spec

    versionen = _ci_python_versions()
    hoechste_ci = max(versionen, key=lambda v: tuple(int(p) for p in v.split(".")))

    obere = re.search(r"<(3\.\d+)", spec)
    assert obere, "requires-python braucht eine Obergrenze, solange die Matrix nicht alles abdeckt"
    ausgeschlossen = obere.group(1)

    # Die hoechste getestete Version muss gerade noch erlaubt sein.
    assert tuple(int(p) for p in hoechste_ci.split(".")) < tuple(
        int(p) for p in ausgeschlossen.split(".")
    ), (
        f"CI testet {hoechste_ci}, requires-python=<{ausgeschlossen} schliesst sie aus — "
        "die Grenze muss der Matrix folgen, nicht umgekehrt"
    )

    # Und die naechste Version darueber darf nicht getestet werden.
    nachfolger = f"{int(ausgeschlossen.split('.')[0])}.{int(ausgeschlossen.split('.')[1])}"
    assert nachfolger not in versionen, (
        f"CI testet {nachfolger}, requires-python schliesst sie aus"
    )


def test_coverage_gate_gilt_nur_auf_der_referenzversion():
    """Auf der Matrix darf ein Gate nicht stillschweigend wegfallen.

    Ein Gate, das nur auf einer Version laeuft, muss das im Workflow auch
    erkennbar machen — sonst sieht es so aus, als pruefe jede Version.
    """
    text = WORKFLOW.read_text(encoding="utf-8")
    pytest_stufe = re.search(r"coverage run -m pytest.*?pip-audit", text, re.S)
    assert pytest_stufe, "der pytest-Schritt wurde nicht gefunden"
    schritt = pytest_stufe.group(0)

    assert "matrix.python-version" in schritt, (
        "der pytest-Schritt muss zwischen den Versionen unterscheiden"
    )
    assert "fail-under" in schritt, "das Gate darf nicht verschwunden sein"
