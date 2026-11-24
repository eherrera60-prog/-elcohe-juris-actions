import asyncio
import base64
import hashlib
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

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
        "search_scj_decisions",
        "search_tc_decisions",
        "recover_official_pdf",
        "extract_decision_passages",
        "propose_ratio_decidendi",
        "register_verified_decision",
        "get_verified_decisions",
        "get_verified_decision_document",
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
    assert set(tool_map["search_scj_decisions"]["inputSchema"]["properties"]) == {
        "q",
        "chamber",
        "matter",
        "year_from",
        "year_to",
        "limit",
    }
    assert set(tool_map["recover_official_pdf"]["inputSchema"]["properties"]) == {
        "url",
        "expected_court",
        "expected_number",
    }
    assert set(
        tool_map["extract_decision_passages"]["inputSchema"]["properties"]
    ) == {
        "url",
        "expected_court",
        "expected_number",
        "legal_issue",
        "limit",
    }
    assert set(
        tool_map["propose_ratio_decidendi"]["inputSchema"]["properties"]
    ) == {
        "url",
        "expected_court",
        "expected_number",
        "legal_issue",
        "limit",
    }
    assert set(tool_map["get_verified_decisions"]["inputSchema"]["properties"]) == {
        "q",
        "court",
        "limit",
    }
    assert set(
        tool_map["get_verified_decision_document"]["inputSchema"]["properties"]
    ) == {"decision_id"}
    assert set(tool_map["verify_official_document"]["inputSchema"]["properties"]) == {
        "url",
        "expected_court",
        "expected_number",
    }
    assert set(
        tool_map["register_verified_decision"]["inputSchema"]["properties"]
    ) == {
        "url",
        "expected_court",
        "expected_number",
        "legal_issue",
        "max_passages",
    }
    number_schema = tool_map["register_verified_decision"]["inputSchema"][
        "properties"
    ]["expected_number"]
    rest_number_schema = app.openapi()["components"]["schemas"][
        "VerifyRequest"
    ]["properties"]["expected_number"]["anyOf"][0]
    assert number_schema["minLength"] == 1
    assert number_schema["maxLength"] == 80
    assert "\\d{1,5}" in number_schema["pattern"]
    assert {
        key: number_schema[key] for key in ("minLength", "maxLength", "pattern")
    } == {
        key: rest_number_schema[key]
        for key in ("minLength", "maxLength", "pattern")
    }
    for tool_name in (
        "recover_official_pdf",
        "extract_decision_passages",
        "propose_ratio_decidendi",
    ):
        assert tool_map[tool_name]["inputSchema"]["properties"][
            "expected_number"
        ] == number_schema
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


def test_mcp_returns_controlled_error_for_invalid_decision_number(client):
    with patch(
        "mcp_server.auth_access.require_active_user",
        new=AsyncMock(return_value={"sub": "test", "role": "user"}),
    ):
        response = client.post(
            "/mcp",
            headers={
                "Authorization": "Bearer test-token",
                "Accept": "application/json, text/event-stream",
            },
            json={
                "jsonrpc": "2.0",
                "id": 22,
                "method": "tools/call",
                "params": {
                    "name": "verify_official_document",
                    "arguments": {
                        "url": "https://example.test/decision.pdf",
                        "expected_court": "SCJ",
                        "expected_number": "sentencia desconocida",
                    },
                },
            },
        )

    assert response.status_code == 200
    result = response.json()["result"]
    assert result["isError"] is True
    assert "expected_number" in result["content"][0]["text"]


