
from mcp_server import _rank_ratio_candidates


def test_party_allegations_are_not_ratio_candidates():
    evidence = {
        "dispositive": {
            "status": "VERIFIED_PASSAGE",
            "text": "Acoger el recurso y anular la sentencia."
        },
        "foundations": [{
            "status": "VERIFIED_PASSAGE",
            "attribution": "DECIDING_COURT_SECTION_REVIEW",
            "reasoning_origin": "UNDETERMINED",
            "passage": (
                "9.6 En el presente caso, el recurso se fundamenta "
                "en violacion al derecho de defensa y vulneracion "
                "a la tutela judicial efectiva. Por tanto solicita "
                "anular la sentencia impugnada."
            ),
        }],
    }
    assert not _rank_ratio_candidates(
        evidence, "tutela judicial efectiva"
    )


from app import strip_running_furniture, extract_dispositive_passage


def test_document_furniture_cleanup_165():
    text = (
        "TRIBUNAL CONSTITUCIONAL\n"
        "El tribunal considera procedente la solicitud.\n"
        "Página 20 de 34\n"
        "SCJ-PS-23-2229 (809) 533-3191 • "
        "Dirección de Internet: http://www.poderjudicial.gob.do"
    )
    cleaned = strip_running_furniture(text)
    assert "El tribunal considera procedente la solicitud." in cleaned
    assert "Página 20 de 34" not in cleaned
    assert "TRIBUNAL CONSTITUCIONAL" not in cleaned
    assert "533-3191" not in cleaned


def test_dispositive_continuation_165():
    pages = [
        {
            "page": 33,
            "text": "DECIDE:\nPRIMERO: Acoger el recurso."
        },
        {
            "page": 34,
            "text": (
                "SEGUNDO: Anular la sentencia impugnada.\n"
                "TERCERO: Ordenar la comunicacion.\n"
                "Página 34 de 34"
            )
        },
    ]
    result = extract_dispositive_passage(pages)
    assert result is not None
    assert result["page"] == 33
    assert result["end_page"] == 34
    assert "SEGUNDO:" in result["text"]
    assert "TERCERO:" in result["text"]
    assert "Página 34 de 34" not in result["text"]
