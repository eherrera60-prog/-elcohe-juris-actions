import asyncio
import os
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
import pytest
from starlette.requests import Request

with patch.dict(
    os.environ,
    {
        "AUTH0_DOMAIN": "example.auth0.com",
        "RENDER_EXTERNAL_URL": "",
    },
):
    from app import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app, base_url="http://localhost") as test_client:
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
    tool_map = {
        tool["name"]: tool
        for tool in tools.json()["result"]["tools"]
    }
    assert set(tool_map) == {
        "health_check",
        "prepare_scj_search",
        "search_tc_decisions",
        "verify_official_document",
    }
    assert set(tool_map["health_check"]["inputSchema"]["properties"]) == set()
    assert set(tool_map["search_tc_decisions"]["inputSchema"]["properties"]) == {
        "q",
        "year",
        "limit",
    }
    assert set(tool_map["prepare_scj_search"]["inputSchema"]["properties"]) == {
        "q",
        "chamber",
        "matter",
        "year_from",
        "year_to",
    }
    assert set(tool_map["verify_official_document"]["inputSchema"]["properties"]) == {
        "url",
        "expected_court",
        "expected_number",
    }
    assert tool_map["search_tc_decisions"]["inputSchema"]["required"] == ["q"]
    assert tool_map["prepare_scj_search"]["inputSchema"]["required"] == ["q"]
    assert tool_map["verify_official_document"]["inputSchema"]["required"] == ["url"]
    assert health.status_code == 200


def test_mcp_executes_health_prepare_search_and_document_verification(client):
    headers = {
        "Authorization": "Bearer test-token",
        "Accept": "application/json, text/event-stream",
    }

    search_result = {
        "query": "TC/0019/24",
        "source": "https://example.invalid/",
        "search_executed": True,
        "results": [],
        "status": "NO_RESULTS_CONFIRMED",
        "message": "Fixture controlado sin inventar jurisprudencia.",
    }
    document = {
        "final_url": "https://tribunalsitestorage.blob.core.windows.net/media/tc-0019-24.pdf",
        "host": "tribunalsitestorage.blob.core.windows.net",
        "content_type": "application/pdf",
        "data": b"%PDF fixture",
        "sha256": "0" * 64,
    }
    extracted = {
        "document_type": "PDF",
        "page_count": 1,
        "full_text": "TRIBUNAL CONSTITUCIONAL SENTENCIA TC/0019/24",
        "extraction_ok": True,
    }

    with (
        patch(
            "mcp_server.auth_access.require_active_user",
            new=AsyncMock(return_value={"sub": "test", "role": "user"}),
        ),
        patch("registry.find_verified_tc_references", return_value=[]),
        patch("app._search_tc_candidates", new=AsyncMock(return_value=search_result)),
        patch("app.fetch_document", new=AsyncMock(return_value=document)),
        patch("app.extract_document_text", return_value=extracted),
        patch("app.detect_court", return_value="TC"),
        patch("app.extract_metadata_from_text", return_value={"decision_number": "TC/0019/24"}),
        patch("app.decision_number_matches", return_value=True),
        patch("app.court_matches", return_value=True),
    ):
        health = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 10,
                "method": "tools/call",
                "params": {"name": "health_check", "arguments": {}},
            },
        )
        search = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 11,
                "method": "tools/call",
                "params": {
                    "name": "search_tc_decisions",
                    "arguments": {"q": "TC/0019/24", "limit": 3},
                },
            },
        )
        prepare = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 12,
                "method": "tools/call",
                "params": {
                    "name": "prepare_scj_search",
                    "arguments": {"q": "contrato", "year_from": 2020, "year_to": 2024},
                },
            },
        )
        verify = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 13,
                "method": "tools/call",
                "params": {
                    "name": "verify_official_document",
                    "arguments": {
                        "url": document["final_url"],
                        "expected_court": "TC",
                        "expected_number": "TC/0019/24",
                    },
                },
            },
        )

    assert health.status_code == 200
    assert health.json()["result"]["content"][0]["text"]
    assert search.status_code == 200
    assert '"TC/0019/24"' in search.json()["result"]["content"][0]["text"]
    assert prepare.status_code == 200
    assert '"SEARCH_PLAN_ONLY"' in prepare.json()["result"]["content"][0]["text"]
    assert verify.status_code == 200
    assert '"VERIFIED_SOURCE"' in verify.json()["result"]["content"][0]["text"]


def test_mcp_rejects_missing_arguments_and_unknown_tool(client):
    headers = {
        "Authorization": "Bearer test-token",
        "Accept": "application/json, text/event-stream",
    }
    with patch(
        "mcp_server.auth_access.require_active_user",
        new=AsyncMock(return_value={"sub": "test", "role": "user"}),
    ):
        missing = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 20,
                "method": "tools/call",
                "params": {"name": "search_tc_decisions", "arguments": {}},
            },
        )
        unknown = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 21,
                "method": "tools/call",
                "params": {"name": "herramienta_inexistente", "arguments": {}},
            },
        )

    assert missing.status_code == 200
    assert missing.json()["result"]["isError"] is True
    assert "validation error" in missing.json()["result"]["content"][0]["text"].lower()
    assert unknown.status_code == 200
    assert unknown.json()["result"]["isError"] is True


def test_mcp_uses_test_resource_audience_without_changing_rest_audience():
    from auth_access import AUTH0_AUDIENCE, REQUIRED_PERMISSION, require_active_user

    def request_for(path):
        return Request(
            {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "POST" if path == "/mcp" else "GET",
                "scheme": "https",
                "path": path,
                "raw_path": path.encode(),
                "query_string": b"",
                "headers": [],
                "server": ("elcohe-juris-163-test.onrender.com", 443),
                "client": ("127.0.0.1", 12345),
            }
        )

    with (
        patch.dict(
            os.environ,
            {"RENDER_EXTERNAL_URL": "https://elcohe-juris-163-test.onrender.com"},
        ),
        patch(
            "auth_access.decode_access_token",
            return_value={
                "sub": "auth0|active-user",
                "permissions": [REQUIRED_PERMISSION],
            },
        ) as decode,
        patch(
            "auth_access.get_authorized_user",
            return_value={"subject": "auth0|active-user", "status": "ACTIVE"},
        ),
    ):
        asyncio.run(require_active_user(request_for("/mcp"), token="test-token"))
        asyncio.run(
            require_active_user(
                request_for("/v1/tc/search"),
                token="test-token",
            )
        )

    assert decode.call_args_list[0].kwargs["audience"] == (
        "https://elcohe-juris-163-test.onrender.com/mcp"
    )
    assert decode.call_args_list[1].kwargs["audience"] == AUTH0_AUDIENCE
