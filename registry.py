"""Registro PostgreSQL de decisiones verificadas; no certifica ratio decidendi."""
from __future__ import annotations
import hashlib
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, HttpUrl
from auth_access import (
    connect_db as connect,
    require_active_user,
    require_admin,
)

router = APIRouter(prefix="/v1/registry", tags=["Registro jurisprudencial"])
require_read_access = require_active_user

class RegisterRequest(BaseModel):
    url: HttpUrl
    expected_court: str = Field(pattern=r"^(SCJ|TC)$")
    expected_number: str = Field(min_length=4, max_length=80)
    legal_issue: str | None = Field(default=None, max_length=500)
    max_passages: int = Field(default=6, ge=1, le=12)


class TCReferenceRequest(BaseModel):
    url: HttpUrl
    expected_number: str = Field(pattern=r"^TC/\d{1,4}/\d{2}$")
    legal_issue: str | None = Field(default=None, min_length=3, max_length=300)


SCHEMA = """
CREATE TABLE IF NOT EXISTS elcohe_decisions (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    court TEXT NOT NULL CHECK (court IN ('SCJ','TC')),
    decision_number TEXT NOT NULL,
    source_url TEXT NOT NULL,
    sha256 CHAR(64) NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    chamber TEXT,
    decision_date TEXT,
    case_number TEXT,
    legal_issue TEXT,
    verification_status TEXT NOT NULL CHECK (verification_status IN ('VERIFIED_SOURCE','HOLDING_CANDIDATE')),
    review_required BOOLEAN NOT NULL DEFAULT TRUE,
    ratio_decidendi_verified BOOLEAN NOT NULL DEFAULT FALSE,
    relevant_passages JSONB NOT NULL DEFAULT '[]'::jsonb,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    extraction_diagnostics JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (court, decision_number, sha256)
);
CREATE INDEX IF NOT EXISTS idx_elcohe_decisions_number ON elcohe_decisions(court, decision_number);
CREATE TABLE IF NOT EXISTS elcohe_decision_documents (
    decision_id BIGINT PRIMARY KEY REFERENCES elcohe_decisions(id) ON DELETE RESTRICT,
    sha256 CHAR(64) NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    content_type TEXT NOT NULL CHECK (content_type = 'application/pdf'),
    page_count INTEGER NOT NULL CHECK (page_count > 0),
    document_bytes BYTEA NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


def init_schema(conn):
    with conn.cursor() as cur:
        for statement in SCHEMA.split(";"):
            if statement.strip():
                cur.execute(statement)


def row_to_dict(row: tuple, fields: list[str]) -> dict[str, Any]:
    return dict(zip(fields, row))


def persist_verified_pdf(
    cursor,
    decision_id: int,
    data: bytes,
    digest: str,
    page_count: int,
) -> None:
    if (
        not isinstance(data, bytes)
        or not data.startswith(b"%PDF")
        or hashlib.sha256(data).hexdigest() != digest
        or page_count < 1
    ):
        raise HTTPException(422, "No se puede persistir un PDF sin integridad verificada.")
    cursor.execute(
        """
        INSERT INTO elcohe_decision_documents
            (decision_id,sha256,content_type,page_count,document_bytes)
        VALUES (%s,%s,'application/pdf',%s,%s)
        ON CONFLICT (decision_id) DO UPDATE SET
            sha256=EXCLUDED.sha256,
            content_type=EXCLUDED.content_type,
            page_count=EXCLUDED.page_count,
            document_bytes=EXCLUDED.document_bytes
        """,
        (decision_id, digest, page_count, data),
    )


def _is_official_tc_host(host: str | None) -> bool:
    if not host:
        return False
    normalized = host.lower().strip(".")
    return (
        normalized == "tribunalconstitucional.gob.do"
        or normalized.endswith(".tribunalconstitucional.gob.do")
        or normalized == "tribunalsitestorage.blob.core.windows.net"
    )


async def register_tc_reference(req: TCReferenceRequest):
    """Fetch and persist a verified official TC PDF without certifying its ratio."""
    from app import (
        _tc_number_parts,
        court_matches,
        decision_number_matches,
        detect_court,
        extract_document_text,
        extract_metadata_from_text,
        fetch_document,
    )
    from psycopg.types.json import Jsonb

    source_url = str(req.url)
    if not _is_official_tc_host(urlparse(source_url).hostname):
        raise HTTPException(422, "La fuente debe pertenecer al TC o a su almacenamiento oficial.")
    number_parts = _tc_number_parts(req.expected_number)
    if number_parts is None:
        raise HTTPException(422, "Se requiere un identificador de sentencia TC válido.")
    canonical_number = number_parts[0]

    document = await fetch_document(source_url)
    if not _is_official_tc_host(document.get("host")):
        raise HTTPException(422, "La descarga redirigió fuera de las fuentes oficiales del TC.")
    data = document.get("data")
    if not isinstance(data, bytes) or not data.startswith(b"%PDF"):
        raise HTTPException(415, "La fuente oficial no devolvió bytes PDF.")

    extracted = extract_document_text(document)
    pages = extracted.get("pages")
    page_count = extracted.get("page_count")
    if (
        extracted.get("document_type") != "PDF"
        or not extracted.get("extraction_ok")
        or not isinstance(pages, list)
        or not isinstance(page_count, int)
        or page_count < 1
        or len(pages) != page_count
    ):
        raise HTTPException(
            422,
            "El PDF oficial no permitió verificar texto extraído y número de páginas.",
        )

    digest = hashlib.sha256(data).hexdigest()
    if document.get("sha256") != digest:
        raise HTTPException(422, "La huella del PDF no coincide con los bytes recibidos.")
    full_text = extracted.get("full_text") or ""
    court = detect_court(document.get("host"), "TC", full_text)
    if not court_matches(court, "TC"):
        raise HTTPException(422, "El contenido del PDF no identifica al Tribunal Constitucional.")
    if not decision_number_matches(full_text, "TC", canonical_number):
        raise HTTPException(422, "El número de sentencia no coincide con el contenido del PDF.")

    metadata = extract_metadata_from_text(full_text, "TC")
    metadata.update({
        "decision_number": canonical_number,
        "document_type": "PDF",
        "page_count": page_count,
        "text_extraction_ok": True,
        "source_sha256": digest,
    })
    diagnostics = {
        "document_bytes": len(data),
        "extracted_text_characters": len(full_text),
        "page_count": page_count,
        "document_type": "PDF",
        "integrity_verified": True,
    }

    try:
        with connect() as conn:
            init_schema(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO elcohe_decisions
                        (court,decision_number,source_url,sha256,decision_date,
                         legal_issue,verification_status,review_required,
                         ratio_decidendi_verified,relevant_passages,metadata,
                         extraction_diagnostics)
                    VALUES ('TC',%s,%s,%s,%s,%s,'VERIFIED_SOURCE',TRUE,FALSE,
                            '[]'::jsonb,%s,%s)
                    ON CONFLICT (court,decision_number,sha256) DO NOTHING
                    RETURNING id,created_at
                    """,
                    (
                        canonical_number,
                        document["final_url"],
                        digest,
                        metadata.get("date"),
                        req.legal_issue,
                        Jsonb(metadata),
                        Jsonb(diagnostics),
                    ),
                )
                stored = cur.fetchone()
                if stored is None:
                    cur.execute(
                        """
                        SELECT id,created_at FROM elcohe_decisions
                        WHERE court='TC' AND decision_number=%s AND sha256=%s
                        """,
                        (canonical_number, digest),
                    )
                    stored = cur.fetchone()
                    if stored is None:
                        raise RuntimeError("No se pudo recuperar la referencia documental duplicada.")
                    cur.execute(
                        """
                        UPDATE elcohe_decisions
                        SET source_url=%s,
                            legal_issue=COALESCE(legal_issue,%s),
                            verification_status='VERIFIED_SOURCE',
                            review_required=TRUE,
                            ratio_decidendi_verified=FALSE,
                            metadata=metadata || %s,
                            extraction_diagnostics=extraction_diagnostics || %s,
                            updated_at=NOW()
                        WHERE id=%s
                        """,
                        (
                            document["final_url"],
                            req.legal_issue,
                            Jsonb(metadata),
                            Jsonb(diagnostics),
                            stored[0],
                        ),
                    )
                    persist_verified_pdf(
                        cur, stored[0], data, digest, page_count
                    )
                    status = "ALREADY_VERIFIED_SOURCE"
                    deduplicated = True
                else:
                    persist_verified_pdf(
                        cur, stored[0], data, digest, page_count
                    )
                    status = "STORED_VERIFIED_SOURCE"
                    deduplicated = False
        return {
            "status": status,
            "id": stored[0],
            "court": "TC",
            "decision_number": canonical_number,
            "source_url": document["final_url"],
            "document_type": "PDF",
            "page_count": page_count,
            "text_extraction_ok": True,
            "sha256_pdf": digest,
            "deduplicated": deduplicated,
            "review_required": True,
            "ratio_decidendi_verified": False,
            "created_at": stored[1],
            "message": (
                "Se verificaron fuente oficial, integridad, tribunal, número, "
                "texto y páginas. La ratio decidendi no fue certificada."
            ),
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            503, f"Error al guardar la referencia verificada ({type(exc).__name__})."
        ) from exc


