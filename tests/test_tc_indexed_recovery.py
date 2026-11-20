import asyncio
import hashlib
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import app
from auth_access import require_active_user
from registry import TCReferenceRequest, register_tc_reference
from tc_indexed_recovery import (
    _indexed_candidates,
    _search_expression,
    search_tc_index,
)


def test_indexed_results_are_limited_to_tc_sources_and_exact_number():
    payload = {
        "web": {
            "results": [
                {
                    "url": "https://tribunalsitestorage.blob.core.windows.net/media/tc-0019-24.pdf",
                    "title": "Sentencia del Tribunal Constitucional",
                    "description": "Tribunal Constitucional",
                },
                {
                    "url": "https://example.test/TC-0019-24.pdf",
                    "title": "Sentencia TC/0019/24",
                    "description": "Tribunal Constitucional",
                },
                {
                    "url": "https://www.tribunalconstitucional.gob.do/decisiones/tc0020-24",
                    "title": "Sentencia TC/0020/24",
                    "description": "Tribunal Constitucional",
                },
            ]
        }
    }

    results = _indexed_candidates(payload, "TC/19/24", None, 10)

    assert len(results) == 1
    assert results[0]["decision_number"] == "TC/0019/24"
    assert results[0]["retrieval_mode"] == "INDEXED_OFFICIAL_SEARCH"
    assert results[0]["source_status"] == "NOT_VERIFIED"
    assert results[0]["ratio_decidendi_verified"] is False


def test_index_query_scopes_domains_and_cannot_accept_user_site_operator():
    expression = _search_expression("debido proceso OR site:example.test")

    assert "site:tribunalconstitucional.gob.do" in expression
    assert "site:tribunalsitestorage.blob.core.windows.net" in expression
    assert "site:example.test" not in expression


def test_index_search_is_disabled_by_default_even_with_api_key(monkeypatch):
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "unused-token")
    monkeypatch.delenv("BRAVE_SEARCH_ENABLED", raising=False)

    with patch(
        "tc_indexed_recovery.httpx.AsyncClient",
        side_effect=AssertionError("Brave debe permanecer desactivado"),
    ):
        result = asyncio.run(search_tc_index("debido proceso"))

    assert result["status"] == "INDEX_SEARCH_DISABLED"
    assert result["search_executed"] is False
    assert result["results"] == []


def test_enabled_index_search_without_api_key_does_not_claim_zero_results(monkeypatch):
    monkeypatch.setenv("BRAVE_SEARCH_ENABLED", "true")
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)

    result = asyncio.run(search_tc_index("debido proceso"))

    assert result["status"] == "INDEX_SEARCH_NOT_CONFIGURED"
    assert result["search_executed"] is False
    assert result["results"] == []


