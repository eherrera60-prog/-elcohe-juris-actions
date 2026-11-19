
import os
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from app import app
import web_client_165
from auth_access import AUTH0_AUDIENCE

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