def _call_mcp(client, name, arguments, request_id):
    response = client.post(
        "/mcp",
        headers={
            "Authorization": "Bearer test-token",
            "Accept": "application/json, text/event-stream",
        },
        json={
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["isError"] is False
    return json.loads(result["content"][0]["text"])


def test_mcp_executes_scj_search_recovery_extraction_and_ratio_proposal(client):
    import httpx

    from app import app

    pdf_text = """SUPREMA CORTE DE JUSTICIA
PRIMERA SALA DE LA SUPREMA CORTE DE JUSTICIA
SENTENCIA SCJ-TS-24-00123
En fecha 4 de marzo de 2024.
LA SALA, DESPUÉS DE HABER DELIBERADO, CONSIDERA QUE:
1) Esta Suprema Corte establece que la interpretación debe atender el caso concreto. En consecuencia, rechaza el recurso de casación.
FALLA:
ÚNICO: RECHAZA el recurso de casación.
"""
    pdf_bytes = b"%PDF-1.7 verified MCP fixture"
    pdf_doc = {
        "final_url": (
            "https://consultasentenciascj.poderjudicial.gob.do/"
            "documentos/SCJ-TS-24-00123.pdf"
        ),
        "host": "consultasentenciascj.poderjudicial.gob.do",
        "content_type": "application/pdf",
        "data": pdf_bytes,
        "sha256": hashlib.sha256(pdf_bytes).hexdigest(),
    }
    html_doc = {
        **pdf_doc,
        "final_url": "https://consultasentenciascj.poderjudicial.gob.do/ficha/123",
        "content_type": "text/html",
        "data": b"<html>fixture</html>",
        "text_response": (
            '<html><a href="/documentos/SCJ-TS-24-00123.pdf">'
            "Sentencia SCJ-TS-24-00123</a></html>"
        ),
    }
    extracted = {
        "document_type": "PDF",
        "pages": [{"page": 1, "text": pdf_text}],
        "full_text": pdf_text,
        "page_count": 1,
        "extraction_ok": True,
    }
    portal_html = b"""<html><select id="cbSala"></select>
      <select id="cbMateria"></select><select id="cbAno"></select></html>"""
    search_json = (
        b'{"recordsFiltered":1,"data":[{"noSentencia":"SCJ-TS-24-00123",'
        b'"urlBlob":"https://consultasentenciascj.poderjudicial.gob.do/ficha/123"}]}'
    )

    class FakeStreamResponse:
        status_code = 200

        def __init__(self, content, content_type):
            self.content = content
            self.headers = {"content-type": content_type}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def aiter_bytes(self):
            yield self.content

        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def stream(self, method, url, **kwargs):
            if method == "GET":
                return FakeStreamResponse(portal_html, "text/html")
            return FakeStreamResponse(search_json, "application/json")

    request = {
        "url": html_doc["final_url"],
        "expected_court": "SCJ",
        "expected_number": "SCJ-TS-24-00123",
    }
    with (
        patch(
            "mcp_server.auth_access.require_active_user",
            new=AsyncMock(return_value={"sub": "test", "role": "user"}),
        ),
        patch("app.validate_official_url"),
        patch.object(httpx, "AsyncClient", FakeClient),
    ):
        search = _call_mcp(
            client, "search_scj_decisions", {"q": "responsabilidad civil"}, 30
        )

    assert search["search_executed"] is True
    assert search["status"] == "CANDIDATES_FOUND"
    assert search["results"][0]["source_status"] == "NOT_VERIFIED"
    assert search["results"][0]["decision_number"] == "SCJ-TS-24-00123"

    with (
        patch(
            "mcp_server.auth_access.require_active_user",
            new=AsyncMock(return_value={"sub": "test", "role": "user"}),
        ),
        patch(
            "app.fetch_document",
            new=AsyncMock(side_effect=[html_doc, pdf_doc]),
        ),
        patch("app.extract_document_text", return_value=extracted),
    ):
        recovered = _call_mcp(client, "recover_official_pdf", request, 31)

    assert recovered["candidate_located"] is True
    assert recovered["document_recovered"] is True
    assert recovered["official_source_verified"] is True
    assert recovered["identity_verified"] is True
    assert recovered["document"]["status"] == "PDF_TEXT_VERIFIED"

    with (
        patch(
            "mcp_server.auth_access.require_active_user",
            new=AsyncMock(return_value={"sub": "test", "role": "user"}),
        ),
        patch("app.fetch_document", new=AsyncMock(return_value=pdf_doc)),
        patch("app.extract_document_text", return_value=extracted),
    ):
        passages = _call_mcp(
            client, "extract_decision_passages", request, 32
        )
        proposal = _call_mcp(
            client,
            "propose_ratio_decidendi",
            {**request, "legal_issue": "interpretación del caso concreto"},
            33,
        )

    assert passages["identity_status"] == "VERIFIED_IDENTITY"
    assert passages["passage_status"] == "VERIFIED_PASSAGE"
    assert passages["deciding_court"] == "Primera Sala"
    assert passages["foundations"][0]["page"] == 1
    assert passages["foundations"][0]["context_pages"] == [1]
    assert passages["dispositive"]["page"] == 1
    assert proposal["status"] == "PROVISIONAL_RATIO_CANDIDATE"
    assert proposal["ratio_decidendi_verified"] is False
    assert proposal["review_required"] is True
    assert "Esta Suprema Corte establece" in proposal[
        "proposed_ratio_decidendi"
    ]["formulation"]
    assert proposal["proposed_ratio_decidendi"]["dispositive_relationship"] == (
        "SHARED_TEXT_WITH_VERIFIED_DISPOSITIVE"
    )


def test_mcp_ratio_proposal_rejects_wrong_identity_and_mixed_sources(client):
    from app import app

    pdf_bytes = b"%PDF-1.7 identity fixture"
    document = {
        "final_url": (
            "https://consultasentenciascj.poderjudicial.gob.do/"
            "documentos/SCJ-TS-24-00123.pdf"
        ),
        "host": "consultasentenciascj.poderjudicial.gob.do",
        "content_type": "application/pdf",
        "data": pdf_bytes,
        "sha256": hashlib.sha256(pdf_bytes).hexdigest(),
    }
    request = {
        "url": document["final_url"],
        "expected_court": "SCJ",
        "expected_number": "SCJ-TS-24-00123",
    }
    texts = (
        "1) En defensa de la sentencia atacada el recurrido sostiene "
        "que la alzada valoró las pruebas.",
        "1) Esta Primera Sala cita como precedente la sentencia TC/0099/22.",
    )
    with patch(
        "mcp_server.auth_access.require_active_user",
        new=AsyncMock(return_value={"sub": "test", "role": "user"}),
    ):
        for index, text in enumerate(texts, start=40):
            extracted = {
                "document_type": "PDF",
                "pages": [{
                    "page": 1,
                    "text": (
                        "SUPREMA CORTE DE JUSTICIA\n"
                        "PRIMERA SALA DE LA SUPREMA CORTE DE JUSTICIA\n"
                        "SENTENCIA SCJ-TS-24-00123\n"
                        "LA SALA, DESPUÉS DE HABER DELIBERADO, CONSIDERA QUE:\n"
                        f"{text}\n"
                        "FALLA:\nÚNICO: RECHAZA EL RECURSO.\n"
                    ),
                }],
                "full_text": (
                    "SUPREMA CORTE DE JUSTICIA\n"
                    "PRIMERA SALA DE LA SUPREMA CORTE DE JUSTICIA\n"
                    "SENTENCIA SCJ-TS-24-00123\n"
                    "LA SALA, DESPUÉS DE HABER DELIBERADO, CONSIDERA QUE:\n"
                    f"{text}\n"
                    "FALLA:\nÚNICO: RECHAZA EL RECURSO.\n"
                ),
                "page_count": 1,
                "extraction_ok": True,
            }
            with (
                patch("app.fetch_document", new=AsyncMock(return_value=document)),
                patch("app.extract_document_text", return_value=extracted),
            ):
                result = _call_mcp(
                    client,
                    "propose_ratio_decidendi",
                    {**request, "legal_issue": "recurso"},
                    index,
                )
            assert result["status"] == "NO_ATTRIBUTABLE_PASSAGE_CANDIDATE"
            assert result["ratio_candidate_located"] is False
            assert result["ratio_decidendi_verified"] is False
            assert result["review_required"] is True

        with (
            patch("app.fetch_document", new=AsyncMock(return_value=document)),
            patch(
                "app.extract_document_text",
                return_value={
                    "document_type": "PDF",
                    "pages": [{"page": 1, "text": "SUPREMA CORTE DE JUSTICIA"}],
                    "full_text": "SUPREMA CORTE DE JUSTICIA",
                    "page_count": 1,
                    "extraction_ok": True,
                },
            ),
        ):
            mismatch = _call_mcp(
                client,
                "propose_ratio_decidendi",
                {
                    **request,
                    "expected_number": "SCJ-TS-24-00999",
                    "legal_issue": "recurso",
                },
                42,
            )
    assert mismatch["status"] == "PROPOSAL_UNAVAILABLE_IDENTITY_UNVERIFIED"
    assert mismatch["identity_status"] == "NOT_VERIFIED"
    assert mismatch["proposed_ratio_decidendi"] is None


def test_mcp_ratio_proposal_requires_issue_relevance_and_decisional_link():
    from mcp_server import _rank_ratio_candidates

    evidence = {
        "identity_status": "VERIFIED_IDENTITY",
        "source_status": "VERIFIED_SOURCE",
        "passage_status": "VERIFIED_PASSAGE",
        "document_recovered": True,
        "deciding_court": "Tribunal Constitucional",
        "metadata": {"decision_number": "TC/0019/24"},
        "dispositive": {
            "status": "VERIFIED_PASSAGE",
            "text": "DECIDE: ACOGER el recurso y ANULAR la sentencia recurrida.",
        },
        "foundations": [
            {
                "status": "VERIFIED_PASSAGE",
                "attribution": "POSSIBLE_JUDICIAL_REASONING_REVIEW",
                "reasoning_origin": "UNDETERMINED",
                "origin_classification_indicators": ["court_self_reference"],
                "foundation_number": "8",
                "tribunal": "Tribunal Constitucional",
                "decision_number": "TC/0019/24",
                "page": 18,
                "passage": (
                    "8. Competencia. Este tribunal es competente para conocer "
                    "del recurso de revisión constitucional de decisión "
                    "jurisdiccional conforme a la Constitución y la ley."
                ),
            },
            {
                "status": "VERIFIED_PASSAGE",
                "attribution": "PARTY_ARGUMENT_OR_MIXED_REVIEW",
                "reasoning_origin": "PARTY_ARGUMENT",
                "origin_classification_indicators": [],
                "foundation_number": "10",
                "tribunal": "Tribunal Constitucional",
                "decision_number": "TC/0019/24",
                "page": 20,
                "passage": (
                    "El recurrente alega que la reforma de la pena en su "
                    "perjuicio vulneró sus garantías procesales."
                ),
            },
            {
                "status": "VERIFIED_PASSAGE",
                "attribution": "DECIDING_COURT_SECTION_REVIEW",
                "reasoning_origin": "UNDETERMINED",
                "origin_classification_indicators": [
                    "tc_own_considerations_section",
                    "requires_individual_review",
                ],
                "attribution_verified": False,
                "review_required": True,
                "foundation_number": "12",
                "tribunal": "Tribunal Constitucional",
                "decision_number": "TC/0019/24",
                "page": 23,
                "passage": (
                    "El Tribunal Constitucional considera que la prohibición "
                    "de reforma en perjuicio del imputado debe aplicarse en "
                    "el proceso penal. En consecuencia, acoge el recurso y "
                    "anula la sentencia impugnada por esa razón."
                ),
            },
        ],
    }
    candidates = _rank_ratio_candidates(
        evidence, "prohibición de reforma en perjuicio del imputado"
    )

    assert [item["foundation_number"] for item in candidates] == ["12"]
    assert candidates[0]["issue_relevance"] == 1.0
    assert candidates[0]["dispositive_relationship"] == (
        "DECISIONAL_LINK_REQUIRES_HUMAN_REVIEW"
    )
    assert candidates[0]["attribution_verified"] is False
    assert candidates[0]["review_required"] is True


def test_mcp_registration_uses_existing_tc_registry_and_requires_admin(client):
    request = {
        "url": (
            "https://tribunalsitestorage.blob.core.windows.net/"
            "media/tc-0019-24.pdf"
        ),
        "expected_court": "TC",
        "expected_number": "TC/0019/24",
        "legal_issue": "reforma en perjuicio",
    }
    headers = {
        "Authorization": "Bearer test-token",
        "Accept": "application/json, text/event-stream",
    }
    with patch(
        "mcp_server.auth_access.require_active_user",
        new=AsyncMock(return_value={"sub": "test-user", "role": "user"}),
    ):
        denied = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 46,
                "method": "tools/call",
                "params": {
                    "name": "register_verified_decision",
                    "arguments": request,
                },
            },
        )
    assert denied.json()["result"]["isError"] is True

    from datetime import datetime, timezone

    document_bytes = b"%PDF-1.7 authorized TC fixture"
    digest = hashlib.sha256(document_bytes).hexdigest()
    connection = MagicMock()
    connection.__enter__.return_value = connection
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.fetchone.return_value = (19, datetime.now(timezone.utc))
    connection.cursor.return_value = cursor
    document = {
        "final_url": request["url"],
        "host": "tribunalsitestorage.blob.core.windows.net",
        "content_type": "application/pdf",
        "data": document_bytes,
        "sha256": digest,
    }
    extracted = {
        "document_type": "PDF",
        "pages": [{
            "page": 1,
            "text": (
                "TRIBUNAL CONSTITUCIONAL SENTENCIA TC/0019/24 "
                + "texto judicial verificado. " * 8
            ),
        }],
        "full_text": (
            "TRIBUNAL CONSTITUCIONAL SENTENCIA TC/0019/24 "
            + "texto judicial verificado. " * 8
        ),
        "page_count": 1,
        "extraction_ok": True,
    }
    with (
        patch(
            "mcp_server.auth_access.require_active_user",
            new=AsyncMock(return_value={"sub": "test-admin", "role": "admin"}),
        ),
        patch("registry.connect", return_value=connection),
        patch("registry.init_schema"),
        patch("app.fetch_document", new=AsyncMock(return_value=document)),
        patch("app.extract_document_text", return_value=extracted),
        patch("app.detect_court", return_value="TC"),
        patch("app.court_matches", return_value=True),
        patch("app.decision_number_matches", return_value=True),
        patch(
            "app.extract_metadata_from_text",
            return_value={"decision_number": "TC/0019/24"},
        ),
    ):
        stored = _call_mcp(client, "register_verified_decision", request, 47)

    assert stored["status"] == "STORED_VERIFIED_SOURCE"
    assert stored["id"] == 19
    assert stored["sha256_pdf"] == digest
    decision_insert = next(
        call.args[0]
        for call in cursor.execute.call_args_list
        if "INSERT INTO elcohe_decisions" in call.args[0]
    )
    assert "ON CONFLICT (court,decision_number,sha256) DO NOTHING" in decision_insert
    document_insert = next(
        call.args[0]
        for call in cursor.execute.call_args_list
        if "INSERT INTO elcohe_decision_documents" in call.args[0]
    )
    assert "ON CONFLICT (decision_id) DO UPDATE" in document_insert


