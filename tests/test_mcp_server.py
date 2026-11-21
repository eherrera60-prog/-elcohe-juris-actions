import os
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
import pytest

with patch.dict(
    os.environ,
    {
        "AUTH0_DOMAIN": "example.auth0.com",
        "RENDER_EXTERNAL_URL": "https://elcohe-juris-rd.onrender.com",
    },
):
    from app import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app, base_url="https://elcohe-juris-rd.onrender.com") as test_client:
        yield test_client


def test_mcp_rejects_unauthorized_requests_and_advertises_oauth_metadata(client):
    with patch.dict(
        os.environ,
        {"RENDER_EXTERNAL_URL": "https://elcohe-juris-rd.onrender.com"},
    ):
        denied = client.post(
            "/mcp",
            headers={"Host": "attacker.example"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        with patch(
            "mcp_server.auth_access.require_active_user",
            new=AsyncMock(return_value={"sub": "test", "role": "user"}),
        ):
            rejected_host = client.post(
                "/mcp",
                headers={
                    "Authorization": "Bearer test-token",
                    "Host": "attacker.example",
                },
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            )
        with patch.dict(os.environ, {"AUTH0_DOMAIN": "example.auth0.com"}):
            metadata = client.get("/.well-known/oauth-protected-resource/mcp")
    assert denied.status_code == 401
    assert "resource_metadata=" in denied.headers["www-authenticate"]
    assert "https://elcohe-juris-rd.onrender.com/.well-known/" in denied.headers[
        "www-authenticate"
    ]
    assert rejected_host.status_code == 421
    assert metadata.status_code == 200
    assert metadata.json() == {
        "resource": "https://elcohe-juris-rd.onrender.com/mcp",
        "authorization_servers": ["https://example.auth0.com/"],
        "scopes_supported": ["access:jurisprudence"],
        "bearer_methods_supported": ["header"],
    }


def test_mcp_initializes_over_streamable_http_and_discovers_tools(client):
    headers = {
        "Authorization": "Bearer test-token",
        "Accept": "application/json, text/event-stream",
    }

    with patch(
        "mcp_server.auth_access.require_active_user",
        new=AsyncMock(return_value={"sub": "test", "role": "user"}),
    ):
        initialized = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 0,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "mcp-smoke", "version": "1"},
                },
            },
        )
        tools = client.post(
            "/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        health = client.get("/health")

    assert initialized.status_code == 200
    assert initialized.json()["result"]["protocolVersion"] == "2025-03-26"
    assert tools.status_code == 200
    assert {tool["name"] for tool in tools.json()["result"]["tools"]} == {
        "health_check",
        "prepare_scj_search",
        "search_tc_decisions",
        "verify_official_document",
    }
    assert health.status_code == 200