def find_verified_tc_references(query: str, limit: int = 10) -> list[dict]:
    """Return previously verified TC documents matching an identifier or topic."""
    from app import _tc_number_parts

    number_parts = _tc_number_parts(query)
    if number_parts:
        conditions = "court='TC' AND decision_number=%s"
        parameters: list[Any] = [number_parts[0]]
    else:
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        conditions = "court='TC' AND legal_issue ILIKE %s"
        parameters = ["%" + escaped + "%"]
    try:
        with connect() as conn:
            init_schema(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id,court,decision_number,source_url,sha256,legal_issue,
                           verification_status,review_required,ratio_decidendi_verified,
                           metadata,extraction_diagnostics,created_at
                    FROM elcohe_decisions
                    WHERE """ + conditions + """
                      AND metadata->>'document_type'='PDF'
                      AND EXISTS (
                          SELECT 1 FROM elcohe_decision_documents doc
                          WHERE doc.decision_id=elcohe_decisions.id
                            AND doc.sha256=elcohe_decisions.sha256
                      )
                    ORDER BY created_at DESC LIMIT %s
                    """,
                    parameters + [limit],
                )
                fields = [column.name for column in cur.description]
                items = [row_to_dict(row, fields) for row in cur.fetchall()]
        for item in items:
            item["retrieval_mode"] = "PREVIOUSLY_VERIFIED_DOCUMENT"
            item["document_bytes_persisted"] = True
            item["official_document_verified"] = True
            item["identity_status"] = "VERIFIED_IDENTITY"
        return items
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            503, f"Error al consultar referencias verificadas ({type(exc).__name__})."
        ) from exc


def find_verified_decisions(
    query: str,
    court: str | None = None,
    limit: int = 10,
) -> list[dict]:
    """Find verified PDF references with their persisted bytes available."""
    normalized_query = query.strip()
    if not normalized_query:
        raise HTTPException(422, "La consulta no puede estar vacía.")
    if court is not None and court not in {"SCJ", "TC"}:
        raise HTTPException(422, "El tribunal debe ser SCJ o TC.")
    if not 1 <= limit <= 100:
        raise HTTPException(422, "El límite debe estar entre 1 y 100.")

    conditions = [
        "d.verification_status IN ('VERIFIED_SOURCE','HOLDING_CANDIDATE')",
        "COALESCE(d.metadata->>'document_type', "
        "d.extraction_diagnostics->>'document_type') = 'PDF'",
        "EXISTS (SELECT 1 FROM elcohe_decision_documents doc "
        "WHERE doc.decision_id=d.id AND doc.sha256=d.sha256)",
    ]
    parameters: list[Any] = []
    if court:
        conditions.append("d.court=%s")
        parameters.append(court)

    escaped = (
        normalized_query.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    conditions.append(
        "(d.decision_number ILIKE %s ESCAPE '\\' "
        "OR COALESCE(d.legal_issue,'') ILIKE %s ESCAPE '\\' "
        "OR COALESCE(d.chamber,'') ILIKE %s ESCAPE '\\')"
    )
    pattern = f"%{escaped}%"
    parameters.extend((pattern, pattern, pattern, limit))

    try:
        with connect() as conn:
            init_schema(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT d.id,d.court,d.decision_number,d.source_url,d.sha256,
                           d.chamber,d.decision_date,d.case_number,d.legal_issue,
                           d.verification_status,d.review_required,
                           d.ratio_decidendi_verified,d.metadata,
                           d.extraction_diagnostics,d.created_at
                    FROM elcohe_decisions d
                    WHERE """ + " AND ".join(conditions) + """
                    ORDER BY d.created_at DESC LIMIT %s
                    """,
                    parameters,
                )
                fields = [column.name for column in cur.description]
                items = [row_to_dict(row, fields) for row in cur.fetchall()]
        for item in items:
            item["retrieval_mode"] = "PREVIOUSLY_VERIFIED_DOCUMENT"
            item["document_bytes_persisted"] = True
            item["official_document_verified"] = True
            item["identity_status"] = "VERIFIED_IDENTITY"
        return items
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            503, f"Error al consultar decisiones verificadas ({type(exc).__name__})."
        ) from exc


