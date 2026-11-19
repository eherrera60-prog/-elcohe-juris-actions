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
"""


def init_schema(conn):
    with conn.cursor() as cur:
        for statement in SCHEMA.split(";"):
            if statement.strip():
                cur.execute(statement)


def row_to_dict(row: tuple, fields: list[str]) -> dict[str, Any]:
    return dict(zip(fields, row))


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
                    status = "ALREADY_VERIFIED_SOURCE"
                    deduplicated = True
                else:
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
                    ORDER BY created_at DESC LIMIT %s
                    """,
                    parameters + [limit],
                )
                fields = [column.name for column in cur.description]
                items = [row_to_dict(row, fields) for row in cur.fetchall()]
        for item in items:
            item["retrieval_mode"] = "PREVIOUSLY_VERIFIED_DOCUMENT"
            item["official_document_verified"] = True
            item["identity_status"] = "VERIFIED_IDENTITY"
            item["ratio_decidendi_verified"] = False
            item["review_required"] = True
        return items
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            503, f"Error al consultar referencias verificadas ({type(exc).__name__})."
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
    from app import AnalyzeDecisionRequest, analyze_official_decision
    from psycopg.types.json import Jsonb

    analysis = await analyze_official_decision(AnalyzeDecisionRequest(**req.model_dump()))
    if not analysis.get("official_source_verified") or analysis.get("status") not in ("VERIFIED_SOURCE", "HOLDING_CANDIDATE"):
        raise HTTPException(422, "Documento no verificado; no se almacena.")
    meta = analysis.get("metadata") or {}
    court = analysis["jurisprudential_card"]["tribunal"]
    number = meta.get("decision_number")
    if court != req.expected_court or not number or number.casefold() != req.expected_number.casefold():
        raise HTTPException(422, "Tribunal o número no coinciden con el documento.")
    digest = analysis.get("sha256", "")
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise HTTPException(422, "Huella documental inválida.")
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
                      Jsonb(meta), Jsonb(analysis.get("extraction_diagnostics", {}))))
                id_, created, updated = cur.fetchone()
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
