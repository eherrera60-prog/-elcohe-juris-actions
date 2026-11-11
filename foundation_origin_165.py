
"""Clasificación conservadora del origen de fundamentos candidatos."""

def classify_reasoning_origin(unit):
    attribution = unit.get("attribution") or ""
    indicators = unit.get("attribution_indicators") or []

    if attribution.startswith("LOWER_COURT"):
        origin = "LOWER_COURT_QUOTATION"
    elif attribution.startswith("PARTY_ARGUMENT"):
        origin = "PARTY_ARGUMENT"
    elif attribution.startswith("NON_DECIDING"):
        origin = "NON_DECIDING_SECTION"
    elif attribution.startswith("CITED_PRECEDENT"):
        origin = "CITED_PRECEDENT"
    else:
        origin = "UNDETERMINED"

    return {
        "reasoning_origin": origin,
        "origin_review_required": True,
        "origin_classification_indicators": list(indicators),
        "origin_verified": False,
    }


def classify_contextual_origin(unit):
    """Refina la atribución sin certificar autoría judicial."""
    import re

    text = " ".join((unit.get("text") or "").split())
    normalized = text.lower()
    normalized = re.sub(
        r"^\s*\d{1,3}\s*[).]\s*",
        "",
        normalized
    )

    base = classify_reasoning_origin(unit)
    indicators = list(base["origin_classification_indicators"])

    rules = [
        (
            r"^procede examinar reunidos.*medios de casaci[oó]n",
            "PARTY_ARGUMENT",
            "cassation_arguments_introduction",
        ),
        (
            r"^en defensa de la sentencia atacada.*recurrido sostiene",
            "PARTY_ARGUMENT",
            "respondent_arguments",
        ),
        (
            r"^el contrato de seguros est[aá] definido.*ley",
            "STATUTORY_QUOTATION",
            "explicit_statutory_definition",
        ),
        (
            r"^la alzada para acoger.*indic[oó].*siguiente",
            "LOWER_COURT_QUOTATION",
            "explicit_lower_court_reasoning",
        ),
    ]

    for pattern, origin, indicator in rules:
        if re.search(pattern, normalized, re.I):
            base["reasoning_origin"] = origin
            base["origin_classification_indicators"] = (
                [x for x in indicators
                 if x != "within_lower_court_quotation_scope"]
                + [indicator]
            )
            break

    base["origin_verified"] = False
    base["origin_review_required"] = True

    return base
