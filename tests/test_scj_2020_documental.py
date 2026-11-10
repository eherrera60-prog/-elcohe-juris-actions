
"""Prueba documental de la sentencia SCJ núm. 36, 29-01-2020."""

import io

import httpx
from pypdf import PdfReader

URL = (
    "https://transparencia.poderjudicial.gob.do/"
    "documentos/pdf/BoletinJudicialIndividual/131020036.pdf"
)

def normalize(text):
    return " ".join(text.upper().split())

def test_scj_2020_documental():
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        response = client.get(URL)

    assert response.status_code == 200
    assert response.content.startswith(b"%PDF-")
    assert response.url.host == "transparencia.poderjudicial.gob.do"

    reader = PdfReader(io.BytesIO(response.content))
    assert len(reader.pages) == 6

    pages = [
        normalize(page.extract_text() or "")
        for page in reader.pages
    ]

    first = pages[0]
    last = pages[-1]

    assert "29 DE ENERO DE 2020" in first
    assert "NÚM. 36" in first
    assert "PRIMERA SALA" in first
    assert "SUPREMA CORTE DE JUSTICIA" in first
    assert "LA COLONIAL" in first

    assert "FALTA DE BASE LEGAL" in last
    assert "FALLA:" in last

    dispositive = last.split("FALLA:", 1)[1]

    assert "PRIMERO: CASA LA SENTENCIA" in dispositive
    assert "SEGUNDO: COMPENSA LAS COSTAS" in dispositive