def test_index_search_uses_authorized_api_and_accepts_official_result(monkeypatch):
    monkeypatch.setenv("BRAVE_SEARCH_ENABLED", "true")
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "test-token")
    request = {}

    class FakeResponse:
        status_code = 200
        headers = {}
        content = json.dumps({
            "web": {
                "results": [{
                    "url": "https://www.tribunalconstitucional.gob.do/decisiones/tc001924",
                    "title": "Sentencia TC/0019/24",
                    "description": "Tribunal Constitucional, debido proceso",
                }]
            }
        }).encode()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def aiter_bytes(self):
            yield self.content

    class FakeClient:
        def __init__(self, **kwargs):
            request["client_options"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def stream(self, method, url, params):
            assert method == "GET"
            request["url"] = url
            request["params"] = params
            return FakeResponse()

    with patch("tc_indexed_recovery.httpx.AsyncClient", FakeClient):
        result = asyncio.run(search_tc_index("debido proceso"))

    assert request["url"] == "https://api.search.brave.com/res/v1/web/search"
    assert "site:tribunalconstitucional.gob.do" in request["params"]["q"]
    assert "site:tribunalsitestorage.blob.core.windows.net" in request["params"]["q"]
    assert request["client_options"]["headers"]["X-Subscription-Token"] == "test-token"
    assert result["status"] == "CANDIDATES_FOUND"
    assert result["results"][0]["decision_number"] == "TC/0019/24"


@pytest.mark.parametrize("query", ["debido proceso", "TC/0019/24"])
def test_tc_search_separates_index_candidates_from_previously_verified_documents(
    query, monkeypatch
):
    monkeypatch.setenv("BRAVE_SEARCH_ENABLED", "true")
    index_result = {
        "status": "CANDIDATES_FOUND",
        "provider": "BRAVE_SEARCH_INDEX",
        "search_executed": True,
        "scoped_domains": [
            "tribunalconstitucional.gob.do",
            "tribunalsitestorage.blob.core.windows.net",
        ],
        "results": [{
            "court": "TC",
            "decision_number": "TC/0019/24",
            "source_url": "https://tribunalsitestorage.blob.core.windows.net/media/tc-0019-24.pdf",
            "retrieval_mode": "INDEXED_OFFICIAL_SEARCH",
            "source_status": "NOT_VERIFIED",
        }],
        "message": "Candidatos del índice.",
    }
    verified = [{
        "court": "TC",
        "decision_number": "TC/0019/24",
        "retrieval_mode": "PREVIOUSLY_VERIFIED_DOCUMENT",
        "official_document_verified": True,
        "ratio_decidendi_verified": False,
    }]
    app.dependency_overrides[require_active_user] = lambda: {"sub": "tester"}
    try:
        with patch(
            "app._search_tc_candidates",
            new=AsyncMock(return_value={
                "status": "ACCESS_FAILED",
                "search_executed": True,
                "http_status": 406,
                "results": [],
                "message": "La búsqueda oficial respondió HTTP 406.",
            }),
        ) as official_search, patch(
            "tc_indexed_recovery.search_tc_index",
            new=AsyncMock(return_value=index_result),
        ) as indexed_search, patch(
            "registry.find_verified_tc_references",
            return_value=verified,
        ) as stored_search:
            response = TestClient(app).get("/v1/tc/search", params={"q": query})
    finally:
        app.dependency_overrides.pop(require_active_user, None)

    assert response.status_code == 200
    body = response.json()
    assert body["search_modes"]["real_time_official"]["status"] == "ACCESS_FAILED"
    assert body["search_modes"]["real_time_official"]["http_status"] == 406
    assert body["search_modes"]["indexed_search"]["status"] == "CANDIDATES_FOUND"
    assert body["search_modes"]["previously_verified_documents"]["result_count"] == 1
    assert body["results"][0]["retrieval_mode"] == "INDEXED_OFFICIAL_SEARCH"
    assert body["verified_documents"][0]["retrieval_mode"] == "PREVIOUSLY_VERIFIED_DOCUMENT"
    assert body["verified_documents"][0]["ratio_decidendi_verified"] is False
    official_search.assert_awaited_once_with(
        query, None, 10, primary_only=True
    )
    indexed_search.assert_awaited_once_with(query, None, 10)
    stored_search.assert_called_once_with(query, 10)


def test_tc_search_reports_brave_disabled_without_calling_external_provider(monkeypatch):
    monkeypatch.delenv("BRAVE_SEARCH_ENABLED", raising=False)
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "unused-token")
    app.dependency_overrides[require_active_user] = lambda: {"sub": "tester"}
    try:
        with patch(
            "app._search_tc_candidates",
            new=AsyncMock(return_value={
                "status": "ACCESS_FAILED",
                "search_executed": True,
                "http_status": 406,
                "results": [],
                "message": "El portal oficial rechazó la consulta.",
            }),
        ), patch(
            "tc_indexed_recovery.httpx.AsyncClient",
            side_effect=AssertionError("No debe consultarse Brave"),
        ), patch("registry.find_verified_tc_references", return_value=[]):
            response = TestClient(app).get(
                "/v1/tc/search", params={"q": "debido proceso"}
            )
    finally:
        app.dependency_overrides.pop(require_active_user, None)

    assert response.status_code == 200
    body = response.json()
    assert body["search_modes"]["indexed_search"]["status"] == "INDEX_SEARCH_DISABLED"
    assert body["search_modes"]["indexed_search"]["provider"] == "BRAVE_SEARCH_INDEX"
    assert body["search_modes"]["real_time_official"]["status"] == "ACCESS_FAILED"
    assert body["search_executed"] is True
    assert body["results"] == []


def test_tc_search_does_not_call_index_when_official_search_finds_candidates():
    official = {
        "status": "CANDIDATES_FOUND",
        "search_executed": True,
        "results": [{
            "decision_number": "TC/0019/24",
            "source_url": "https://tribunalsitestorage.blob.core.windows.net/media/tc-0019-24.pdf",
            "retrieval_mode": "REAL_TIME_OFFICIAL_SEARCH",
        }],
        "message": "Fuente oficial.",
    }
    app.dependency_overrides[require_active_user] = lambda: {"sub": "tester"}
    try:
        with patch(
            "app._search_tc_candidates",
            new=AsyncMock(return_value=official),
        ), patch(
            "tc_indexed_recovery.search_tc_index",
            new=AsyncMock(side_effect=AssertionError("índice no debe consultarse")),
        ) as indexed_search, patch(
            "registry.find_verified_tc_references",
            return_value=[],
        ):
            response = TestClient(app).get(
                "/v1/tc/search", params={"q": "TC/0019/24"}
            )
    finally:
        app.dependency_overrides.pop(require_active_user, None)

    assert response.status_code == 200
    body = response.json()
    assert body["results"][0]["retrieval_mode"] == "REAL_TIME_OFFICIAL_SEARCH"
    assert body["search_modes"]["indexed_search"]["status"] == "NOT_RUN"
    indexed_search.assert_not_awaited()


