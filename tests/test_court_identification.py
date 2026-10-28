import unittest

from app import (
    annotate_foundation_attribution,
    allowed_host,
    decision_number_matches,
    detect_court,
    extract_document_text,
    find_relevant_passages,
    primary_decision_number,
)
from fastapi import HTTPException


class CourtIdentificationTests(unittest.TestCase):
    def test_tc_header_and_primary_number_ignore_scj_precedent(self):
        text = """TRIBUNAL CONSTITUCIONAL
SENTENCIA TC/0042/24
La Suprema Corte de Justicia, en la sentencia SCJ-TS-23-00012, sostuvo...
"""
        self.assertEqual(detect_court(None, text=text), "TC")
        self.assertEqual(primary_decision_number(text, "TC"), "TC/0042/24")
        self.assertTrue(decision_number_matches(text, "TC", "TC/0042/24"))
        self.assertFalse(decision_number_matches(text, "TC", "TC/0001/24"))

    def test_scj_header_and_primary_number_ignore_tc_precedent(self):
        text = """SUPREMA CORTE DE JUSTICIA
SENTENCIA SCJ-TS-24-00123
El Tribunal Constitucional, por sentencia TC/0099/22, estableció...
"""
        self.assertEqual(detect_court(None, text=text), "SCJ")
        self.assertEqual(primary_decision_number(text, "SCJ"), "SCJ-TS-24-00123")
        self.assertTrue(decision_number_matches(text, "SCJ", "SCJ-TS-24-00123"))

    def test_scj_historical_first_chamber_number(self):
        text = """SENTENCIA DEL 29 DE ENERO DE 2020, NÚM. 14
    Ordenanza impugnada: Segunda Sala de la Cámara Civil y Comercial de la Corte de Apelación del Distrito Nacional.
    Materia: Civil.
    Recurrente: Transportes Unidos C. por A.
    Recurrido: Banco Popular Dominicano, S. A.
    Juez ponente: Magistrado Napoleón R. Estévez Lavandier.
    EN NOMBRE DE LA REPÚBLICA
    La PRIMERA SALA DE LA SUPREMA CORTE DE JUSTICIA, competente para conocer recursos civiles...
La ordenanza impugnada fue dictada por la Segunda Sala de la Cámara Civil.
"""
        self.assertEqual(detect_court(None, text=text), "SCJ")
        self.assertEqual(primary_decision_number(text, "SCJ"), "NÚM. 14")
        self.assertTrue(decision_number_matches(text, "SCJ", "NÚM. 14"))
        self.assertFalse(decision_number_matches(text, "SCJ", "NÚM. 15"))

    def test_scj_party_response_and_mixed_reasoning_are_not_attributed_as_court(self):
        units = [
            {"section": "DECIDING_COURT", "text": "1) Que la parte recurrente no ha establecido la falta de base legal."},
            {"section": "DECIDING_COURT", "text": "2) Que la alzada fundamentó su decisión en el artículo 557."},
            {"section": "DECIDING_COURT", "text": "3) Que para ordenar el levantamiento la Corte a qua no incurrió en falta; esta Suprema Corte ha reiterado el criterio."},
        ]
        attributed = annotate_foundation_attribution(units, "SCJ")
        self.assertEqual(
            [unit["attribution"] for unit in attributed],
            ["PARTY_ARGUMENT_OR_MIXED_REVIEW"] * 3,
        )

    def test_cross_court_citation_before_principal_identifier(self):
        text = """TRIBUNAL CONSTITUCIONAL
    VISTO: TC/0010/20 y SCJ-TS-23-00012, citados como precedentes.
SENTENCIA TC/0042/24
"""
        self.assertEqual(detect_court(None, text=text), "TC")
        self.assertEqual(primary_decision_number(text, "TC"), "TC/0042/24")

    def test_contradictory_institutional_headings_are_unresolved(self):
        text = """TRIBUNAL CONSTITUCIONAL
SUPREMA CORTE DE JUSTICIA
SENTENCIA TC/0042/24
"""
        self.assertIsNone(detect_court(None, text=text))
        self.assertIsNone(primary_decision_number(text, "TC"))

    def test_header_conflicting_with_only_principal_identifier_is_unresolved(self):
        text = """TRIBUNAL CONSTITUCIONAL
SENTENCIA SCJ-TS-24-00123
"""
        self.assertIsNone(detect_court(None, text=text))
        self.assertIsNone(primary_decision_number(text, "TC"))
        self.assertFalse(decision_number_matches(text, "TC", "TC/0042/24"))

    def test_malformed_identifiers_do_not_match(self):
        tc_text = "TRIBUNAL CONSTITUCIONAL\nSENTENCIA TC/42/2024"
        scj_text = "SUPREMA CORTE DE JUSTICIA\nSENTENCIA SCJ-TS-24-12"
        self.assertIsNone(primary_decision_number(tc_text, "TC"))
        self.assertIsNone(primary_decision_number(scj_text, "SCJ"))
        self.assertFalse(decision_number_matches(tc_text, "TC", "TC/0042/24"))
        self.assertFalse(decision_number_matches(scj_text, "SCJ", "SCJ-TS-24-00012"))

    def test_corrupt_pdf_is_rejected(self):
        with self.assertRaises(HTTPException) as error:
            extract_document_text({"content_type": "application/pdf", "data": b"%PDF-invalid"})
        self.assertEqual(error.exception.status_code, 422)

    def test_heuristic_passages_never_verify_ratio_or_authorship(self):
        pages = [{
            "page": 1,
            "text": "LA SALA, DESPUÉS DE HABER DELIBERADO, CONSIDERA QUE:\n"
                    "1) Esta Suprema Corte establece que la interpretación requiere análisis del caso concreto.",
        }]
        passages = find_relevant_passages(pages, "interpretación jurídica", 3, "SCJ")
        self.assertTrue(passages)
        self.assertTrue(all(not passage["ratio_decidendi_verified"] for passage in passages))
        self.assertTrue(all(not passage["attribution_is_determinative"] for passage in passages))
        self.assertTrue(all(passage["review_required"] for passage in passages))

    def test_official_hosts_and_lookalikes(self):
        self.assertTrue(allowed_host("tribunalconstitucional.gob.do"))
        self.assertTrue(allowed_host("consultasentenciascj.poderjudicial.gob.do"))
        self.assertTrue(allowed_host("archivo.tribunalconstitucional.gob.do"))
        self.assertFalse(allowed_host("tribunalconstitucional.gob.do.example.com"))
        self.assertFalse(allowed_host("evilpoderjudicial.gob.do"))
        self.assertFalse(allowed_host("example.com"))
        self.assertFalse(allowed_host(None))


if __name__ == "__main__":
    unittest.main()