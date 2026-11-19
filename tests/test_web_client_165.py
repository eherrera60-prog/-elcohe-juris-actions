
import os
from urllib.parse import parse_qs, urlparse
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from app import app
import web_client_165
import portal_validation_165
from auth_access import AUTH0_AUDIENCE, require_active_user

client = TestClient(
    app, base_url="https://testserver"
)


def test_root_redirects_to_public_portal():
    response = client.get("/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/portal"


def test_web_portal_public():
    r = client.get("/portal")
    assert r.status_code == 200
    assert "Iniciar sesión" in r.text


def test_portal_validation_get_route_is_registered_and_requires_a_session():
    with patch(
        "portal_validation_165.active_session",
        new=AsyncMock(return_value={"csrf": "test-csrf"}),
    ):
        response = client.get("/portal/validation")

    assert response.status_code == 200
    assert "Validación funcional" in response.text
    assert "value='test-csrf'" in response.text


def test_portal_validation_executes_authorized_database_check():
    session = {"csrf": "test-csrf", "token": "test-token"}
    app.dependency_overrides[require_active_user] = lambda: {
        "sub": "test-admin",
        "role": "admin",
    }
    try:
        with patch(
            "portal_validation_165.active_session",
            new=AsyncMock(return_value=session),
        ), patch(
            "auth_access.check_database_connection",
            return_value={"status": "DATABASE_CONNECTED"},
        ):
            response = client.post(
                "/portal/validation",
                data={"csrf": "test-csrf", "operation": "database"},
            )
    finally:
        app.dependency_overrides.pop(require_active_user, None)

    assert response.status_code == 200
    assert "HTTP 200" in response.text
    assert "DATABASE_CONNECTED" in response.text


def test_portal_validation_rejects_invalid_csrf():
    with patch(
        "portal_validation_165.active_session",
        new=AsyncMock(return_value={"csrf": "expected-csrf"}),
    ):
        response = client.post(
            "/portal/validation",
            data={"csrf": "wrong-csrf", "operation": "database"},
        )

    assert response.status_code == 403


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


def test_oauth_login_uses_render_service_url_for_callback():
    with patch.dict(os.environ, {
        "RENDER_EXTERNAL_URL": "https://elcohe-juris-dev.onrender.com",
    }), patch(
        "web_client_165.configuration",
        return_value=("test-client", "test-secret", AUTH0_AUDIENCE),
    ):
        response = client.get("/auth/login", follow_redirects=False)

    flow_id = response.cookies.get(web_client_165.FLOW_COOKIE)
    if flow_id:
        web_client_165.FLOWS.pop(flow_id, None)

    assert response.status_code == 303
    params = parse_qs(urlparse(response.headers["location"]).query)
    assert params["redirect_uri"] == [
        "https://elcohe-juris-dev.onrender.com/auth/callback"
    ]


def test_oauth_callback_rejects_invalid_render_service_url():
    with patch.dict(os.environ, {
        "RENDER_EXTERNAL_URL": "http://user:password@example.com/path",
    }):
        with pytest.raises(HTTPException) as error:
            web_client_165._callback_url()

    assert error.value.status_code == 503
