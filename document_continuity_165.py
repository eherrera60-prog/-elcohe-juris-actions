
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
