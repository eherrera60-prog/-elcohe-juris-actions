from typing import Annotated

from pydantic import Field


HISTORICAL_SCJ_NUMBER_LABEL = (
    r"(?:N\s*[ÚU]M(?:ERO)?\.?|N\s*\.?\s*[Ooº°]\.?)"
)
HISTORICAL_SCJ_NUMBER_SOURCE = (
    rf"{HISTORICAL_SCJ_NUMBER_LABEL}\s*\d{{1,5}}"
)

OFFICIAL_DECISION_NUMBER_PATTERN = (
    r"^(?:"
    r"(?:"
    r"(?:N\s*[ÚU]M(?:ERO)?\.?|N\s*\.?\s*[Ooº°]\.?)\s*)?"
    r"\d{1,5}"
    r"|"
    r"(?:[Nn][ÚúUu][Mm]\.?[ \t]*)?\d{1,5}"
    r"|[Tt][Cc]/\d{1,4}/\d{2}"
    r"|[Ss][Cc][Jj](?:[ \t\r\n]{0,12}[-\u00ad\u2010-\u2014\u2212]"
    r"[ \t\r\n]{0,12}|[ \t\r\n]{1,12})[A-Za-z]{2}"
    r"(?:[ \t\r\n]{0,12}[-\u00ad\u2010-\u2014\u2212]"
    r"[ \t\r\n]{0,12}|[ \t\r\n]{1,12})(?:\d{2}|\d{4})"
    r"(?:[ \t\r\n]{0,12}[-\u00ad\u2010-\u2014\u2212]"
    r"[ \t\r\n]{0,12}|[ \t\r\n]{1,12})\d{3,5}"
    r")$"
)

OfficialDecisionNumber = Annotated[
    str,
    Field(
        min_length=1,
        max_length=80,
        pattern=OFFICIAL_DECISION_NUMBER_PATTERN,
    ),
]