def test_mcp_verified_search_distinguishes_empty_database_from_unavailable(client):
    args = {"q": "TC/0019/24", "court": "TC", "limit": 5}
    fields = [
        "id", "court", "decision_number", "source_url", "sha256", "chamber",
        "decision_date", "case_number", "legal_issue", "verification_status",
        "review_required", "ratio_decidendi_verified", "metadata",
        "extraction_diagnostics", "created_at",
    ]
    connection = MagicMock()
    connection.__enter__.return_value = connection
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.description = [SimpleNamespace(name=name) for name in fields]
    cursor.fetchall.return_value = []
    connection.cursor.return_value = cursor
    with (
        patch(
            "mcp_server.auth_access.require_active_user",
            new=AsyncMock(return_value={"sub": "test", "role": "user"}),
        ),
        patch("registry.connect", return_value=connection),
        patch("registry.init_schema"),
    ):
        empty = _call_mcp(client, "get_verified_decisions", args, 48)
    assert empty["status"] == "NO_VERIFIED_DOCUMENTS"
    assert empty["count"] == 0

    with (
        patch(
            "mcp_server.auth_access.require_active_user",
            new=AsyncMock(return_value={"sub": "test", "role": "user"}),
        ),
        patch("registry.connect", side_effect=RuntimeError("postgres unavailable")),
        patch("registry.init_schema"),
    ):
        unavailable = client.post(
            "/mcp",
            headers={
                "Authorization": "Bearer test-token",
                "Accept": "application/json, text/event-stream",
            },
            json={
                "jsonrpc": "2.0",
                "id": 49,
                "method": "tools/call",
                "params": {
                    "name": "get_verified_decisions",
                    "arguments": args,
                },
            },
        ).json()["result"]
    assert unavailable["isError"] is True
    assert "NO_VERIFIED_DOCUMENTS" not in unavailable["content"][0]["text"]