def test_tc_primary_official_search_does_not_repeat_rejected_alternative_routes():
    from app import _search_tc_candidates

    attempted = []
    client_options = {}

    class FakeResponse:
        status_code = 406

    class FakeClient:
        def __init__(self, **kwargs):
            client_options.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, params):
            attempted.append(str(url))
            return FakeResponse()

    with patch("app.httpx.AsyncClient", FakeClient), patch(
        "app.fetch_document", new=AsyncMock()
    ) as fetch:
        result = asyncio.run(
            _search_tc_candidates(
                "debido proceso", None, 5, primary_only=True
            )
        )

    assert result["status"] == "ACCESS_FAILED"
    assert result["http_status"] == 406
    assert len(attempted) == 1
    assert fetch.await_count == 0
    assert client_options["follow_redirects"] is False


def test_tc_reference_registration_remains_admin_only_and_hidden_from_openapi():
    response = TestClient(app).post(
        "/v1/registry/tc-references",
        json={
            "url": "https://tribunalsitestorage.blob.core.windows.net/media/tc-0019-24.pdf",
            "expected_number": "TC/0019/24",
        },
    )

    assert response.status_code == 401
    assert "/v1/registry/tc-references" not in app.openapi()["paths"]


def _verified_tc_pdf(number="TC/0019/24"):
    data = b"%PDF-1.7 verified test fixture"
    digest = hashlib.sha256(data).hexdigest()
    text = (
        "TRIBUNAL CONSTITUCIONAL\n"
        f"SENTENCIA {number}\n"
        "El Tribunal Constitucional decide el asunto conforme a la Constitución. "
        "Este texto de prueba permite verificar la extracción documental."
    )
    document = {
        "final_url": (
            "https://tribunalsitestorage.blob.core.windows.net/media/tc-0019-24.pdf"
        ),
        "host": "tribunalsitestorage.blob.core.windows.net",
        "content_type": "application/pdf",
        "data": data,
        "sha256": digest,
    }
    extracted = {
        "document_type": "PDF",
        "pages": [{"page": 1, "text": text}, {"page": 2, "text": "Página final."}],
        "page_count": 2,
        "full_text": text,
        "extraction_ok": True,
    }
    return document, extracted


def test_verified_tc_reference_persists_integrity_identity_text_and_pages_without_ratio():
    now = datetime.now(timezone.utc)
    document, extracted = _verified_tc_pdf()
    connection = MagicMock()
    connection.__enter__.return_value = connection
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.fetchone.return_value = (12, now)
    connection.cursor.return_value = cursor
    request = TCReferenceRequest(
        url=document["final_url"],
        expected_number="TC/0019/24",
        legal_issue="debido proceso",
    )

    with (
        patch("registry.connect", return_value=connection),
        patch("registry.init_schema"),
        patch("app.fetch_document", new=AsyncMock(return_value=document)),
        patch("app.extract_document_text", return_value=extracted),
    ):
        result = asyncio.run(register_tc_reference(request))

    insert = cursor.execute.call_args.args[0]
    assert "ON CONFLICT (court,decision_number,sha256) DO NOTHING" in insert
    assert "'VERIFIED_SOURCE',TRUE,FALSE" in insert
    assert result["status"] == "STORED_VERIFIED_SOURCE"
    assert result["document_type"] == "PDF"
    assert result["page_count"] == 2
    assert result["text_extraction_ok"] is True
    assert result["sha256_pdf"] == document["sha256"]
    assert result["ratio_decidendi_verified"] is False
    assert result["review_required"] is True
    metadata = cursor.execute.call_args.args[1][-2]
    assert metadata.obj["document_type"] == "PDF"
    assert metadata.obj["source_sha256"] == document["sha256"]


def test_duplicate_verified_tc_reference_is_not_inserted_twice():
    now = datetime.now(timezone.utc)
    document, extracted = _verified_tc_pdf()
    connection = MagicMock()
    connection.__enter__.return_value = connection
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.fetchone.side_effect = [None, (12, now)]
    connection.cursor.return_value = cursor
    request = TCReferenceRequest(
        url=document["final_url"],
        expected_number="TC/0019/24",
    )

    with (
        patch("registry.connect", return_value=connection),
        patch("registry.init_schema"),
        patch("app.fetch_document", new=AsyncMock(return_value=document)),
        patch("app.extract_document_text", return_value=extracted),
    ):
        result = asyncio.run(register_tc_reference(request))

    assert result["status"] == "ALREADY_VERIFIED_SOURCE"
    assert result["deduplicated"] is True
    assert cursor.execute.call_count == 3
    assert "ratio_decidendi_verified=FALSE" in cursor.execute.call_args.args[0]


def test_tc_reference_rejects_mismatched_document_number():
    document, extracted = _verified_tc_pdf("TC/0020/24")
    request = TCReferenceRequest(
        url=document["final_url"],
        expected_number="TC/0019/24",
    )

    with (
        patch("app.fetch_document", new=AsyncMock(return_value=document)),
        patch("app.extract_document_text", return_value=extracted),
        pytest.raises(HTTPException) as error,
    ):
        asyncio.run(register_tc_reference(request))

    assert error.value.status_code == 422
    assert "número de sentencia" in error.value.detail
