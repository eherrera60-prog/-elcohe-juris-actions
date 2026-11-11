
def identify_page_continuities(units):
    """
    Detecta continuidad textual entre páginas.

    No altera los fundamentos originales ni determina
    autoría judicial o ratio decidendi.
    """
    results = []

    for index in range(len(units) - 1):
        left = units[index]
        right = units[index + 1]

        if left.get("page") == right.get("page"):
            continue

        left_text = (left.get("text") or "").strip()
        right_text = (right.get("text") or "").strip()

        if not left_text or not right_text:
            continue

        # La continuidad debe estar apoyada en un indicio
        # sintáctico concreto. No se fusionan unidades.
        unfinished = (
            left_text.rstrip().lower().endswith(
                (" de la", " de los", " de las", " del",
                 " para la", " para el", " y la", " y el")
            )
        )

        if not unfinished:
            continue

        results.append({
            "unit_before": index + 1,
            "unit_after": index + 2,
            "start_page": left.get("page"),
            "end_page": right.get("page"),
            "continuity_candidate": True,
            "text_before": left_text,
            "text_after": right_text,
            "human_review_required": True,
            "judicial_authorship_verified": False,
            "ratio_decidendi_verified": False,
        })

    return results


def merge_verified_page_continuities(units):
    """Une continuidades sintácticas candidatas sin verificar autoría."""
    from copy import deepcopy

    merged = deepcopy(units)
    index = 0

    while index < len(merged) - 1:
        left = merged[index]
        right = merged[index + 1]

        candidates = identify_page_continuities([left, right])

        compatible = (
            bool(candidates)
            and left.get("section") == right.get("section")
            and left.get("foundation_number") is None
            and right.get("foundation_number") is None
            and left.get("historical_unnumbered") is True
            and right.get("historical_unnumbered") is True
            and right.get("page") == left.get("end_page", left.get("page")) + 1
        )

        if not compatible:
            index += 1
            continue

        left["text"] = (
            left["text"].rstrip() + " " +
            right["text"].lstrip()
        )
        left["end_page"] = right.get("end_page", right["page"])
        left["page_continuity_merged"] = True
        left["review_required"] = True
        merged.pop(index + 1)

    return merged