def test_real_tc_0019_24_mcp_proposal_abstains_on_allegations_and_competence(
    client,
):
    with patch(
        "mcp_server.auth_access.require_active_user",
        new=AsyncMock(return_value={"sub": "real-tc-test", "role": "user"}),
    ):
        result = _call_mcp(
            client,
            "propose_ratio_decidendi",
            {
                "url": (
                    "https://tribunalsitestorage.blob.core.windows.net/"
                    "media/53968/tc-0019-24-tc-04-2023-0274.pdf"
                ),
                "expected_court": "TC",
                "expected_number": "TC/0019/24",
                "legal_issue": (
                    "prohibición de reforma en perjuicio del imputado"
                ),
                "limit": 30,
            },
            501,
        )

    assert result["identity_status"] == "VERIFIED_IDENTITY"
    assert result["source_status"] == "VERIFIED_SOURCE"
    assert result["status"] == "NO_ATTRIBUTABLE_PASSAGE_CANDIDATE"
    assert result["proposed_ratio_decidendi"] is None
    assert result["supporting_passages"] == []
    assert result["ratio_decidendi_verified"] is False
    assert result["review_required"] is True


def test_real_scj_january_2020_number_36_mcp_extraction(client):
    with patch(
        "mcp_server.auth_access.require_active_user",
        new=AsyncMock(return_value={"sub": "real-scj-test", "role": "user"}),
    ):
        result = _call_mcp(
            client,
            "extract_decision_passages",
            {
                "url": (
                    "https://transparencia.poderjudicial.gob.do/"
                    "documentos/pdf/BoletinJudicialIndividual/131020036.pdf"
                ),
                "expected_court": "SCJ",
                "expected_number": "36",
                "legal_issue": "falta de base legal",
                "limit": 30,
            },
            502,
        )

    assert result["identity_status"] == "VERIFIED_IDENTITY"
    assert result["source_status"] == "VERIFIED_SOURCE"
    assert result["metadata"]["decision_number"] == "NÚM. 36"
    assert result["metadata"]["date"] == "29 de enero de 2020"
    assert result["metadata"]["chamber"] == "Primera Sala"
    assert result["dispositive_status"] == "VERIFIED_PASSAGE"
    assert "FALLA:" in result["dispositive"]["text"]
    assert result["ratio_decidendi_verified"] is False


