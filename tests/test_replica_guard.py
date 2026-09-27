"""Regressionstests fuer die Replika-Absicherung (Issue #24, Luecke 3).

Der Key-Zaehler liegt bei KIWIKI_RATE_LIMIT_STORE=memory im Prozessspeicher.
Bei mehreren Replikas zaehlt jeder Pod fuer sich — die Drosselung wird dann um
den Faktor der Replikas schwaecher statt staerker, ohne eine einzige Logzeile.

Absicherung in zwei Schranken:
1. `values.schema.json` begrenzt `replicaCount` auf 1.
2. `kiwiki.assertSharedRateLimitStore` bricht auch dann ab, wenn jemand die
   Schema-Schranke umgeht (z. B. mit `--skip-schema-validation`).

Die Tests laufen ohne Helm, indem sie die Bedingungen nachbilden, die der
Helper in `app/rate_limit_store.py` und im Chart pruefen. Der Helper selbst
wird zusaetzlich per Text geprueft.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import rate_limit_store as store_mod
from app.rate_limit_store import assert_shared_store_for_replicas

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "charts/kiwiki"


# ── Schranke 1: das Schema begrenzt die Replika-Zahl ───────────────────────

def test_schema_begrenzt_replica_count_auf_eins():
    schema = json.loads((CHART / "values.schema.json").read_text(encoding="utf-8"))
    replica = schema["properties"]["replicaCount"]

    assert replica["maximum"] == 1
    assert "prozesslokal" in replica["description"] or "process-local" in replica["description"], (
        "die Begruendung gehoert zum Schema, sonst wirkt die Schranke willkuerlich"
    )


def test_schema_kennt_den_geteilten_speicher():
    schema = json.loads((CHART / "values.schema.json").read_text(encoding="utf-8"))
    env = schema["properties"]["env"]["properties"]

    assert env["KIWIKI_RATE_LIMIT_STORE"]["enum"] == ["memory", "sqlite"]


# ── Schranke 2: der Template-Helper ───────────────────────────────────────

def test_helper_prueft_replika_und_store():
    helpers = (CHART / "templates/_helpers.tpl").read_text(encoding="utf-8")

    assert "kiwiki.assertSharedRateLimitStore" in helpers
    assert "KIWIKI_RATE_LIMIT_STORE" in helpers
    assert "ReadWriteMany" in helpers, "mehrere Replikas brauchen ein RWX-Volume fuer die SQLite-Datei"


def test_deployment_ruft_den_helper_auf():
    """Ohne den Aufruf in der Deployment-Vorlage waere der Helper toter Code."""
    deployment = (CHART / "templates/deployment.yaml").read_text(encoding="utf-8")

    assert 'include "kiwiki.assertSharedRateLimitStore"' in deployment


# ── Die Bedingung, die der Helper durchsetzt ──────────────────────────────

def test_eine_replika_ist_erlaubt():
    assert_shared_store_for_replicas(1)


def test_mehrere_replikas_ohne_sqlite_werden_abgewiesen():
    with pytest.raises(ValueError) as error:
        assert_shared_store_for_replicas(4)

    message = str(error.value)
    assert "shared rate-limit store" in message
    assert "weaker, not stronger" in message, (
        "die Meldung muss die Konsequenz erklaeren, sonst wird sie als Formalitaet missachtet"
    )


def test_sqlite_gilt_als_geteilt(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod, "_store", store_mod.SqliteAttemptStore(tmp_path / "rl.sqlite", window=60))

    assert_shared_store_for_replicas(4)
