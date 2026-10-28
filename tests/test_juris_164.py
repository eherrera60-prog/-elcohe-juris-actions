import unittest
import asyncio
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from fastapi import HTTPException
import httpx

from app import app, search_tc
from juris_164 import _scj_candidates, search_scj_candidates


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
    def test_tc_search_extracts_an_official_decision_as_unverified_candidate(self):
        class FakeResponse:
            status_code = 200
            headers = {"content-type": "text/html; charset=utf-8"}
            url = httpx.URL("https://www.tribunalconstitucional.gob.do/consultas/sentencias/")
            text = (
                '<a href="/consultas/secretaría/sentencias/tc110026">'
                "TC/1100/26</a>"
            )

            def raise_for_status(self):
                return None

        class FakeClient:
            def __init__(self, **kwargs):
                self.params = None

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def get(self, url, params):
                self.params = params
                return FakeResponse()

        with patch("app.httpx.AsyncClient", FakeClient):
            result = asyncio.run(search_tc("TC/1100/26", 2026, 5))
        self.assertEqual(result["status"], "LOCATED_RESULTS")
        self.assertEqual(result["results"][0]["decision_number"], "TC/1100/26")
        self.assertEqual(result["results"][0]["verification_status"], "CANDIDATE")
        self.assertEqual(result["results"][0]["source_status"], "NOT_VERIFIED")

    def test_tc_search_empty_html_is_not_reported_as_no_decisions(self):
        class FakeResponse:
            status_code = 200
            headers = {"content-type": "text/html"}
            url = httpx.URL("https://www.tribunalconstitucional.gob.do/consultas/sentencias/")
            text = "<html><body>No matching anchors</body></html>"

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
            result = asyncio.run(search_tc("referencia jurídica", None, 5))
        self.assertEqual(result["status"], "EXTRACTION_UNVERIFIED")
        self.assertEqual(result["results"], [])

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
        self.assertIsNone(results[0]["source_url"])
        self.assertEqual(results[0]["status"], "CANDIDATE")

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
                "responsabilidad civil", "Primera Sala", "Civil y Comercial",
                2024, 2024, 3,
            ))
        self.assertEqual(result["status"], "CANDIDATES_FOUND")
        self.assertEqual(result["results"][0]["decision_number"], "SCJ-TS-24-00123")
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0][1]["IdTribunal"], "1")
        self.assertEqual(posted[0][1]["Materia"], "2")
        self.assertEqual(posted[0][1]["Ano"], "2024")
        self.assertEqual(posted[0][1]["Contenido"], "responsabilidad civil")


if __name__ == "__main__":
    unittest.main()
