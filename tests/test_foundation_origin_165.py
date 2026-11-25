
"""Pruebas de clasificación conservadora de fundamentos."""

from foundation_origin_165 import classify_reasoning_origin


def test_lower_court():
    result = classify_reasoning_origin({
        "attribution": "LOWER_COURT_QUOTATION_REVIEW",
        "attribution_indicators": ["within_lower_court_quotation_scope"],
    })
    assert result["reasoning_origin"] == "LOWER_COURT_QUOTATION"
    assert result["origin_review_required"] is True
    assert result["origin_verified"] is False


def test_party_argument():
    result = classify_reasoning_origin({
        "attribution": "PARTY_ARGUMENT_OR_MIXED_REVIEW",
    })
    assert result["reasoning_origin"] == "PARTY_ARGUMENT"
    assert result["origin_verified"] is False


def test_undetermined():
    result = classify_reasoning_origin({
        "attribution": "DECIDING_COURT_SECTION_REVIEW",
    })
    assert result["reasoning_origin"] == "UNDETERMINED"
    assert result["origin_review_required"] is True


def test_no_attribution():
    result = classify_reasoning_origin({})
    assert result["reasoning_origin"] == "UNDETERMINED"


def test_indicators_preserved():
    result = classify_reasoning_origin({
        "attribution": "LOWER_COURT_QUOTATION_REVIEW",
        "attribution_indicators": ["explicit_quote"],
    })
    assert result["origin_classification_indicators"] == ["explicit_quote"]