def test_mcp_recovers_persisted_pdf_after_hash_and_identity_checks(client):
    from app import app

    data = b"%PDF-1.7 persisted MCP bytes"
    digest = hashlib.sha256(data).hexdigest()
    fields = [
        "id", "court", "decision_number", "source_url", "sha256",
        "verification_status", "review_required", "ratio_decidendi_verified",
        "metadata", "extraction_diagnostics", "content_type", "page_count",
        "document_bytes",
    ]
    row = (
        19, "TC", "TC/0019/24",
        "https://tribunalsitestorage.blob.core.windows.net/media/tc-0019-24.pdf",
        digest, "VERIFIED_SOURCE", True, False, {}, {},
        "application/pdf", 2, data,
    )
    connection = MagicMock()
    connection.__enter__.return_value = connection
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.description = [SimpleNamespace(name=name) for name in fields]
    cursor.fetchone.return_value = row
    connection.cursor.return_value = cursor

    with (
        patch(
            "mcp_server.auth_access.require_active_user",
            new=AsyncMock(return_value={"sub": "test", "role": "user"}),
        ),
        patch("registry.connect", return_value=connection),
        patch("registry.init_schema"),
        patch(
            "app.extract_document_text",
            return_value={
                "extraction_ok": True,
                "page_count": 2,
                "full_text": "TRIBUNAL CONSTITUCIONAL SENTENCIA TC/0019/24",
            },
        ),
        patch("app.detect_court", return_value="TC"),
        patch("app.court_matches", return_value=True),
        patch("app.decision_number_matches", return_value=True),
        patch("app.allowed_host", return_value=True),
    ):
        recovered = _call_mcp(
            client,
            "get_verified_decision_document",
            {"decision_id": 19},
            43,
        )

    assert recovered["status"] == "DOCUMENT_RECOVERED_FROM_POSTGRESQL"
    assert recovered["retrieval_mode"] == "POSTGRESQL_PERSISTED_PDF"
    assert recovered["search_executed"] is False
    assert recovered["document_recovered"] is True
    assert recovered["official_source_verified"] is True
    assert recovered["identity_status"] == "VERIFIED_IDENTITY"
    assert base64.b64decode(recovered["document_bytes_base64"]) == data


