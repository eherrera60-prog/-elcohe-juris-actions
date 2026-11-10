
from document_continuity_165 import identify_page_continuities


def test_continuity_preserves_original_units():
    units = [
        {
            "page": 5,
            "text": "Se encuentran los elementos constitutivos de la"
        },
        {
            "page": 6,
            "text": "responsabilidad civil contractual."
        }
    ]

    original = [dict(x) for x in units]
    result = identify_page_continuities(units)

    assert len(result) == 1
    assert result[0]["start_page"] == 5
    assert result[0]["end_page"] == 6
    assert result[0]["human_review_required"] is True
    assert result[0]["ratio_decidendi_verified"] is False
    assert units == original
