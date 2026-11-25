
from foundation_origin_165 import classify_contextual_origin


def classify(text, attribution="LOWER_COURT_QUOTATION_REVIEW"):
    return classify_contextual_origin({
        "text": text,
        "attribution": attribution,
        "attribution_indicators": [],
    })


def test_appellant_arguments_not_lower_court():
    result = classify(
        "Procede examinar reunidos por su estrecha vinculación "
        "los medios de casación planteados por el recurrente."
    )
    assert result["reasoning_origin"] == "PARTY_ARGUMENT"


def test_respondent_arguments_not_lower_court():
    result = classify(
        "En defensa de la sentencia atacada el recurrido sostiene "
        "que la alzada valoró las pruebas."
    )
    assert result["reasoning_origin"] == "PARTY_ARGUMENT"


def test_statutory_quote_not_lower_court():
    result = classify(
        "El contrato de seguros está definido en el Art. 1 "
        "de la Ley núm. 146-02 sobre Seguros y Fianzas."
    )
    assert result["reasoning_origin"] == "STATUTORY_QUOTATION"


def test_lower_court_quote_retained():
    result = classify(
        "La alzada para acoger el incumplimiento contractual "
        "indicó fundamentalmente lo siguiente."
    )
    assert result["reasoning_origin"] == "LOWER_COURT_QUOTATION"


def test_no_automatic_verification():
    result = classify(
        "Esta Sala ha verificado la falta de base legal.",
        "POSSIBLE_JUDICIAL_REASONING_REVIEW",
    )
    assert result["origin_verified"] is False
    assert result["origin_review_required"] is True
