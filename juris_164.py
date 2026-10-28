"""Official SCJ search and page-anchored extraction for ELCOHE-JURIS RD 1.6.4."""
from __future__ import annotations

import json
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, HttpUrl

SCJ_SEARCH_URL = "https://consultasentenciascj.poderjudicial.gob.do/Home/GetExpedientes"
MAX_SCJ_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_SCJ_YEAR_SPAN = 10


class FoundationExtractionRequest(BaseModel):
    url: HttpUrl
    expected_court: str = Field(pattern=r"^(TC|SCJ)$")
    expected_number: str = Field(min_length=3, max_length=40)
    legal_issue: str | None = Field(default=None, min_length=3, max_length=300)
    limit: int = Field(default=12, ge=1, le=20)


def _read_options(soup: BeautifulSoup, select_id: str) -> dict[str, str]:
    select = soup.find("select", id=select_id)
    if select is None:
        raise HTTPException(502, f"El portal SCJ no publicó el filtro esperado: {select_id}.")
    options = {}
    for option in select.find_all("option"):
        value = (option.get("value") or "").strip()
        label = " ".join(option.get_text(" ", strip=True).split())
        if value and label and label.lower() not in {"seleccione", "seleccione un año"}:
            options[label.casefold()] = value
            options[value.casefold()] = value
    return options


def _option_value(options: dict[str, str], label: str | None, field: str) -> str:
    if label is None:
        return ""
    normalized = label.strip().casefold()
    if not normalized:
        return ""
    value = options.get(normalized)
    if value is None:
        matches = {option_value for option, option_value in options.items()
                   if normalized in option}
        if len(matches) == 1:
            value = matches.pop()
    if value is None:
        raise HTTPException(422, f"El filtro {field} no coincide con una opción del portal SCJ.")
    return value


def _official_document_url(value: object, allowed_host) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = urljoin("https://consultasentenciascj.poderjudicial.gob.do/", value.strip())
    parsed = urlparse(candidate)
    if parsed.scheme == "https" and allowed_host(parsed.hostname):
        return candidate
    if parsed.scheme == "https" and parsed.hostname == "visorjs.azurewebsites.net":
        embedded = parse_qs(parsed.query).get("file", [])
        if embedded:
            document = urlparse(embedded[0])
            if document.scheme == "https" and allowed_host(document.hostname):
                return embedded[0]
    return None


def _scj_candidates(payload: object, allowed_host) -> tuple[list[dict], int] | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return None
    try:
        count = int(payload.get("recordsFiltered", len(payload["data"])))
    except (TypeError, ValueError):
        return None

    results = []
    seen = set()
    for row in payload["data"]:
        if not isinstance(row, dict):
            continue
        number = row.get("noSentencia")
        number = number.strip() if isinstance(number, str) else None
        case_number = row.get("noUnico")
        case_number = case_number.strip() if isinstance(case_number, str) else None
        document_url = _official_document_url(row.get("urlBlob"), allowed_host)
        key = (number, case_number, document_url)
        if key in seen:
            continue
        seen.add(key)
        results.append({
            "status": "CANDIDATE",
            "court": "SCJ",
            "tribunal": row.get("descTribunal"),
            "decision_number": number or None,
            "case_number": case_number or None,
            "date": row.get("fechaFallo"),
            "source_url": document_url,
            "source_status": "NOT_VERIFIED",
            "identity_status": "NOT_VERIFIED",
            "passage_status": "NOT_VERIFIED",
            "official_document_verified": False,
            "ratio_decidendi_verified": False,
        })
    return results, count


