
from app import (
    extract_numbered_foundations,
    annotate_foundation_attribution,
)
from foundation_origin_165 import classify_contextual_origin


def test_numbered_foundation_contextual_origin():
    pages = [{
        "page": 1,
        "text": chr(10).join([
            "SUPREMA CORTE DE JUSTICIA",
            "PRIMERA SALA DE LA SUPREMA CORTE DE JUSTICIA",
            "LA SALA, DESPUÉS DE HABER DELIBERADO, CONSIDERA QUE:",
            "1) En defensa de la sentencia atacada el recurrido "
            "sostiene que la alzada valoró las pruebas.",
            "FALLA:",
            "PRIMERO: RECHAZA el recurso.",
        ])
    }]

    units = extract_numbered_foundations(pages, "SCJ")

    assert len(units) == 1
    assert units[0]["foundation_number"] == "1"

    annotated = annotate_foundation_attribution(units, "SCJ")
    result = classify_contextual_origin(annotated[0])

    assert result["reasoning_origin"] == "PARTY_ARGUMENT"
    assert result["origin_verified"] is False
    assert result["origin_review_required"] is True


def test_numbered_argument_preserves_original_text():
    unit = {
        "text": "1) En defensa de la sentencia atacada "
                "el recurrido sostiene que la alzada valoró las pruebas.",
        "attribution": "LOWER_COURT_QUOTATION_REVIEW",
        "attribution_indicators": [],
    }

    original = unit["text"]
    result = classify_contextual_origin(unit)

    assert result["reasoning_origin"] == "PARTY_ARGUMENT"
    assert unit["text"] == original
    assert result["origin_verified"] is False
