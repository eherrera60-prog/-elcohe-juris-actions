"""Aprobación humana auditable de criterios jurisprudenciales previamente registrados.

No se infiere una ratio decidendi a partir de heurísticas. La firma es una
atestación humana explícita, con control administrativo y evidencia documental.
"""
from __future__ import annotations
import hashlib
import json
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator

from registry import connect, init_schema, require_admin, require_read_access

router = APIRouter(prefix="/v1/registry", tags=["Aprobación jurídica humana"])

CONFIRMATION = "CONFIRMO_LECTURA_INTEGRAL"

class LegalApprovalRequest(BaseModel):
    decision_id: int = Field(ge=1)
    expected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    legal_issue: str = Field(min_length=15, max_length=500)
    legal_proposition: str = Field(min_length=30, max_length=2000)
    support_page: int = Field(ge=1)
    support_quote: str = Field(min_length=40, max_length=1800)
    outcome_page: int = Field(ge=1)
    outcome_quote: str = Field(min_length=12, max_length=1800)
    reviewer_name: str = Field(min_length=5, max_length=120)
    reviewer_confirmation: Literal["CONFIRMO_LECTURA_INTEGRAL"]
    judicial_reasoning_reviewed: Literal[True]
    # Explicit declaration of what was personally reviewed; no auto certification.
    reviewer_scope: str = Field(min_length=30, max_length=800)


MIGRATION = """
CREATE TABLE IF NOT EXISTS elcohe_holding_approvals (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    decision_id BIGINT NOT NULL REFERENCES elcohe_decisions(id) ON DELETE RESTRICT,
    document_sha256 CHAR(64) NOT NULL,
    legal_issue TEXT NOT NULL,
    legal_proposition TEXT NOT NULL,
    support_page INTEGER NOT NULL CHECK (support_page > 0),
    support_quote TEXT NOT NULL,
    outcome_page INTEGER NOT NULL CHECK (outcome_page > 0),
    outcome_quote TEXT NOT NULL,
    reviewer_name TEXT NOT NULL,
    reviewer_scope TEXT NOT NULL,
    reviewer_confirmation TEXT NOT NULL CHECK (reviewer_confirmation = 'CONFIRMO_LECTURA_INTEGRAL'),
    approval_digest CHAR(64) NOT NULL UNIQUE,
    approved_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_elcohe_holding_approvals_decision ON elcohe_holding_approvals(decision_id);
"""


def init_approval_schema(conn):
    init_schema(conn)
    with conn.cursor() as cur:
        for statement in MIGRATION.split(";"):
            if statement.strip():
                cur.execute(statement)


def _norm(s: str) -> str:
    import re
    import unicodedata
    s = unicodedata.normalize("NFD", s.lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s).strip()


def _digest(req: LegalApprovalRequest) -> str:
    payload = req.model_dump(mode="json")
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


