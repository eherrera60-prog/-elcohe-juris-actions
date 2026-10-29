import unittest
import asyncio
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from fastapi import HTTPException
import httpx

from app import (
    app,
    decision_number_matches,
    fetch_document,
    primary_decision_number,
    search_tc,
    search_tc_decisions,
)
from juris_164 import (
    _scj_archive_candidates,
    _scj_candidates,
    _search_scj_official_archive,
    search_scj_candidates,
)


SCJ_TEXT = """SUPREMA CORTE DE JUSTICIA
PRIMERA SALA DE LA SUPREMA CORTE DE JUSTICIA
SENTENCIA SCJ-TS-24-00123
En fecha 4 de marzo de 2024.
LA SALA, DESPUÉS DE HABER DELIBERADO, CONSIDERA QUE:
1) Esta Suprema Corte establece que la interpretación debe atender el caso concreto.
El Tribunal Constitucional, mediante sentencia TC/0099/22, fue citado como precedente.
"""


def document(text=SCJ_TEXT, *, extraction_ok=True):
    return {
        "document_type": "PDF",
        "pages": [{"page": 1, "text": text}],
        "full_text": text,
        "page_count": 1,
        "extraction_ok": extraction_ok,
    }


def official_pdf():
    return {
        "final_url": "https://consultasentenciascj.poderjudicial.gob.do/documento.pdf",
        "host": "consultasentenciascj.poderjudicial.gob.do",
        "content_type": "application/pdf",
        "data": b"%PDF-1.7 test bytes",
        "sha256": "a" * 64,
    }


class FoundationExtractionTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.payload = {
            "url": "https://consultasentenciascj.poderjudicial.gob.do/documento.pdf",
            "expected_court": "SCJ",
            "expected_number": "SCJ-TS-24-00123",
        }

    def post_extraction(self, doc=None, extracted=None, extraction_error=None):
        with patch("app.fetch_document", new=AsyncMock(return_value=doc or official_pdf())):
            if extraction_error:
                extractor = patch("app.extract_document_text", side_effect=extraction_error)
            else:
                extractor = patch(
                    "app.extract_document_text",
                    return_value=extracted or document(),
                )
            with extractor:
                return self.client.post("/v1/extract-foundations", json=self.payload)

    def assert_evidence_is_not_a_verified_criterion(self, result):
        self.assertEqual(result["evidence_type"], "EXTRACTED_SOURCE_TEXT")
        self.assertEqual(result["citation_classification"], "NOT_PERFORMED")
        self.assertIsNone(result["interpretation"])
        self.assertEqual(result["criterion_status"], "NOT_VERIFIED")
        self.assertIsNone(result["verified_criterion"])

    def test_verified_pdf_returns_page_anchored_passage_without_certifying_holding(self):
        response = self.post_extraction()
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(result["source_status"], "VERIFIED_SOURCE")
        self.assertEqual(result["identity_status"], "VERIFIED_IDENTITY")
        self.assertEqual(result["passage_status"], "VERIFIED_PASSAGE")
        self.assertEqual(result["status"], "VERIFIED_PASSAGE")
        self.assert_evidence_is_not_a_verified_criterion(result)
        self.assertEqual(result["legal_holding_status"], "CANDIDATE")
        passage = result["foundations"][0]
        self.assert_evidence_is_not_a_verified_criterion(passage)
        self.assertEqual(passage["decision_number"], "SCJ-TS-24-00123")
        self.assertEqual(passage["tribunal"], "Primera Sala")
        self.assertEqual(passage["date"], "4 de marzo de 2024")
        self.assertEqual(passage["page"], 1)
        self.assertIn("Esta Suprema Corte establece", passage["passage"])
        self.assertFalse(passage["ratio_decidendi_verified"])

    def test_analyze_decision_distinguishes_extracted_text_from_criterion(self):
        with patch("app.fetch_document", new=AsyncMock(return_value=official_pdf())), \
             patch("app.extract_document_text", return_value=document()):
            response = self.client.post("/v1/analyze-decision", json={
                **self.payload,
                "legal_issue": "interpretación jurídica y caso concreto",
            })
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assert_evidence_is_not_a_verified_criterion(result)
        self.assertTrue(result["relevant_passages"])
        self.assert_evidence_is_not_a_verified_criterion(result["relevant_passages"][0])
        self.assertIsNone(result["jurisprudential_card"]["ratio_decidendi"])

    def test_decision_excerpt_is_source_text_not_interpretation(self):
        with patch("app.fetch_document", new=AsyncMock(return_value=official_pdf())), \
             patch("app.extract_document_text", return_value=document()):
            response = self.client.post("/v1/decision-excerpt", json={
                **self.payload,
                "page": 1,
                "query": "Esta Suprema Corte",
            })
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assert_evidence_is_not_a_verified_criterion(result)
        self.assertTrue(result["query_found"])
        self.assertFalse(result["ratio_decidendi_verified"])

    def test_promote_holding_keeps_criterion_unverified_without_review(self):
        with patch("app.fetch_document", new=AsyncMock(return_value=official_pdf())), \
             patch("app.extract_document_text", return_value=document()):
            response = self.client.post("/v1/promote-holding", json={
                **self.payload,
                "legal_issue": "interpretación jurídica y caso concreto",
                "legal_proposition": "La interpretación depende del análisis concreto del tribunal.",
                "support_quote": "Esta cita inventada no aparece en el documento oficial.",
                "support_page": 1,
                "judicial_reasoning_reviewed": False,
            })
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(result["status"], "HOLDING_NOT_PROMOTED")
        self.assert_evidence_is_not_a_verified_criterion(result)
        self.assertIsNone(result["legal_proposition"])

    def test_cited_tc_number_does_not_replace_scj_identity(self):
        response = self.post_extraction()
        result = response.json()
        self.assertEqual(result["metadata"]["decision_number"], "SCJ-TS-24-00123")
        self.assertEqual(result["identity_status"], "VERIFIED_IDENTITY")
        self.assertEqual(result["foundations"][0]["decision_number"], "SCJ-TS-24-00123")

    def test_wrong_court_is_not_verified_even_when_other_court_is_cited(self):
        self.payload["expected_court"] = "TC"
        self.payload["expected_number"] = "TC/0099/22"
        response = self.post_extraction()
        result = response.json()
        self.assertEqual(result["source_status"], "VERIFIED_SOURCE")
        self.assertEqual(result["identity_status"], "NOT_VERIFIED")
        self.assertEqual(result["passage_status"], "NOT_VERIFIED")
        self.assertEqual(result["foundations"], [])

    def test_wrong_primary_number_is_not_mistaken_for_cross_reference(self):
        self.payload["expected_number"] = "SCJ-TS-24-00999"
        result = self.post_extraction().json()
        self.assertEqual(result["identity_status"], "NOT_VERIFIED")
        self.assertEqual(result["passage_status"], "NOT_VERIFIED")

    def test_official_html_is_not_a_verified_pdf(self):
        html_doc = {
            **official_pdf(),
            "content_type": "text/html",
            "data": b"<html>not a judgment</html>",
        }
        result = self.post_extraction(doc=html_doc).json()
        self.assertEqual(result["status"], "NOT_VERIFIED")
        self.assertEqual(result["source_status"], "NOT_VERIFIED")
        self.assertEqual(result["identity_status"], "NOT_VERIFIED")

    def test_failed_text_extraction_never_verifies_identity_or_passage(self):
        result = self.post_extraction(extracted=document("", extraction_ok=False)).json()
        self.assertEqual(result["source_status"], "VERIFIED_SOURCE")
        self.assertEqual(result["identity_status"], "NOT_VERIFIED")
        self.assertEqual(result["passage_status"], "NOT_VERIFIED")
        self.assertEqual(result["foundations"], [])

    def test_corrupt_pdf_extraction_error_is_explicit(self):
        response = self.post_extraction(
            extraction_error=HTTPException(422, "PDF inválido o ilegible.")
        )
        self.assertEqual(response.status_code, 422)


