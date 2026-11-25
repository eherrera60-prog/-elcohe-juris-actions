
"""Pruebas de atribución contextual ELCOHE-JURIS RD 1.6.5."""

import pytest

from app import annotate_foundation_attribution
from foundation_origin_165 import classify_reasoning_origin


def classify(texts, court="SCJ"):
    units = [
        {
            "text": text,
            "section": "DECIDING_COURT",
            "foundation_number": index,
            "page": 1,
        }
        for index, text in enumerate(texts, 1)
    ]

    annotated = annotate_foundation_attribution(units, court)

    return [
        {**unit, **classify_reasoning_origin(unit)}
        for unit in annotated
    ]


def test_lower_court_quotation_scope():
    result = classify([
        "La sentencia impugnada transcribe textualmente lo siguiente:",
        "La corte a qua consideró procedente la demanda.",
        "El recurrente pretende obtener la casación de la sentencia.",
    ])

    assert result[0]["reasoning_origin"] == "LOWER_COURT_QUOTATION"
    assert result[1]["reasoning_origin"] == "LOWER_COURT_QUOTATION"
    assert result[2]["reasoning_origin"] != "LOWER_COURT_QUOTATION"


def test_party_argument_is_not_verified_judicial_reasoning():
    result = classify([
        "El recurrente pretende obtener la casación de la sentencia.",
    ])

    assert result[0]["origin_verified"] is False
    assert result[0]["origin_review_required"] is True


def test_deciding_court_section_does_not_prove_authorship():
    result = classify([
        "Esta Primera Sala examina los argumentos expuestos.",
    ])

    assert result[0]["origin_verified"] is False
    assert result[0]["origin_review_required"] is True


def test_tc_requires_individual_review():
    result = classify([
        "El Tribunal Constitucional examina los argumentos presentados.",
    ], court="TC")

    assert result[0]["origin_verified"] is False
    assert result[0]["origin_review_required"] is True


def test_mixed_precedent_remains_unverified():
    result = classify([
        "Esta Primera Sala cita una sentencia anterior y examina "
        "los argumentos del recurrente.",
    ])

    assert result[0]["origin_verified"] is False
    assert result[0]["origin_review_required"] is True


def test_non_deciding_section():
    unit = {
        "text": "El recurrente solicita la casación.",
        "section": "PARTY_SUBMISSIONS",
    }

    annotated = annotate_foundation_attribution([unit], "SCJ")
    result = classify_reasoning_origin(annotated[0])

    assert result["reasoning_origin"] == "NON_DECIDING_SECTION"
    assert result["origin_verified"] is False