def get_verified_document(decision_id: int) -> dict:
    """Retrieve stored PDF bytes only when their hash and verified identity agree."""
    if decision_id < 1:
        raise HTTPException(422, "Identificador inválido.")
    from app import (
        allowed_host,
        court_matches,
        decision_number_matches,
        detect_court,
        extract_document_text,
    )

    try:
        with connect() as conn:
            init_schema(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT d.id,d.court,d.decision_number,d.source_url,d.sha256,
                           d.verification_status,d.review_required,
                           d.ratio_decidendi_verified,d.metadata,
                           d.extraction_diagnostics,
                           doc.content_type,doc.page_count,doc.document_bytes
                    FROM elcohe_decisions d
                    JOIN elcohe_decision_documents doc ON doc.decision_id=d.id
                    WHERE d.id=%s
                      AND d.verification_status IN ('VERIFIED_SOURCE','HOLDING_CANDIDATE')
                      AND doc.sha256=d.sha256
                    """,
                    (decision_id,),
                )
                row = cur.fetchone()
                if row is None:
                    raise HTTPException(404, "Documento verificado no encontrado.")
                fields = [column.name for column in cur.description]
                result = row_to_dict(row, fields)
        data = result.pop("document_bytes")
        parsed_source = urlparse(result["source_url"])
        if (
            result["content_type"] != "application/pdf"
            or not isinstance(data, bytes)
            or not data.startswith(b"%PDF")
            or hashlib.sha256(data).hexdigest() != result["sha256"]
            or parsed_source.scheme != "https"
            or not allowed_host(parsed_source.hostname)
        ):
            raise HTTPException(
                422,
                "La fuente oficial o integridad del PDF almacenado no pudo verificarse.",
            )
        extracted = extract_document_text({
            "data": data,
            "content_type": result["content_type"],
            "host": parsed_source.hostname,
        })
        detected_court = detect_court(
            parsed_source.hostname,
            result["court"],
            extracted["full_text"],
        )
        if (
            not extracted["extraction_ok"]
            or extracted["page_count"] != result["page_count"]
            or not court_matches(detected_court, result["court"])
            or not decision_number_matches(
                extracted["full_text"],
                detected_court,
                result["decision_number"],
            )
        ):
            raise HTTPException(
                422,
                "La identidad del documento almacenado no coincide con su registro.",
            )
        result.update({
            "document_bytes": data,
            "document_type": "PDF",
            "source_status": "VERIFIED_SOURCE",
            "official_document_verified": True,
            "identity_status": "VERIFIED_IDENTITY",
            "document_recovered": True,
        })
        return result
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            503, f"Error al recuperar el documento verificado ({type(exc).__name__})."
        ) from exc


@router.post(
    "/tc-references",
    operation_id="registerVerifiedTcReference",
    dependencies=[Depends(require_admin)],
    include_in_schema=False,
)
async def register_verified_tc_reference(req: TCReferenceRequest):
    return await register_tc_reference(req)


@router.post(
    "/decisions",
    operation_id="registerVerifiedDecision",
    dependencies=[Depends(require_admin)],
    include_in_schema=False,
)
async def register_decision(req: RegisterRequest):
    # Importación diferida: reutiliza exactamente el verificador de la aplicación original.
    from app import AnalyzeDecisionRequest, _analyze_official_decision
    from psycopg.types.json import Jsonb

    analysis = await _analyze_official_decision(
        AnalyzeDecisionRequest(**req.model_dump()),
        include_document_bytes=True,
    )
    if not analysis.get("official_source_verified") or analysis.get("status") not in ("VERIFIED_SOURCE", "HOLDING_CANDIDATE"):
        raise HTTPException(422, "Documento no verificado; no se almacena.")
    document_bytes = analysis.pop("_verified_document_bytes", None)
    if not isinstance(document_bytes, bytes) or not document_bytes.startswith(b"%PDF"):
        raise HTTPException(422, "Solo se almacenan bytes de un PDF oficial verificado.")
    meta = analysis.get("metadata") or {}
    court = analysis["jurisprudential_card"]["tribunal"]
    number = meta.get("decision_number")
    if court != req.expected_court or not number or number.casefold() != req.expected_number.casefold():
        raise HTTPException(422, "Tribunal o número no coinciden con el documento.")
    digest = analysis.get("sha256", "")
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise HTTPException(422, "Huella documental inválida.")
    if hashlib.sha256(document_bytes).hexdigest() != digest:
        raise HTTPException(422, "La huella del PDF no coincide con sus bytes.")
    page_count = analysis.get("page_count")
    if not isinstance(page_count, int) or page_count < 1:
        raise HTTPException(422, "No se verificó la cantidad de páginas del PDF.")
    meta = {**meta, "document_type": "PDF", "page_count": page_count}
    diagnostics = {
        **(analysis.get("extraction_diagnostics") or {}),
        "document_type": "PDF",
        "page_count": page_count,
        "document_bytes": len(document_bytes),
        "source_sha256": digest,
    }
    try:
        with connect() as conn:
            init_schema(conn)
            with conn.cursor() as cur:
                cur.execute("""
                INSERT INTO elcohe_decisions
                    (court,decision_number,source_url,sha256,chamber,decision_date,case_number,
                     legal_issue,verification_status,relevant_passages,metadata,extraction_diagnostics)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (court,decision_number,sha256) DO UPDATE SET
                    source_url=EXCLUDED.source_url,
                    legal_issue=EXCLUDED.legal_issue,
                    verification_status=EXCLUDED.verification_status,
                    relevant_passages=EXCLUDED.relevant_passages,
                    metadata=EXCLUDED.metadata,
                    extraction_diagnostics=EXCLUDED.extraction_diagnostics,
                    updated_at=NOW()
                RETURNING id,created_at,updated_at
                """, (court, number, analysis["source_url"], digest, meta.get("chamber"),
                      meta.get("date"), meta.get("case_number"), req.legal_issue,
                      analysis["status"], Jsonb(analysis.get("relevant_passages", [])),
                      Jsonb(meta), Jsonb(diagnostics)))
                id_, created, updated = cur.fetchone()
                persist_verified_pdf(
                    cur, id_, document_bytes, digest, page_count
                )
        return {"status": "STORED_VERIFIED_SOURCE", "id": id_, "court": court,
                "decision_number": number, "sha256": digest, "source_url": analysis["source_url"],
                "verification_status": analysis["status"], "review_required": True,
                "ratio_decidendi_verified": False, "created_at": created, "updated_at": updated}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, f"Error al guardar la decisión ({type(exc).__name__}).")


@router.get("/decisions", operation_id="listRegisteredDecisions", dependencies=[Depends(require_read_access)])
def list_decisions(court: str | None = Query(default=None, pattern=r"^(SCJ|TC)$"),
                   decision_number: str | None = Query(default=None, max_length=80),
                   limit: int = Query(default=20, ge=1, le=100),
                   offset: int = Query(default=0, ge=0)):
    conditions = []
    params: list[Any] = []
    if court:
        conditions.append("court=%s"); params.append(court)
    if decision_number:
        conditions.append("decision_number ILIKE %s"); params.append("%" + decision_number.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    sql = ("SELECT id,court,decision_number,source_url,sha256,chamber,decision_date,case_number,"
           "legal_issue,verification_status,review_required,ratio_decidendi_verified,created_at,updated_at "
           "FROM elcohe_decisions" + where + " ORDER BY id DESC LIMIT %s OFFSET %s")
    try:
        with connect() as conn:
            init_schema(conn)
            with conn.cursor() as cur:
                cur.execute(sql, params + [limit, offset])
                fields = [d.name for d in cur.description]
                items = [row_to_dict(row, fields) for row in cur.fetchall()]
        return {"status": "REGISTERED_RESULTS", "items": items, "count": len(items), "limit": limit, "offset": offset}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, f"Error al consultar el registro ({type(exc).__name__}).")


@router.get("/decisions/{decision_id}", operation_id="getRegisteredDecision", dependencies=[Depends(require_read_access)])
def get_decision(decision_id: int):
    if decision_id < 1:
        raise HTTPException(422, "Identificador inválido.")
    try:
        with connect() as conn:
            init_schema(conn)
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM elcohe_decisions WHERE id=%s", (decision_id,))
                row = cur.fetchone()
                if not row:
                    raise HTTPException(404, "Decisión no registrada.")
                result = row_to_dict(row, [d.name for d in cur.description])
        return {"status": "REGISTERED_DECISION", "decision": result}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, f"Error al consultar la decisión ({type(exc).__name__}).")
