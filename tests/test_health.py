from fastapi.testclient import TestClient


def test_application_reports_release_version():
    from app.main import app

    assert app.version == "3.0.0"


def test_livez_is_public_and_only_reports_process_liveness(monkeypatch):
    monkeypatch.delenv("KIWIKI_USERS", raising=False)
    from app.main import app

    with TestClient(app) as client:
        response = client.get("/livez")

    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


def test_security_headers_only_allow_self_hosted_assets(monkeypatch):
    monkeypatch.delenv("KIWIKI_USERS", raising=False)
    from app.main import app

    with TestClient(app) as client:
        response = client.get("/livez")

    csp = response.headers["content-security-policy"]
    assert "https://" not in csp
    assert "default-src 'self'" in csp
    assert response.headers["x-robots-tag"] == "noindex, nofollow"
    assert response.headers["permissions-policy"] == "camera=(), microphone=(), geolocation=()"


def test_requests_receive_a_safe_correlation_id(monkeypatch):
    monkeypatch.delenv("KIWIKI_USERS", raising=False)
    from app.main import app

    with TestClient(app) as client:
        generated = client.get("/livez", headers={"X-Request-ID": "invalid id with spaces"})
        forwarded = client.get("/livez", headers={"X-Request-ID": "client-request_42"})

    assert len(generated.headers["x-request-id"]) == 32
    assert generated.headers["x-request-id"].isalnum()
    assert forwarded.headers["x-request-id"] == "client-request_42"


def test_readyz_checks_user_config_data_dir_and_sqlite(monkeypatch):
    monkeypatch.setenv("KIWIKI_USERS", "admin:admin-key:admin")
    from app.main import app

    with TestClient(app) as client:
        response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert response.json()["version"] == "3.0.0"


def test_readyz_fails_without_valid_user_configuration(monkeypatch):
    monkeypatch.delenv("KIWIKI_USERS", raising=False)
    from app.main import app

    with TestClient(app) as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["status"] == "not ready"


def test_readyz_fails_when_sqlite_is_unusable(monkeypatch):
    monkeypatch.setenv("KIWIKI_USERS", "admin:admin-key:admin")
    import app.main as main_mod

    with TestClient(main_mod.app) as client:
        monkeypatch.setattr(main_mod, "init_db", lambda: (_ for _ in ()).throw(OSError("db down")))
        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["status"] == "not ready"