def test_mcp_searches_verified_decisions_using_registry_signature(client):
    from datetime import datetime, timezone

    fields = [
        "id", "court", "decision_number", "source_url", "sha256", "chamber",
        "decision_date", "case_number", "legal_issue", "verification_status",
        "review_required", "ratio_decidendi_verified", "metadata",
        "extraction_diagnostics", "created_at",
    ]
    row = (
        19, "SCJ", "SCJ-PS-20-00036",
        "https://transparencia.poderjudicial.gob.do/decision.pdf",
        "a" * 64, "Primera Sala", "2020-01-29", None, "seguro",
        "VERIFIED_SOURCE", True, False, {"document_type": "PDF"}, {},
        datetime.now(timezone.utc),
    )
    connection = MagicMock()
    connection.__enter__.return_value = connection
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.description = [SimpleNamespace(name=name) for name in fields]
    cursor.fetchall.return_value = [row]
    connection.cursor.return_value = cursor

    with (
        patch(
            "mcp_server.auth_access.require_active_user",
            new=AsyncMock(return_value={"sub": "test", "role": "user"}),
        ),
        patch("registry.connect", return_value=connection),
        patch("registry.init_schema"),
    ):
        result = _call_mcp(
            client,
            "get_verified_decisions",
            {"q": "SCJ-PS-20-00036", "court": "SCJ", "limit": 5},
            44,
        )

    assert result["status"] == "VERIFIED_DOCUMENTS_FOUND"
    assert result["count"] == 1
    assert result["items"][0]["decision_number"] == "SCJ-PS-20-00036"
    assert cursor.execute.call_args.args[1] == [
        "SCJ",
        "%SCJ-PS-20-00036%",
        "%SCJ-PS-20-00036%",
        "%SCJ-PS-20-00036%",
        5,
    ]


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