def _foundation_payload(req, doc: dict, extracted: dict | None) -> dict:
    from app import (
        allowed_host,
        annotate_foundation_attribution,
        court_matches,
        decision_number_matches,
        detect_court,
        evidence_classification_fields,
        extract_metadata_from_text,
        extract_numbered_foundations,
        normalize_space,
        score_passage,
    )

    official_pdf = (
        allowed_host(doc["host"])
        and doc["data"].startswith(b"%PDF")
    )
    source_status = "VERIFIED_SOURCE" if official_pdf else "NOT_VERIFIED"
    base = {
        "status": "NOT_VERIFIED",
        **evidence_classification_fields(),
        "source_status": source_status,
        "identity_status": "NOT_VERIFIED",
        "passage_status": "NOT_VERIFIED",
        "official_document_verified": False,
        "legal_holding_status": "CANDIDATE",
        "ratio_decidendi_verified": False,
        "review_required": True,
        "source_url": doc["final_url"],
        "sha256_pdf": doc["sha256"],
        "requested_court": req.expected_court,
        "requested_number": req.expected_number,
        "foundations": [],
    }
    if not official_pdf:
        base["message"] = "La URL oficial no devolvió bytes PDF verificables."
        return base
    if extracted is None:
        base["message"] = "No se pudo extraer texto del PDF oficial."
        return base

    base["document_type"] = extracted["document_type"]
    base["page_count"] = extracted["page_count"]
    base["text_extraction_ok"] = extracted["extraction_ok"]
    if not extracted["extraction_ok"]:
        base["message"] = "No se pudo extraer texto suficiente para verificar la identidad del documento."
        return base

    court = detect_court(doc["host"], req.expected_court, extracted["full_text"])
    metadata = extract_metadata_from_text(extracted["full_text"], court)
    court_ok = court_matches(court, req.expected_court)
    number_ok = decision_number_matches(
        extracted["full_text"], court, req.expected_number
    )
    base.update({
        "court": court,
        "metadata": metadata,
        "expected_court_match": court_ok,
        "expected_number_match": number_ok,
    })
    if not (court_ok and number_ok):
        base["message"] = "La fuente PDF es oficial, pero no se verificó la identidad judicial solicitada."
        return base

    base.update({
        "status": "VERIFIED_IDENTITY",
        "identity_status": "VERIFIED_IDENTITY",
        "official_document_verified": True,
    })
    units = extract_numbered_foundations(extracted["pages"], court)
    units = annotate_foundation_attribution(units, court)
    candidates = [
        unit for unit in units
        if unit.get("section") == "DECIDING_COURT" and unit.get("text")
    ]
    if req.legal_issue:
        candidates.sort(
            key=lambda unit: score_passage(
                unit["text"], req.legal_issue, unit.get("attribution")
            ),
            reverse=True,
        )
    passages = []
    for unit in candidates[:req.limit]:
        passage = normalize_space(unit["text"])
        passages.append({
            "status": "VERIFIED_PASSAGE",
            **evidence_classification_fields(),
            "legal_holding_status": "CANDIDATE",
            "court": court,
            "tribunal": metadata.get("chamber") or (
                "Tribunal Constitucional" if court == "TC"
                else "Suprema Corte de Justicia"
            ),
            "decision_number": metadata.get("decision_number"),
            "date": metadata.get("date"),
            "page": unit["page"],
            "end_page": unit.get("end_page", unit["page"]),
            "foundation_number": unit["foundation_number"],
            "passage": passage[:5000],
            "passage_truncated": len(passage) > 5000,
            "attribution": unit.get("attribution"),
            "attribution_indicators": unit.get("attribution_indicators", []),
            "attribution_verified": False,
            "ratio_decidendi_verified": False,
            "review_required": True,
        })
    base.update({
        "status": "VERIFIED_PASSAGE" if passages else "VERIFIED_IDENTITY",
        "passage_status": "VERIFIED_PASSAGE" if passages else "NOT_VERIFIED",
        "foundations": passages,
        "extraction_diagnostics": {
            "numbered_foundations_detected": len(units),
            "deciding_court_foundations": len(candidates),
            "passages_returned": len(passages),
            "limit": req.limit,
        },
        "message": (
            "La presencia textual y la página del pasaje fueron extraídas del PDF verificado. "
            "La atribución judicial y la ratio decidendi requieren revisión jurídica humana."
        ),
    })
    return base


