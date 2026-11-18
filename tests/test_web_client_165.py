
from fastapi.testclient import TestClient
from app import app

client = TestClient(
    app, base_url="https://testserver"
)


def test_web_portal_public():
    r = client.get("/portal")
    assert r.status_code == 200
    assert "Iniciar sesión" in r.text


def test_callback_without_state():
    r = client.get("/auth/callback")
    assert r.status_code == 400


def test_jurisprudence_remains_protected():
    r = client.get("/v1/registry/decisions")
    assert r.status_code == 401


def test_health_remains_public():
    assert client.get("/health").status_code == 200


def test_public_routes_not_in_openapi():
    paths = app.openapi()["paths"]
    assert "/auth/login" not in paths
    assert "/auth/callback" not in paths
    assert "/portal" not in paths
