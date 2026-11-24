import asyncio
import hashlib
import os

import pytest

TC_PDF_URL = (
    "https://tribunalsitestorage.blob.core.windows.net/"
    "media/53968/tc-0019-24-tc-04-2023-0274.pdf"
)
SCJ_PDF_URL = (
    "https://transparencia.poderjudicial.gob.do/"
    "documentos/pdf/BoletinJudicialIndividual/131020036.pdf"
)


def _authorized_isolated_test_database() -> bool:
    if (
        os.getenv("RENDER_SERVICE_NAME") != "elcohe-juris-163-test"
        or os.getenv("ELCOHE_DB_TEST_ISOLATED") != "true"
        or os.getenv("ELCOHE_PERSISTENCE_TEST_ONCE") != "true"
        or not os.getenv("DATABASE_URL")
    ):
        return False
    try:
        from psycopg.conninfo import conninfo_to_dict

        connection = conninfo_to_dict(os.environ["DATABASE_URL"])
    except (ImportError, ValueError):
        return False
    return (
        connection.get("host") in {
            "dpg-dbi0vsei0phs73ch9pdg-a",
            "dpg-dbi0vsei0phs73ch9pdg-a.oregon-postgres.render.com",
        }
        and connection.get("dbname") == "elcohe_juris_164_db"
        and connection.get("user") == "elcohe_juris_nuevo"
    )


@pytest.mark.skipif(
    not _authorized_isolated_test_database(),
    reason="PostgreSQL real requiere autorización explícita del entorno de pruebas aislado.",
)
@pytest.mark.parametrize(
    ("court", "number", "url"),
    [
        ("TC", "TC/0019/24", TC_PDF_URL),
        ("SCJ", "NÚM. 36", SCJ_PDF_URL),
    ],
)
def test_authorized_postgres_persists_and_recovers_real_verified_pdf(
    court, number, url
):
    from app import fetch_document
    from registry import (
        RegisterRequest,
        TCReferenceRequest,
        connect,
        find_verified_decisions,
        get_verified_document,
        init_schema,
        register_decision,
        register_tc_reference,
    )

    document = asyncio.run(fetch_document(url))
    digest = hashlib.sha256(document["data"]).hexdigest()
    with connect() as connection:
        init_schema(connection)
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id FROM elcohe_decisions
                WHERE court=%s AND decision_number=%s AND sha256=%s
                """,
                (court, number, digest),
            )
            if cursor.fetchone() is not None:
                pytest.skip(
                    "El registro ya existía antes de esta prueba; no se modifica."
                )

    if court == "TC":
        request = TCReferenceRequest(
            url=url,
            expected_number=number,
            legal_issue="integración documental de prueba",
        )
        register = register_tc_reference
    else:
        request = RegisterRequest(
            url=url,
            expected_court=court,
            expected_number=number,
            legal_issue="integración documental de prueba",
        )
        register = register_decision

    first = asyncio.run(register(request))
    second = asyncio.run(register(request))
    assert second["id"] == first["id"]
    if court == "TC":
        assert second["deduplicated"] is True
    else:
        assert second["status"] == "STORED_VERIFIED_SOURCE"

    matches = find_verified_decisions(number, court=court)
    record = next(item for item in matches if item["id"] == first["id"])
    recovered = get_verified_document(first["id"])
    assert record["document_bytes_persisted"] is True
    assert recovered["identity_status"] == "VERIFIED_IDENTITY"
    assert recovered["sha256"] == digest
    assert hashlib.sha256(recovered["document_bytes"]).hexdigest() == digest
