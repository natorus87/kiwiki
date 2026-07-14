import json
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


def test_docker_image_defines_healthcheck():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "HEALTHCHECK" in dockerfile
    assert "/livez" in dockerfile


def test_release_version_is_consistent():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    chart = yaml.safe_load((ROOT / "charts/kiwiki/Chart.yaml").read_text(encoding="utf-8"))
    values = yaml.safe_load((ROOT / "charts/kiwiki/values.yaml").read_text(encoding="utf-8"))

    assert 'version = "3.0.0"' in pyproject
    assert chart["version"] == "3.0.0"
    assert chart["appVersion"] == "3.0.0"
    assert values["image"]["tag"] == "3.0.0"


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
