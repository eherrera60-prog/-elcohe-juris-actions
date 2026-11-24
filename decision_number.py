from typing import Annotated

from pydantic import Field


OFFICIAL_DECISION_NUMBER_PATTERN = (
    r"^(?:"
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
