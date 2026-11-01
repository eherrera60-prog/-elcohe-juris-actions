"""Registro PostgreSQL de decisiones verificadas; no certifica ratio decidendi."""
from __future__ import annotations
from typing import Any

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
