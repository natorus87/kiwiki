import json
import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_compose_requires_external_admin_secret_and_binds_loopback():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    service = compose["services"]["kiwiki"]

    assert service["ports"] == ["127.0.0.1:8082:8080"]
    assert service["environment"]["KIWIKI_USERS"] == "${KIWIKI_USERS:?KIWIKI_USERS must be set}"
    assert service["environment"]["KIWIKI_OAUTH_TOKEN_SECRET"] == (
        "${KIWIKI_OAUTH_TOKEN_SECRET:?KIWIKI_OAUTH_TOKEN_SECRET must be set}"
    )
    assert "healthcheck" in service


def test_helm_requires_secret_and_uses_existing_claim():
    secret = (ROOT / "charts/kiwiki/templates/secret.yaml").read_text(encoding="utf-8")
    deployment = (ROOT / "charts/kiwiki/templates/deployment.yaml").read_text(encoding="utf-8")
    values = yaml.safe_load((ROOT / "charts/kiwiki/values.yaml").read_text(encoding="utf-8"))

    assert "required" in secret
    assert "existingSecret" in secret
    assert "persistence.existingClaim" in deployment
    assert values["secretEnv"]["KIWIKI_USERS"] == ""
    assert values["livenessProbe"]["httpGet"]["path"] == "/livez"
    assert values["readinessProbe"]["httpGet"]["path"] == "/readyz"


def test_helm_defaults_to_hardened_single_replica_runtime():
    values = yaml.safe_load((ROOT / "charts/kiwiki/values.yaml").read_text(encoding="utf-8"))
    deployment = (ROOT / "charts/kiwiki/templates/deployment.yaml").read_text(encoding="utf-8")
    schema = json.loads((ROOT / "charts/kiwiki/values.schema.json").read_text(encoding="utf-8"))

    assert values["replicaCount"] == 1
    assert schema["properties"]["replicaCount"]["maximum"] == 1
    assert values["containerSecurityContext"]["readOnlyRootFilesystem"] is True
    assert values["podSecurityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
    assert "automountServiceAccountToken: false" in deployment


def test_knowledge_engine_is_opt_in_for_compose_and_helm():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    values = yaml.safe_load((ROOT / "charts/kiwiki/values.yaml").read_text(encoding="utf-8"))
    schema = json.loads((ROOT / "charts/kiwiki/values.schema.json").read_text(encoding="utf-8"))

    compose_env = compose["services"]["kiwiki"]["environment"]
    assert compose_env["KIWIKI_KNOWLEDGE_ENABLED"] == "${KIWIKI_KNOWLEDGE_ENABLED:-false}"
    assert values["env"]["KIWIKI_KNOWLEDGE_ENABLED"] == "false"
    assert schema["properties"]["env"]["properties"]["KIWIKI_KNOWLEDGE_ENABLED"]["enum"] == [
        "true",
        "false",
    ]


def test_docker_image_defines_healthcheck():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "HEALTHCHECK" in dockerfile
    assert "/livez" in dockerfile


def test_release_version_is_consistent():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    chart = yaml.safe_load((ROOT / "charts/kiwiki/Chart.yaml").read_text(encoding="utf-8"))
    values = yaml.safe_load((ROOT / "charts/kiwiki/values.yaml").read_text(encoding="utf-8"))

    assert 'version = "4.1.0"' in pyproject
    assert chart["version"] == "4.1.0"
    assert chart["appVersion"] == "4.1.0"
    assert values["image"]["tag"] == "4.1.0"


def test_runtime_dependencies_are_exactly_pinned():
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    dependencies = [line for line in requirements if line and not line.startswith("#")]

    assert dependencies
    assert all("==" in dependency for dependency in dependencies)


def test_ci_enforces_coverage_and_dependency_audits():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "coverage run" in workflow
    assert "--fail-under" in workflow
    assert "pip-audit" in workflow
    assert "npm audit" in workflow


def test_coverage_gate_is_identical_in_pyproject_and_ci():
    """Die Schwelle steht zweimal; ein Drift laesst eines der beiden Gates still greifen."""
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    match = re.search(r"fail_under\s*=\s*(\d+)", pyproject)
    assert match, "pyproject.toml setzt keine Coverage-Schwelle"
    threshold = match.group(1)

    assert f"--fail-under={threshold}" in workflow, (
        f"pyproject.toml verlangt {threshold} %, der CI-Schritt verlangt eine andere Schwelle"
    )


def test_shipped_version_matches_the_app_constant():
    """APP_VERSION ist die ausgelieferte Version (FastAPI, /version, MCP serverInfo)."""
    from app.constants import APP_VERSION

    assert APP_VERSION == "4.1.0"


def _configured_env_keys() -> set[str]:
    """Alle KIWIKI_*-Variablen, die der Code tatsächlich liest."""
    keys: set[str] = set()
    pattern = re.compile(r'(?:os\.environ\.get|os\.getenv)\(\s*"(KIWIKI_\w+)"')
    for path in (ROOT / "app").rglob("*.py"):
        keys |= set(pattern.findall(path.read_text(encoding="utf-8")))
    return keys


def test_every_env_var_read_by_the_code_is_documented_in_the_readme():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    documented = set(re.findall(r"\|\s*`(KIWIKI_\w+)`", readme))

    undocumented = _configured_env_keys() - documented
    assert not undocumented, f"im Code gelesen, aber nicht in der README-Tabelle: {sorted(undocumented)}"


def test_env_example_only_contains_real_env_vars():
    """Kommentierte Beispiele duerfen Keys nennen, unbekannte duerfen nicht."""
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    mentioned = set(re.findall(r"KIWIKI_\w+", example))

    unknown = mentioned - _configured_env_keys()
    assert not unknown, f"in .env.example, aber im Code nicht vorhanden: {sorted(unknown)}"


def test_helm_env_exposes_the_ui_and_session_limits():
    values = yaml.safe_load((ROOT / "charts/kiwiki/values.yaml").read_text(encoding="utf-8"))
    env = values["env"]

    assert env["KIWIKI_UI_LIMIT"] == "240"
    assert env["KIWIKI_SESSION_TTL_SECONDS"] == "2592000"


def test_agents_md_marks_local_paths_as_local():
    """AGENTS.md ist versioniert, .claude/ nicht — der Unterschied muss dort stehen.

    Die Existenz von .claude/ darf *nicht* geprueft werden: das Verzeichnis ist
    per .gitignore ausgeschlossen und fehlt in jedem frischen Klon und in CI.
    """
    agents_md = (ROOT / "AGENTS.md").read_text(encoding="utf-8")

    assert ".Codex/" not in agents_md, "AGENTS.md verweist auf ein Verzeichnis, das es nicht gibt"
    assert ".gitignore" in agents_md, "AGENTS.md muss sagen, dass .claude/ nicht versioniert ist"
