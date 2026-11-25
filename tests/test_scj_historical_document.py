"""Documental validation for historical SCJ headings and unnumbered reasons."""

import hashlib

import httpx

from app import extract_document_text
from juris_164 import FoundationExtractionRequest, _foundation_payload


URL = (
    "https://transparencia.poderjudicial.gob.do/"
    "documentos/pdf/BoletinJudicialIndividual/105830002.pdf"
)


def test_real_historical_scj_pdf_identity_and_unnumbered_foundations():
    response = httpx.get(URL, timeout=60, follow_redirects=True)
    response.raise_for_status()
    assert response.url.host == "transparencia.poderjudicial.gob.do"
    assert response.content.startswith(b"%PDF-")

    doc = {
        "host": response.url.host,
        "final_url": str(response.url),
        "data": response.content,
        "sha256": hashlib.sha256(response.content).hexdigest(),
    }
    extracted = extract_document_text({
        "content_type": response.headers.get("content-type", "application/pdf"),
        "data": response.content,
    })
    request = FoundationExtractionRequest(
        url=URL,
        expected_court="SCJ",
        expected_number="No. 2",
        limit=30,
    )

    result = _foundation_payload(request, doc, extracted)

    assert extracted["page_count"] == 5
    assert extracted["extraction_ok"] is True
    assert result["identity_status"] == "VERIFIED_IDENTITY"
    assert result["metadata"]["decision_number"] == "NÚM. 2"
    assert result["metadata"]["chamber"] == "Cámara Penal"
    assert result["metadata"]["date"] == "14 de enero de 1999"
    assert result["passage_status"] == "VERIFIED_PASSAGE"
    assert result["foundations"]
    assert any(
        item["reasoning_origin"] == "PARTY_ARGUMENT"
        for item in result["foundations"]
    )
    assert any(
        item["reasoning_origin"] == "UNDETERMINED"
        for item in result["foundations"]
    )
    assert all(
        item["ratio_decidendi_verified"] is False
        and item["attribution_verified"] is False
        and item["review_required"] is True
        for item in result["foundations"]
    )
    assert all(
        item["context_pages"]
        and item["page"] in item["context_pages"]
        for item in result["foundations"]
    )
    assert all(
        not item["passage"].startswith("Por tales motivos")
        for item in result["foundations"]
    )
    assert result["dispositive_status"] == "VERIFIED_PASSAGE"
    assert result["dispositive"]["page"] == 4
    assert "Por tales motivos, Primero:" in result["dispositive"]["text"]
    assert result["dispositive"]["legal_effect_verified"] is False
