
from document_continuity_165 import identify_page_continuities


def test_continuity_across_pages():
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

    original = [dict(item) for item in units]
    result = identify_page_continuities(units)

    assert len(result) == 1
    assert result[0]["start_page"] == 5
    assert result[0]["end_page"] == 6
    assert result[0]["human_review_required"] is True
    assert result[0]["judicial_authorship_verified"] is False
    assert result[0]["ratio_decidendi_verified"] is False
    assert units == original


def test_separate_party_arguments():
    units = [
        {
            "page": 4,
            "text": "El recurrente solicita casar la sentencia."
        },
        {
            "page": 5,
            "text": "En defensa de la sentencia el recurrido sostiene."
        }
    ]

    assert identify_page_continuities(units) == []