async def search_scj_candidates(
    q: str,
    chamber: str | None = None,
    matter: str | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
    limit: int = 10,
) -> dict:
    """Query the SCJ's public DataTables endpoint; every returned row is a candidate."""
    from app import SCJ_PORTAL_URL, allowed_host, validate_official_url

    if not q.strip():
        raise HTTPException(422, "q no puede estar vacío.")
    if year_from is not None and year_to is not None and year_from > year_to:
        raise HTTPException(400, "year_from no puede ser mayor que year_to.")
    if (year_from is None) != (year_to is None):
        raise HTTPException(422, "Para filtrar por rango SCJ se requieren year_from y year_to.")
    if year_from is not None and year_to is not None:
        if year_from < 1908 or year_to > 2100:
            raise HTTPException(422, "Año SCJ fuera del rango permitido.")
        if year_to - year_from > MAX_SCJ_YEAR_SPAN:
            raise HTTPException(422, "El rango SCJ no puede superar 10 años por consulta.")
        years = range(year_from, year_to + 1)
    else:
        years = (None,)

    portal_url = SCJ_PORTAL_URL.rstrip("/") + "/"
    validate_official_url(portal_url)
    validate_official_url(SCJ_SEARCH_URL)
    results: list[dict] = []
    reported_count = 0
    page_length = max(10, min(limit, 50))

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30, connect=10),
            follow_redirects=False,
            headers={
                "User-Agent": "ELCOHE-JURIS-RD/1.6.4",
                "Accept": "text/html,application/json",
            },
            trust_env=False,
        ) as client:
            async with client.stream("GET", portal_url) as response:
                if 300 <= response.status_code < 400:
                    raise HTTPException(502, "El portal SCJ redirigió la consulta inicial.")
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").lower()
                if "html" not in content_type:
                    raise HTTPException(502, "El portal SCJ no devolvió su formulario de búsqueda.")
                chunks = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_SCJ_RESPONSE_BYTES:
                        raise HTTPException(502, "El formulario oficial SCJ supera el límite de lectura.")
                    chunks.append(chunk)
                soup = BeautifulSoup(b"".join(chunks), "html.parser")

            tribunal_options = _read_options(soup, "cbSala")
            matter_options = _read_options(soup, "cbMateria")
            year_options = _read_options(soup, "cbAno")
            tribunal_id = _option_value(tribunal_options, chamber, "chamber")
            matter_id = _option_value(matter_options, matter, "matter")

            for year in years:
                year_id = _option_value(year_options, str(year), "year") if year is not None else "-1"
                form = {
                    "draw": "1",
                    "start": "0",
                    "length": str(page_length),
                    "search[value]": "",
                    "search[regex]": "false",
                    "IdTribunal": tribunal_id,
                    "Materia": matter_id,
                    "Ano": year_id,
                    "Mes": "",
                    "IdTipoDocumento": "1",
                    "Contenido": q.strip(),
                }
                async with client.stream(
                    "POST",
                    SCJ_SEARCH_URL,
                    data=form,
                    headers={
                        "Referer": portal_url,
                        "Origin": "https://consultasentenciascj.poderjudicial.gob.do",
                        "X-Requested-With": "XMLHttpRequest",
                        "Accept": "application/json, text/javascript, */*; q=0.01",
                    },
                ) as response:
                    if 300 <= response.status_code < 400:
                        raise HTTPException(502, "La búsqueda oficial SCJ redirigió la consulta.")
                    response.raise_for_status()
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_SCJ_RESPONSE_BYTES:
                            raise HTTPException(502, "La respuesta oficial SCJ supera el límite de lectura.")
                try:
                    payload = json.loads(body)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    return {
                        "query": q,
                        "official_portal": portal_url,
                        "status": "EXTRACTION_UNVERIFIED",
                        "results": [],
                        "message": "La respuesta del portal SCJ no contiene JSON interpretable.",
                    }
                parsed = _scj_candidates(payload, allowed_host)
                if parsed is None:
                    return {
                        "query": q,
                        "official_portal": portal_url,
                        "status": "EXTRACTION_UNVERIFIED",
                        "results": [],
                        "message": "La respuesta SCJ no coincide con el esquema publicado de resultados.",
                    }
                candidates, count = parsed
                reported_count += count
                for candidate in candidates:
                    if candidate not in results:
                        results.append(candidate)
                    if len(results) >= limit:
                        break
                if len(results) >= limit:
                    break
    except HTTPException:
        raise
    except httpx.HTTPError as exc:
        raise HTTPException(
            502, f"No fue posible consultar el buscador oficial SCJ ({type(exc).__name__})."
        ) from exc

    return {
        "query": q,
        "official_portal": portal_url,
        "filters": {
            "chamber": chamber,
            "matter": matter,
            "year_from": year_from,
            "year_to": year_to,
        },
        "official_results_reported": reported_count,
        "status": "CANDIDATES_FOUND" if results else "NO_CANDIDATES_IN_RESPONSE",
        "results": results[:limit],
        "note": (
            "Los registros son candidatos devueltos por la búsqueda oficial. "
            "La ausencia de registros en esta respuesta no prueba que no existan sentencias; "
            "verifique cada PDF, tribunal, número y pasaje antes de atribuir criterios."
        ),
    }


def install(app: FastAPI) -> None:
    @app.get("/v1/scj/search", operation_id="searchScjDecisions")
    async def search_scj(
        q: str = Query(min_length=2, max_length=240),
        chamber: str | None = Query(default=None, max_length=80),
        matter: str | None = Query(default=None, max_length=80),
        year_from: int | None = Query(default=None, ge=1908, le=2100),
        year_to: int | None = Query(default=None, ge=1908, le=2100),
        limit: int = Query(default=10, ge=1, le=20),
    ):
        return await search_scj_candidates(
            q, chamber, matter, year_from, year_to, limit
        )

    @app.post("/v1/extract-foundations", operation_id="extractVerifiedFoundations")
    async def extract_verified_foundations(req: FoundationExtractionRequest):
        from app import extract_document_text, fetch_document

        doc = await fetch_document(str(req.url))
        extracted = (
            extract_document_text(doc)
            if doc["data"].startswith(b"%PDF") else None
        )
        return _foundation_payload(req, doc, extracted)