class ScjSearchParsingTests(unittest.TestCase):
    def test_fetch_document_reports_official_source_http_status(self):
        for status_code in (403, 404):
            with self.subTest(status_code=status_code):
                class FakeResponse:
                    headers = {}

                    def __init__(self, response_status):
                        self.status_code = response_status

                    async def __aenter__(self):
                        return self

                    async def __aexit__(self, *args):
                        return None

                    def raise_for_status(self):
                        request = httpx.Request(
                            "GET", "https://poderjudicial.gob.do/documento.pdf"
                        )
                        response = httpx.Response(status_code, request=request)
                        raise httpx.HTTPStatusError(
                            "Source error", request=request, response=response
                        )

                class FakeClient:
                    def __init__(self, **kwargs):
                        pass

                    async def __aenter__(self):
                        return self

                    async def __aexit__(self, *args):
                        return None

                    def stream(self, method, url):
                        return FakeResponse(status_code)

                with patch("app.validate_official_url"), \
                     patch("app.httpx.AsyncClient", FakeClient):
                    with self.assertRaises(HTTPException) as raised:
                        asyncio.run(fetch_document(
                            "https://poderjudicial.gob.do/documento.pdf"
                        ))
                self.assertIn(f"HTTP {status_code}", raised.exception.detail)

    def test_fetch_document_reports_timeout_type(self):
        class FakeClient:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            def stream(self, method, url):
                raise httpx.ReadTimeout("official source timed out")

        with patch("app.validate_official_url"), \
             patch("app.httpx.AsyncClient", FakeClient):
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(fetch_document(
                    "https://poderjudicial.gob.do/documento.pdf"
                ))
        self.assertIn("ReadTimeout", raised.exception.detail)

    def test_scj_search_plan_is_explicitly_not_executed_and_does_not_claim_filters_applied(self):
        response = TestClient(app).get(
            "/v1/scj/search-plan",
            params={
                "q": "responsabilidad civil",
                "chamber": "Primera Sala",
                "matter": "Civil y Comercial",
                "year_from": 2020,
                "year_to": 2024,
            },
        )
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(result["status"], "SEARCH_PLAN_ONLY")
        self.assertFalse(result["search_executed"])
        self.assertFalse(result["filters_applied"])
        self.assertEqual(result["filters"]["sala"], "Primera Sala")
        self.assertEqual(result["filters"]["desde"], 2020)

    def test_scj_archive_html_without_explicit_empty_state_is_extraction_failure(self):
        class FakeResponse:
            status_code = 200
            headers = {"content-type": "text/html; charset=utf-8"}
            text = "<html><body><main>Portal institucional</main></body></html>"
            url = httpx.URL("https://poderjudicial.gob.do/?s=consulta")

            def raise_for_status(self):
                return None

        class FakeClient:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def get(self, url, **kwargs):
                return FakeResponse()

        with patch("juris_164.httpx.AsyncClient", FakeClient):
            result = asyncio.run(_search_scj_official_archive(
                "consulta",
                lambda host: bool(host and host.endswith("poderjudicial.gob.do")),
                lambda url: None,
            ))
        self.assertEqual(result["status"], "EXTRACTION_FAILED")
        self.assertEqual(result["results"], [])
        self.assertEqual(result["search_url"], "https://poderjudicial.gob.do/?s=consulta")

    def test_scj_archive_explicit_empty_state_confirms_only_that_archive_query(self):
        class FakeResponse:
            status_code = 200
            headers = {"content-type": "text/html; charset=utf-8"}
            text = """<html><body class="search search-no-results">
              <div class="elementor-posts-container">
                <div class="elementor-posts-nothing-found">
                  Parece que no podemos encontrar lo que buscas. Intente nuevamente con otros términos.
                </div>
              </div>
            </body></html>"""
            url = httpx.URL("https://poderjudicial.gob.do/?s=consulta")

            def raise_for_status(self):
                return None

        class FakeClient:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def get(self, url, **kwargs):
                return FakeResponse()

        with patch("juris_164.httpx.AsyncClient", FakeClient):
            result = asyncio.run(_search_scj_official_archive(
                "consulta",
                lambda host: bool(host and host.endswith("poderjudicial.gob.do")),
                lambda url: None,
            ))
        self.assertEqual(result["status"], "NO_RESULTS_CONFIRMED")
        self.assertEqual(result["results"], [])
        self.assertTrue(result["search_executed"])

    def test_scj_archive_generic_empty_text_without_official_state_is_extraction_failure(self):
        class FakeResponse:
            status_code = 200
            headers = {"content-type": "text/html; charset=utf-8"}
            text = "<html><body><main>No se encontraron resultados</main></body></html>"
            url = httpx.URL("https://poderjudicial.gob.do/?s=consulta")

            def raise_for_status(self):
                return None

        class FakeClient:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def get(self, url, **kwargs):
                return FakeResponse()

        with patch("juris_164.httpx.AsyncClient", FakeClient):
            result = asyncio.run(_search_scj_official_archive(
                "consulta",
                lambda host: bool(host and host.endswith("poderjudicial.gob.do")),
                lambda url: None,
            ))
        self.assertEqual(result["status"], "EXTRACTION_FAILED")
        self.assertTrue(result["search_executed"])
        self.assertEqual(result["results"], [])

    def test_scj_archive_http_403_is_an_access_failure(self):
        class FakeResponse:
            status_code = 403
            headers = {"content-type": "text/html; charset=utf-8"}
            url = httpx.URL("https://poderjudicial.gob.do/?s=consulta")

            def raise_for_status(self):
                request = httpx.Request("GET", str(self.url))
                response = httpx.Response(403, request=request)
                raise httpx.HTTPStatusError(
                    "Forbidden", request=request, response=response
                )

        class FakeClient:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def get(self, url, **kwargs):
                return FakeResponse()

        with patch("juris_164.httpx.AsyncClient", FakeClient):
            result = asyncio.run(_search_scj_official_archive(
                "consulta",
                lambda host: bool(host and host.endswith("poderjudicial.gob.do")),
                lambda url: None,
            ))
        self.assertEqual(result["status"], "ACCESS_FAILED")
        self.assertEqual(result["http_status"], 403)
        self.assertFalse(result["search_executed"])
        self.assertEqual(result["results"], [])

    def test_scj_search_http_403_is_an_access_failure_not_an_empty_search(self):
        class FakeResponse:
            status_code = 403
            headers = {"content-type": "text/html"}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def aiter_bytes(self):
                yield b"Forbidden"

            def raise_for_status(self):
                request = httpx.Request("GET", "https://consultasentenciascj.poderjudicial.gob.do/")
                response = httpx.Response(403, request=request)
                raise httpx.HTTPStatusError("Forbidden", request=request, response=response)

        class FakeClient:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            def stream(self, method, url, **kwargs):
                return FakeResponse()

        with patch("app.validate_official_url"), patch("juris_164.httpx.AsyncClient", FakeClient):
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(search_scj_candidates("consulta"))
        self.assertEqual(raised.exception.detail["status"], "ACCESS_FAILED")
        self.assertEqual(raised.exception.detail["http_status"], 403)
        self.assertFalse(raised.exception.detail["search_executed"])

    def test_scj_search_http_404_is_an_access_failure_not_an_empty_search(self):
        class FakeResponse:
            status_code = 404
            headers = {"content-type": "text/html"}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            def raise_for_status(self):
                request = httpx.Request("GET", "https://consultasentenciascj.poderjudicial.gob.do/")
                response = httpx.Response(404, request=request)
                raise httpx.HTTPStatusError("Not found", request=request, response=response)

        class FakeClient:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            def stream(self, method, url, **kwargs):
                return FakeResponse()

        with patch("app.validate_official_url"), patch("juris_164.httpx.AsyncClient", FakeClient):
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(search_scj_candidates("consulta"))
        self.assertEqual(raised.exception.detail["status"], "ACCESS_FAILED")
        self.assertEqual(raised.exception.detail["http_status"], 404)
        self.assertFalse(raised.exception.detail["search_executed"])

    def test_scj_search_timeout_is_an_access_failure(self):
        class FakeClient:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            def stream(self, method, url, **kwargs):
                raise httpx.ReadTimeout("portal timed out")

        with patch("app.validate_official_url"), patch("juris_164.httpx.AsyncClient", FakeClient):
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(search_scj_candidates("consulta"))
        self.assertEqual(raised.exception.detail["status"], "ACCESS_FAILED")
        self.assertEqual(raised.exception.detail["error_type"], "ReadTimeout")
        self.assertFalse(raised.exception.detail["search_executed"])

    def test_scj_four_digit_year_identifier_is_recognized_from_official_pdf_text(self):
        text = """REPÚBLICA DOMINICANA
SUPREMA CORTE DE JUSTICIA
SCJ-TS-2024-01248
La Tercera Sala de la Suprema Corte de Justicia, en fecha 31 de julio de 2024,
dicta la siguiente sentencia.
"""
        self.assertEqual(primary_decision_number(text, "SCJ"), "SCJ-TS-2024-01248")
        self.assertTrue(
            decision_number_matches(text, "SCJ", "SCJ-TS-2024-01248")
        )

    def test_scj_archive_extracts_official_judgment_link_as_unverified_candidate(self):
        article = """<html><body>
          <div class="elementor-widget-theme-post-content">
            <p>La Tercera Sala de la Suprema Corte de Justicia, en fecha 31 de
            julio de 2024, emitió la sentencia número SCJ-TS-2024-01248.</p>
            <p>La sentencia íntegra se puede visualizar
            <a href="https://transparencia.poderjudicial.gob.do/documentos/PDF/sentencias_destacadas/2023-RECA-00992.pdf">aquí.</a></p>
          </div>
        </body></html>"""
        results = _scj_archive_candidates(
            article,
            "https://poderjudicial.gob.do/decisiones/ejemplo/",
            "Publicación institucional",
            lambda host: bool(host and host.endswith("poderjudicial.gob.do")),
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["decision_number"], "SCJ-TS-2024-01248")
        self.assertEqual(results[0]["tribunal"], "Tercera Sala de la Suprema Corte de Justicia")
        self.assertEqual(results[0]["date"], "31 de julio de 2024")
        self.assertEqual(results[0]["status"], "CANDIDATE")
        self.assertEqual(results[0]["source_status"], "NOT_VERIFIED")
        self.assertFalse(results[0]["official_document_verified"])
        self.assertFalse(results[0]["ratio_decidendi_verified"])
        self.assertTrue(results[0]["source_url"].endswith("2023-RECA-00992.pdf"))

    def test_scj_archive_rejects_unofficial_or_unrelated_pdf_links(self):
        article = """<div class="elementor-widget-theme-post-content">
          <p>Sentencia SCJ-TS-2024-01248 de la Tercera Sala de la Suprema Corte de Justicia.</p>
          <p>Certificado <a href="https://example.com/sentencia.pdf">aquí</a></p>
        </div>"""
        results = _scj_archive_candidates(
            article,
            "https://poderjudicial.gob.do/decisiones/ejemplo/",
            "Publicación institucional",
            lambda host: bool(host and host.endswith("poderjudicial.gob.do")),
        )
        self.assertEqual(results, [])

    def test_scj_archive_fetches_post_id_without_adding_a_referrer(self):
        requests = []
        listing = """<article class="post-41099">
          <h2><a href="https://poderjudicial.gob.do/sentencia-scj/">Sentencia SCJ</a></h2>
        </article>"""
        article = """<div class="elementor-widget-theme-post-content">
          <p>Sentencia SCJ-TS-2024-01248 de la Tercera Sala de la Suprema Corte de Justicia.</p>
          <p>Sentencia íntegra <a href="https://transparencia.poderjudicial.gob.do/documentos/sentencia.pdf">aquí</a></p>
        </div>"""

        class FakeResponse:
            status_code = 200
            headers = {"content-type": "text/html; charset=utf-8"}

            def __init__(self, text, url):
                self.text = text
                self.url = httpx.URL(url)

            def raise_for_status(self):
                return None

        class FakeClient:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def get(self, url, **kwargs):
                requests.append((url, kwargs))
                if "params" in kwargs:
                    return FakeResponse(
                        listing,
                        "https://poderjudicial.gob.do/?s=SCJ-TS-2024-01248",
                    )
                return FakeResponse(article, "https://poderjudicial.gob.do/?p=41099")

        with patch("juris_164.httpx.AsyncClient", FakeClient):
            result = asyncio.run(_search_scj_official_archive(
                "SCJ-TS-2024-01248",
                lambda host: bool(host and host.endswith("poderjudicial.gob.do")),
                lambda url: None,
            ))

        self.assertEqual(result["status"], "CANDIDATES_FOUND")
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(
            str(requests[1][0]),
            "https://poderjudicial.gob.do/?p=41099",
        )
        self.assertEqual(requests[1][1], {})

    def test_tc_search_extracts_an_official_decision_as_unverified_candidate(self):
        class FakeResponse:
            status_code = 200
            headers = {"content-type": "text/html; charset=utf-8"}
            url = httpx.URL("https://www.tribunalconstitucional.gob.do/consultas/sentencias/")
            text = (
                '<input id="searchString" value="TC/1100/26">'
                '<script>var filter = "2026";</script>'
                '<article><span>Tribunal Constitucional 2026-03-01</span>'
                '<a href="/consultas/secretaría/sentencias/tc110026">'
                "TC/1100/26</a>"
                "</article>"
            )

            def raise_for_status(self):
                return None

        class FakeClient:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def get(self, url, params):
                requests.append((url, params))
                return FakeResponse()

        requests = []
        with patch("app.httpx.AsyncClient", FakeClient):
            result = asyncio.run(search_tc_decisions("  TC/1100/26  ", 2026, 5))
        self.assertEqual(result["status"], "CANDIDATES_FOUND")
        self.assertTrue(result["search_executed"])
        self.assertEqual(result["query"], "TC/1100/26")
        self.assertEqual(requests[0][1]["searchString"], "TC/1100/26")
        self.assertEqual(result["results"][0]["decision_number"], "TC/1100/26")
        self.assertEqual(result["results"][0]["tribunal"], "Tribunal Constitucional")
        self.assertEqual(result["results"][0]["date"], "2026-03-01")
        self.assertEqual(result["results"][0]["verification_status"], "CANDIDATE")
        self.assertEqual(result["results"][0]["source_status"], "NOT_VERIFIED")
        self.assertFalse(result["results"][0]["official_document_verified"])
        self.assertFalse(result["results"][0]["ratio_decidendi_verified"])

    def test_tc_legacy_adapter_preserves_integrated_search_status(self):
        class FakeResponse:
            status_code = 200
            headers = {"content-type": "text/html"}
            url = httpx.URL("https://www.tribunalconstitucional.gob.do/consultas/sentencias/")
            text = (
                '<input id="searchString" value="TC/1100/26">'
                '<script>var filter = "2026";</script>'
                '<a href="/consultas/secretaría/sentencias/tc110026">TC/1100/26</a>'
            )

            def raise_for_status(self):
                return None

        class FakeClient:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def get(self, url, params):
                return FakeResponse()

        with patch("app.httpx.AsyncClient", FakeClient):
            result = asyncio.run(search_tc("TC/1100/26", 2026, 5))
        self.assertEqual(result["status"], "LOCATED_RESULTS")
        self.assertTrue(result["search_executed"])

    def test_tc_explicit_empty_state_confirms_only_this_query_has_no_results(self):
        class FakeResponse:
            status_code = 200
            headers = {"content-type": "text/html"}
            url = httpx.URL("https://www.tribunalconstitucional.gob.do/consultas/sentencias/")
            text = (
                '<input id="searchString" value="referencia jurídica">'
                "<h4>No hay resultados para mostrar</h4>"
            )

            def raise_for_status(self):
                return None

        class FakeClient:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def get(self, url, params):
                return FakeResponse()

        with patch("app.httpx.AsyncClient", FakeClient):
            result = asyncio.run(search_tc(" referencia   jurídica ", None, 5))
        self.assertEqual(result["status"], "NO_RESULTS_CONFIRMED")
        self.assertTrue(result["search_executed"])
        self.assertEqual(result["query"], "referencia jurídica")
        self.assertEqual(result["results"], [])

    def test_tc_empty_page_without_official_empty_state_is_extraction_failure(self):
        class FakeResponse:
            status_code = 200
            headers = {"content-type": "text/html"}
            url = httpx.URL("https://www.tribunalconstitucional.gob.do/consultas/sentencias/")
            text = '<input id="searchString" value="consulta">'

            def raise_for_status(self):
                return None

        class FakeClient:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def get(self, url, params):
                return FakeResponse()

        with patch("app.httpx.AsyncClient", FakeClient):
            result = asyncio.run(search_tc("consulta", None, 5))
        self.assertEqual(result["status"], "EXTRACTION_FAILED")
        self.assertTrue(result["search_executed"])
        self.assertEqual(result["results"], [])

    def test_tc_empty_state_with_unextracted_decision_reference_is_not_confirmed(self):
        class FakeResponse:
            status_code = 200
            headers = {"content-type": "text/html"}
            url = httpx.URL("https://www.tribunalconstitucional.gob.do/consultas/sentencias/")
            text = (
                '<input id="searchString" value="consulta">'
                "<h4>No hay resultados para mostrar</h4>"
                "<p>TC/1234/24</p>"
            )

            def raise_for_status(self):
                return None

        class FakeClient:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def get(self, url, params):
                return FakeResponse()

        with patch("app.httpx.AsyncClient", FakeClient):
            result = asyncio.run(search_tc("consulta", None, 5))
        self.assertEqual(result["status"], "EXTRACTION_FAILED")
        self.assertTrue(result["search_executed"])
        self.assertEqual(result["results"], [])

    def test_tc_connection_failure_is_distinguished_from_extraction_failure(self):
        class FakeClient:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def get(self, url, params):
                raise httpx.ConnectError("connection refused")

        with patch("app.httpx.AsyncClient", FakeClient):
            result = asyncio.run(search_tc("consulta", None, 5))
        self.assertEqual(result["status"], "ACCESS_FAILED")
        self.assertFalse(result["search_executed"])
        self.assertEqual(result["results"], [])

    def test_tc_http_403_is_an_access_failure(self):
        class FakeClient:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def get(self, url, params):
                request = httpx.Request("GET", str(url))
                response = httpx.Response(403, request=request)
                raise httpx.HTTPStatusError("Forbidden", request=request, response=response)

        with patch("app.httpx.AsyncClient", FakeClient):
            result = asyncio.run(search_tc("consulta", None, 5))
        self.assertEqual(result["status"], "ACCESS_FAILED")
        self.assertEqual(result["http_status"], 403)
        self.assertFalse(result["search_executed"])

    def test_tc_http_404_is_an_access_failure(self):
        class FakeClient:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def get(self, url, params):
                request = httpx.Request("GET", str(url))
                response = httpx.Response(404, request=request)
                raise httpx.HTTPStatusError("Not found", request=request, response=response)

        with patch("app.httpx.AsyncClient", FakeClient):
            result = asyncio.run(search_tc_decisions("consulta", None, 5))
        self.assertEqual(result["status"], "ACCESS_FAILED")
        self.assertEqual(result["http_status"], 404)
        self.assertFalse(result["search_executed"])

    def test_tc_timeout_is_an_access_failure(self):
        class FakeClient:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def get(self, url, params):
                raise httpx.ReadTimeout("portal timed out")

        with patch("app.httpx.AsyncClient", FakeClient):
            result = asyncio.run(search_tc_decisions("consulta", None, 5))
        self.assertEqual(result["status"], "ACCESS_FAILED")
        self.assertEqual(result["error_type"], "ReadTimeout")
        self.assertFalse(result["search_executed"])

    def test_tc_year_filter_must_be_echoed_by_the_official_page(self):
        class FakeResponse:
            status_code = 200
            headers = {"content-type": "text/html"}
            url = httpx.URL("https://www.tribunalconstitucional.gob.do/consultas/sentencias/")
            text = (
                '<input id="searchString" value="consulta">'
                '<script>var filter = "2023";</script>'
            )

            def raise_for_status(self):
                return None

        class FakeClient:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def get(self, url, params):
                self.params = params
                return FakeResponse()

        with patch("app.httpx.AsyncClient", FakeClient):
            result = asyncio.run(search_tc("consulta", 2024, 5))
        self.assertEqual(result["status"], "EXTRACTION_FAILED")
        self.assertFalse(result["search_executed"])
        self.assertEqual(result["results"], [])

    def test_tc_query_echo_mismatch_is_not_a_confirmed_empty_search(self):
        class FakeResponse:
            status_code = 200
            headers = {"content-type": "text/html"}
            url = httpx.URL("https://www.tribunalconstitucional.gob.do/consultas/sentencias/")
            text = (
                '<input id="searchString" value="otra consulta">'
                "<h4>No hay resultados para mostrar</h4>"
            )

            def raise_for_status(self):
                return None

        class FakeClient:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def get(self, url, params):
                return FakeResponse()

        with patch("app.httpx.AsyncClient", FakeClient):
            result = asyncio.run(search_tc("consulta", None, 5))
        self.assertEqual(result["status"], "EXTRACTION_FAILED")
        self.assertFalse(result["search_executed"])

    def test_official_search_rows_are_candidates_not_verified_decisions(self):
        payload = {
            "recordsFiltered": 1,
            "data": [{
                "noSentencia": "SCJ-TS-24-00123",
                "noUnico": "2024-R00001",
                "descTribunal": "PRIMERA SALA",
                "fechaFallo": "2024-03-04",
                "urlBlob": "https://consultasentenciascj.poderjudicial.gob.do/files/decision.pdf",
            }],
        }
        results, count = _scj_candidates(
            payload, lambda host: host == "consultasentenciascj.poderjudicial.gob.do"
        )
        self.assertEqual(count, 1)
        self.assertEqual(results[0]["status"], "CANDIDATE")
        self.assertEqual(results[0]["source_status"], "NOT_VERIFIED")
        self.assertEqual(results[0]["identity_status"], "NOT_VERIFIED")
        self.assertEqual(results[0]["passage_status"], "NOT_VERIFIED")
        self.assertFalse(results[0]["ratio_decidendi_verified"])

    def test_unofficial_document_link_is_not_returned_as_official_source(self):
        payload = {
            "recordsFiltered": 1,
            "data": [{"urlBlob": "https://example.com/fake.pdf"}],
        }
        results, _ = _scj_candidates(payload, lambda host: False)
        self.assertEqual(results, [])

    def test_unrecognized_search_response_is_not_reported_as_empty_results(self):
        self.assertIsNone(_scj_candidates({"error": "temporarily unavailable"}, lambda _: True))

    def test_scj_search_posts_to_official_data_endpoint_and_returns_candidates(self):
        portal = b"""<html><select id="cbSala">
          <option value="1">PRIMERA SALA SUPREMA CORTE DE JUSTICIA</option>
        </select><select id="cbMateria">
          <option value="2">CIVIL Y COMERCIAL</option>
        </select><select id="cbAno">
          <option value="2024">2024</option>
        </select></html>"""
        response_body = (
            b'{"recordsFiltered":1,"data":[{"noSentencia":"SCJ-TS-24-00123",'
            b'"urlBlob":"https://consultasentenciascj.poderjudicial.gob.do/decision.pdf"}]}'
        )
        posted = []

        class FakeResponse:
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
                    return FakeResponse(portal, "text/html")
                posted.append((url, kwargs["data"]))
                return FakeResponse(response_body, "application/json")

        with patch("app.validate_official_url"), patch("juris_164.httpx.AsyncClient", FakeClient):
            result = asyncio.run(search_scj_candidates(
                "  responsabilidad \n civil  ", "Primera Sala", "Civil y Comercial",
                2024, 2024, 3,
            ))
        self.assertEqual(result["status"], "CANDIDATES_FOUND")
        self.assertTrue(result["search_executed"])
        self.assertEqual(result["query"], "responsabilidad civil")
        self.assertEqual(result["results"][0]["decision_number"], "SCJ-TS-24-00123")
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0][1]["IdTribunal"], "1")
        self.assertEqual(posted[0][1]["Materia"], "2")
        self.assertEqual(posted[0][1]["Ano"], "2024")
        self.assertEqual(posted[0][1]["Contenido"], "responsabilidad civil")

    def test_scj_search_without_year_uses_the_portals_unfiltered_year_value(self):
        portal = b"""<html><select id="cbSala"></select><select id="cbMateria"></select>
          <select id="cbAno"><option value="2024">2024</option></select></html>"""
        response_body = (
            b'{"recordsFiltered":1,"recordsTotal":1,"data":[{"noSentencia":"SCJ-TS-24-00123",'
            b'"urlBlob":"https://consultasentenciascj.poderjudicial.gob.do/decision.pdf"}]}'
        )
        posted = []

        class FakeResponse:
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
                    return FakeResponse(portal, "text/html")
                posted.append(kwargs["data"])
                return FakeResponse(response_body, "application/json")

        with patch("app.validate_official_url"), \
             patch("juris_164.httpx.AsyncClient", FakeClient), \
             patch("juris_164._search_scj_official_archive",
                   new=AsyncMock()) as search_archive:
            result = asyncio.run(search_scj_candidates("Constitución", limit=1))

        self.assertEqual(result["status"], "CANDIDATES_FOUND")
        self.assertTrue(result["search_executed"])
        self.assertEqual(result["official_results_reported"], 1)
        self.assertEqual(posted[0]["Ano"], "")
        self.assertEqual(posted[0]["IdTipoDocumento"], "1")
        self.assertEqual(result["results"][0]["decision_number"], "SCJ-TS-24-00123")
        self.assertEqual(result["results"][0]["source_status"], "NOT_VERIFIED")
        self.assertFalse(result["results"][0]["official_document_verified"])
        self.assertFalse(result["results"][0]["ratio_decidendi_verified"])
        search_archive.assert_not_awaited()

    def test_scj_primary_zero_preserves_alternative_source_status(self):
        portal = b"""<html><select id="cbSala"><option value="1">PRIMERA SALA</option></select>
          <select id="cbMateria"><option value="2">CIVIL Y COMERCIAL</option></select>
          <select id="cbAno"><option value="2024">2024</option></select></html>"""
        response_body = b'{"recordsFiltered":0,"recordsTotal":0,"data":[]}'

        class FakeResponse:
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
                    return FakeResponse(portal, "text/html")
                return FakeResponse(response_body, "application/json")

        expected_statuses = {
            "ACCESS_FAILED": "ACCESS_FAILED",
            "EXTRACTION_FAILED": "EXTRACTION_FAILED",
            "NO_RESULTS_CONFIRMED": "NO_RESULTS_CONFIRMED",
        }
        for archive_status, expected_status in expected_statuses.items():
            no_archive = {
                "status": archive_status,
                "search_url": "https://poderjudicial.gob.do/",
                "results": [],
                "search_executed": archive_status != "ACCESS_FAILED",
                "message": f"Archive reported {archive_status}",
            }
            with self.subTest(archive_status=archive_status), \
                 patch("app.validate_official_url"), \
                 patch("juris_164.httpx.AsyncClient", FakeClient), \
                 patch("juris_164._search_scj_official_archive",
                       new=AsyncMock(return_value=no_archive)):
                result = asyncio.run(search_scj_candidates(
                    "  sin   coincidencias ", limit=3
                ))
            self.assertEqual(result["status"], expected_status)
            self.assertTrue(result["search_executed"])
            self.assertEqual(result["query"], "sin coincidencias")
            self.assertEqual(result["official_results_reported"], 0)
            self.assertEqual(result["results"], [])
            self.assertEqual(result["archive_search"]["status"], archive_status)
            self.assertEqual(
                result["archive_search"]["search_executed"],
                archive_status != "ACCESS_FAILED",
            )

    def test_scj_falls_back_to_first_party_search_after_primary_zero_results(self):
        portal = b"""<html><select id="cbSala"></select><select id="cbMateria"></select>
          <select id="cbAno"></select></html>"""
        response_body = b'{"recordsFiltered":0,"recordsTotal":0,"data":[]}'

        class FakeResponse:
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
                    return FakeResponse(portal, "text/html")
                return FakeResponse(response_body, "application/json")

        candidate = {
            "status": "CANDIDATE",
            "court": "SCJ",
            "decision_number": "SCJ-TS-2024-01248",
            "source_url": "https://transparencia.poderjudicial.gob.do/judgment.pdf",
            "source_status": "NOT_VERIFIED",
            "official_document_verified": False,
            "ratio_decidendi_verified": False,
        }
        archive = {
            "status": "CANDIDATES_FOUND",
            "search_url": "https://poderjudicial.gob.do/",
            "results": [candidate],
            "message": "El buscador institucional localizó un PDF candidato.",
        }
        with patch("app.validate_official_url"), \
             patch("juris_164.httpx.AsyncClient", FakeClient), \
             patch("juris_164._search_scj_official_archive", new=AsyncMock(return_value=archive)):
            result = asyncio.run(search_scj_candidates("SCJ-TS-2024-01248", limit=3))
        self.assertEqual(result["status"], "CANDIDATES_FOUND")
        self.assertEqual(result["official_results_reported"], 0)
        self.assertEqual(result["archive_search"]["status"], "CANDIDATES_FOUND")
        self.assertEqual(result["results"][0]["decision_number"], "SCJ-TS-2024-01248")
        self.assertFalse(result["results"][0]["official_document_verified"])

    def test_scj_reported_rows_without_extractable_candidates_are_not_zero_results(self):
        portal = b"""<html><select id="cbSala"></select><select id="cbMateria"></select>
          <select id="cbAno"></select></html>"""
        response_body = b'{"recordsFiltered":2,"recordsTotal":2,"data":[null,null]}'

        class FakeResponse:
            status_code = 200
            headers = {"content-type": "text/html"}

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
                    return FakeResponse(portal, "text/html")
                return FakeResponse(response_body, "application/json")

        with patch("app.validate_official_url"), patch("juris_164.httpx.AsyncClient", FakeClient):
            result = asyncio.run(search_scj_candidates("consulta", limit=3))
        self.assertEqual(result["status"], "EXTRACTION_FAILED")
        self.assertTrue(result["search_executed"])
        self.assertEqual(result["official_results_reported"], 2)
        self.assertEqual(result["results"], [])

    def test_scj_connection_failure_has_explicit_access_status(self):
        class FakeClient:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            def stream(self, method, url, **kwargs):
                raise httpx.ConnectError("connection refused")

        with patch("app.validate_official_url"), patch("juris_164.httpx.AsyncClient", FakeClient):
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(search_scj_candidates("consulta"))
        self.assertEqual(raised.exception.status_code, 502)
        self.assertEqual(raised.exception.detail["status"], "ACCESS_FAILED")
        self.assertFalse(raised.exception.detail["search_executed"])


if __name__ == "__main__":
    unittest.main()