@router.post("/holdings/approve", operation_id="approveReviewedHolding", dependencies=[Depends(require_admin)])
async def approve_reviewed_holding(req: LegalApprovalRequest):
    """Aprobación por revisor declarado. Verifica nuevamente PDF oficial y citas antes de escritura."""
    from app import fetch_document, extract_document_text, detect_court, court_matches, decision_number_matches, normalized_contains, strip_running_furniture, locate_support_context, attribution_context

    # First verify registered immutable identity; lock row during write after external I/O.
    try:
        with connect() as conn:
            init_approval_schema(conn)
            with conn.cursor() as cur:
                cur.execute("SELECT court, decision_number, source_url, sha256 FROM elcohe_decisions WHERE id=%s", (req.decision_id,))
                row = cur.fetchone()
                if row is None:
                    raise HTTPException(404, "Decisión no registrada.")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, f"No se pudo consultar el registro ({type(exc).__name__}).")

    court, number, url, stored_digest = row
    if stored_digest != req.expected_sha256:
        raise HTTPException(409, "La huella esperada no coincide con la decisión registrada.")

    doc = await fetch_document(url)
    if doc["sha256"] != stored_digest:
        raise HTTPException(409, "El documento oficial ha cambiado; no se aprueba sin nueva revisión.")
    ex = extract_document_text(doc)
    detected = detect_court(doc["host"], court, ex["full_text"])
    if not ex["extraction_ok"] or not court_matches(detected, court) or not decision_number_matches(ex["full_text"], detected, number):
        raise HTTPException(422, "Identidad judicial o extracción documental no verificadas.")
    pages = ex["pages"]
    if req.support_page > len(pages) or req.outcome_page > len(pages):
        raise HTTPException(422, "Página de fundamento o dispositivo inexistente.")
    support_text = pages[req.support_page - 1]["text"]
    outcome_text = pages[req.outcome_page - 1]["text"]
    support_ok = normalized_contains(support_text, req.support_quote) or normalized_contains(strip_running_furniture(support_text), req.support_quote)
    outcome_ok = normalized_contains(outcome_text, req.outcome_quote) or normalized_contains(strip_running_furniture(outcome_text), req.outcome_quote)
    if not support_ok or not outcome_ok:
        raise HTTPException(422, {"error": "Citas no encontradas literalmente en las páginas declaradas.", "support_quote_verified": support_ok, "outcome_quote_verified": outcome_ok})

    context = locate_support_context(pages, req.support_page, req.support_quote)
    if not context["located"]:
        raise HTTPException(422, "El fundamento no pudo vincularse a una unidad contextual.")
    attribution, _ = attribution_context(context["unit"], context["preceding"], context["following"])
    # A heuristic cannot replace a lawyer's explicit attribution, but known negative
    # classes must be rejected; indeterminate passages require manual scope statement.
    if attribution in {"PARTY_ARGUMENT_OR_MIXED_REVIEW", "LOWER_COURT_QUOTATION_REVIEW", "DOCTRINAL_QUOTATION_REVIEW", "DISPOSITIVE_REVIEW"}:
        raise HTTPException(422, f"La unidad seleccionada no puede atribuirse con seguridad al tribunal: {attribution}.")

    digest = _digest(req)
    try:
        with connect() as conn:
            init_approval_schema(conn)
            with conn.cursor() as cur:
                cur.execute("SELECT sha256 FROM elcohe_decisions WHERE id=%s FOR UPDATE", (req.decision_id,))
                locked = cur.fetchone()
                if locked is None or locked[0] != stored_digest:
                    raise HTTPException(409, "El registro cambió durante la validación.")
                cur.execute("SELECT id, approval_digest FROM elcohe_holding_approvals WHERE decision_id=%s ORDER BY id DESC LIMIT 1", (req.decision_id,))
                prior = cur.fetchone()
                if prior:
                    if prior[1] == digest:
                        return {"status": "HOLDING_APPROVED_BY_REVIEWER", "approval_id": prior[0], "decision_id": req.decision_id, "idempotent": True, "source_sha256": stored_digest}
                    raise HTTPException(409, "Ya existe una aprobación para esta decisión. Requiere procedimiento de revisión/versionado separado.")
                cur.execute("""INSERT INTO elcohe_holding_approvals
                  (decision_id, document_sha256, legal_issue, legal_proposition, support_page, support_quote,
                   outcome_page, outcome_quote, reviewer_name, reviewer_scope, reviewer_confirmation, approval_digest)
                  VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                  (req.decision_id, stored_digest, req.legal_issue, req.legal_proposition, req.support_page,
                   req.support_quote, req.outcome_page, req.outcome_quote, req.reviewer_name,
                   req.reviewer_scope, req.reviewer_confirmation, digest))
                approval_id = cur.fetchone()[0]
                # Do not mutate elcohe_decisions legacy status: preserve its original
                # semantics and compatibility. Approval is a distinct, audited record.
        return {"status": "HOLDING_APPROVED_BY_REVIEWER", "approval_id": approval_id,
                "decision_id": req.decision_id, "idempotent": False, "source_sha256": stored_digest,
                "attribution_heuristic": attribution,
                "note": "Aprobación humana declarada; no certificación automática ni validación independiente."}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, f"No se pudo guardar la aprobación ({type(exc).__name__}).")


@router.get("/holdings/{decision_id}", operation_id="getReviewedHolding", dependencies=[Depends(require_read_access)])
def get_reviewed_holding(decision_id: int):
    if decision_id < 1:
        raise HTTPException(422, "Identificador inválido.")
    try:
        with connect() as conn:
            init_approval_schema(conn)
            with conn.cursor() as cur:
                cur.execute("""SELECT id, decision_id, document_sha256, legal_issue, legal_proposition,
                    support_page, support_quote, outcome_page, outcome_quote, reviewer_name,
                    reviewer_scope, approved_at FROM elcohe_holding_approvals
                    WHERE decision_id=%s ORDER BY id DESC LIMIT 1""", (decision_id,))
                row = cur.fetchone()
                if not row:
                    raise HTTPException(404, "No existe aprobación humana para esta decisión.")
                return {"status": "HOLDING_APPROVED_BY_REVIEWER", "holding": dict(zip([d.name for d in cur.description], row))}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, f"No se pudo consultar la aprobación ({type(exc).__name__}).")
