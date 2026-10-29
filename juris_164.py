"""Official SCJ search and page-anchored extraction for ELCOHE-JURIS RD 1.6.4."""
from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, HttpUrl

SCJ_SEARCH_URL = "https://consultasentenciascj.poderjudicial.gob.do/Home/GetExpedientes"
SCJ_ARCHIVE_SEARCH_URL = "https://poderjudicial.gob.do/"
MAX_SCJ_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_SCJ_YEAR_SPAN = 10
MAX_SCJ_ARCHIVE_ARTICLES = 12


class FoundationExtractionRequest(BaseModel):
    url: HttpUrl
    expected_court: str = Field(pattern=r"^(TC|SCJ)$")
    expected_number: str = Field(min_length=3, max_length=40)
    legal_issue: str | None = Field(default=None, min_length=3, max_length=300)
    limit: int = Field(default=12, ge=1, le=30)


def _read_options(soup: BeautifulSoup, select_id: str) -> dict[str, str]:
    select = soup.find("select", id=select_id)
    if select is None:
        raise HTTPException(
            502,
            f"El portal SCJ no publicó el filtro esperado: {select_id}.",
        )
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


def _scj_error(
    query: str,
    portal_url: str,
    status: str,
    message: str,
    *,
    search_executed: bool = False,
    error_type: str | None = None,
    http_status: int | None = None,
) -> HTTPException:
    detail = {
        "query": query,
        "official_portal": portal_url,
        "search_executed": search_executed,
        "status": status,
        "message": message,
    }
    if error_type:
        detail["error_type"] = error_type
    if http_status is not None:
        detail["http_status"] = http_status
    return HTTPException(502, detail=detail)


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
    from app import canonical_scj_identifier

    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return None
    try:
        count = int(payload["recordsFiltered"])
    except (TypeError, ValueError, OverflowError):
        return None
    if count < 0:
        return None

    results = []
    seen = set()
    for row in payload["data"]:
        if not isinstance(row, dict):
            continue
        number = row.get("noSentencia")
        number = number.strip() if isinstance(number, str) and number.strip() else None
        canonical_number = canonical_scj_identifier(number)
        case_number = row.get("noUnico")
        case_number = case_number.strip() if isinstance(case_number, str) and case_number.strip() else None
        document_url = _official_document_url(row.get("urlBlob"), allowed_host)
        if not number and not document_url:
            continue
        key = (number, case_number, document_url)
        if key in seen:
            continue
        seen.add(key)
        candidate = {
            "status": "CANDIDATE",
            "court": "SCJ",
            "source_status": "NOT_VERIFIED",
            "identity_status": "NOT_VERIFIED",
            "passage_status": "NOT_VERIFIED",
            "official_document_verified": False,
            "ratio_decidendi_verified": False,
        }
        source_fields = {
            "tribunal": row.get("descTribunal"),
            "decision_number": canonical_number or number,
            "case_number": case_number,
            "date": row.get("fechaFallo"),
            "source_url": document_url,
        }
        candidate.update({
            field: value.strip()
            for field, value in source_fields.items()
            if isinstance(value, str) and value.strip()
        })
        if canonical_number and canonical_number != number:
            candidate["reported_decision_number"] = number
        results.append(candidate)
    return results, count


def _scj_archive_candidates(
    article_html: str,
    article_url: str,
    article_title: str,
    allowed_host,
) -> list[dict]:
    from app import extract_source_date

    soup = BeautifulSoup(article_html, "html.parser")
    content = soup.select_one(
        ".elementor-widget-theme-post-content, .entry-content"
    )
    if content is None:
        content = soup.find("article")
    if content is None:
        return []

    text = " ".join(content.get_text(" ", strip=True).split())
    identifier_pattern = re.compile(
        r"\bSCJ\s*-\s*[A-Z]{2}\s*-\s*(?:\d{2}|\d{4})\s*-\s*\d{3,5}\b",
        re.I,
    )
    identifiers = {
        re.sub(r"\s+", "", match.group(0)).upper()
        for match in identifier_pattern.finditer(text)
    }
    if len(identifiers) != 1:
        return []
    decision_number = identifiers.pop()

    pdf_links = []
    for anchor in content.find_all("a", href=True):
        source_url = urljoin(article_url, anchor["href"].strip())
        parsed = urlparse(source_url)
        if (
            parsed.scheme != "https"
            or not allowed_host(parsed.hostname)
            or not parsed.path.lower().endswith(".pdf")
        ):
            continue
        parent_text = " ".join(anchor.parent.get_text(" ", strip=True).split())
        pdf_links.append((source_url, parent_text))

    matching_links = [
        link for link in pdf_links
        if decision_number.replace("-", "").casefold()
        in urlparse(link[0]).path.replace("-", "").casefold()
    ]
    if len(matching_links) == 1:
        source_url = matching_links[0][0]
    else:
        explicit_judgment_links = [
            link for link in pdf_links
            if re.search(r"\b(?:sentencia|fallo|decisi[oó]n)\b", link[1], re.I)
        ]
        if len(explicit_judgment_links) != 1:
            return []
        source_url = explicit_judgment_links[0][0]

    tribunal_match = re.search(
        r"\b(Primera|Segunda|Tercera)\s+Sala\s+de\s+la\s+Suprema\s+Corte\s+de\s+Justicia\b",
        text,
        re.I,
    )
    candidate = {
        "status": "CANDIDATE",
        "court": "SCJ",
        "decision_number": decision_number,
        "source_url": source_url,
        "discovery_url": article_url,
        "discovery_title": article_title,
        "search_source": "OFFICIAL_JUDICIARY_SITE",
        "source_status": "NOT_VERIFIED",
        "identity_status": "NOT_VERIFIED",
        "passage_status": "NOT_VERIFIED",
        "official_document_verified": False,
        "ratio_decidendi_verified": False,
    }
    if tribunal_match:
        candidate["tribunal"] = " ".join(tribunal_match.group(0).split())
    decision_date = extract_source_date(text, require_issuance_marker=True)
    if decision_date:
        candidate["date"] = decision_date
    return [candidate]


async def _search_scj_official_archive(
    query: str,
    allowed_host,
    validate_official_url,
) -> dict:
    from app import official_request

    search_url = SCJ_ARCHIVE_SEARCH_URL
    validate_official_url(search_url)
    article_urls: list[tuple[str, str, str]] = []
    article_failures = 0

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(20, connect=8),
            follow_redirects=True,
            max_redirects=3,
            headers={
                "User-Agent": "ELCOHE-JURIS-RD/1.6.4",
                "Accept": "text/html,application/xhtml+xml",
            },
            trust_env=False,
        ) as client:
            response = await official_request(
                client, "get", search_url, params={"s": query}
            )
            response.raise_for_status()
            if not allowed_host(response.url.host):
                return {
                    "status": "ACCESS_FAILED",
                    "search_url": str(response.url),
                    "results": [],
                    "search_executed": False,
                    "message": "La búsqueda oficial redirigió fuera del dominio judicial permitido.",
                }
            search_url = str(response.url)
            if "html" not in response.headers.get("content-type", "").lower():
                return {
                    "status": "EXTRACTION_FAILED",
                    "search_url": str(response.url),
                    "results": [],
                    "search_executed": True,
                    "message": "El buscador institucional no devolvió HTML interpretable.",
                }

            listing = BeautifulSoup(response.text, "html.parser")
            for article in listing.find_all("article"):
                heading = article.find(["h1", "h2", "h3", "h4"])
                anchor = (
                    heading.find("a", href=True)
                    if heading else article.find("a", href=True)
                )
                if anchor is None:
                    continue
                article_url = urljoin(str(response.url), anchor["href"].strip())
                parsed = urlparse(article_url)
                if (
                    parsed.scheme != "https"
                    or not allowed_host(parsed.hostname)
                    or parsed.path.lower().endswith(".pdf")
                ):
                    continue
                title = " ".join(anchor.get_text(" ", strip=True).split())
                post_id = next(
                    (
                        match.group(1)
                        for class_name in article.get("class", [])
                        if (match := re.fullmatch(r"post-(\d+)", class_name))
                    ),
                    None,
                )
                fetch_url = (
                    f"{SCJ_ARCHIVE_SEARCH_URL}?p={post_id}"
                    if post_id else article_url
                )
                article_urls.append((article_url, fetch_url, title))
                if len(article_urls) >= MAX_SCJ_ARCHIVE_ARTICLES:
                    break

            results = []
            for article_url, fetch_url, title in article_urls:
                try:
                    article_response = await official_request(
                        client, "get", fetch_url
                    )
                    article_response.raise_for_status()
                except httpx.HTTPError:
                    article_failures += 1
                    continue
                if (
                    not allowed_host(article_response.url.host)
                    or "html" not in article_response.headers.get(
                        "content-type", ""
                    ).lower()
                ):
                    article_failures += 1
                    continue
                results.extend(_scj_archive_candidates(
                    article_response.text,
                    article_url,
                    title,
                    allowed_host,
                ))
    except httpx.HTTPError as exc:
        return {
            "status": "ACCESS_FAILED",
            "search_url": search_url,
            "results": [],
            "search_executed": False,
            "error_type": type(exc).__name__,
            "http_status": exc.response.status_code
            if isinstance(exc, httpx.HTTPStatusError) else None,
            "message": "No fue posible consultar el buscador institucional del Poder Judicial.",
        }

    if results:
        return {
            "status": "CANDIDATES_FOUND",
            "search_url": search_url,
            "results": results,
            "search_executed": True,
            "message": (
                "El buscador institucional enlazó candidatos PDF. Verifique el "
                "PDF oficial antes de confirmar identidad o contenido."
            ),
        }
    if article_failures:
        return {
            "status": "ACCESS_FAILED",
            "search_url": search_url,
            "results": [],
            "search_executed": True,
            "article_failures": article_failures,
            "message": "No fue posible leer todas las páginas institucionales candidatas.",
        }
    body = listing.body
    body_classes = set(body.get("class", [])) if body else set()
    empty_results = listing.select_one(
        ".elementor-posts-container .elementor-posts-nothing-found"
    )
    empty_text = (
        " ".join(empty_results.get_text(" ", strip=True).split())
        if empty_results else ""
    )
    if (
        "search-no-results" in body_classes
        and re.search(
            r"\bparece que no podemos encontrar lo que buscas\b",
            empty_text,
            re.I,
        )
    ):
        return {
            "status": "NO_RESULTS_CONFIRMED",
            "search_url": search_url,
            "results": [],
            "search_executed": True,
            "message": "El archivo institucional confirmó cero resultados para esta consulta.",
        }
    return {
        "status": "EXTRACTION_FAILED",
        "search_url": search_url,
        "results": [],
        "search_executed": True,
        "message": (
            "La página del archivo no permitió confirmar resultados ni un estado "
            "explícito de cero coincidencias."
        ),
    }


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
        extract_dispositive_passage,
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
        "dispositive_status": "NOT_VERIFIED",
        "dispositive": None,
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
    dispositive = extract_dispositive_passage(extracted["pages"])
    dispositive_payload = None
    if dispositive:
        dispositive_text = normalize_space(dispositive["text"])
        dispositive_payload = {
            "status": "VERIFIED_PASSAGE",
            "section": "PART_DISPOSITIVE_CANDIDATE",
            "page": dispositive["page"],
            "end_page": dispositive["end_page"],
            "text": dispositive_text[:5000],
            "text_truncated": len(dispositive_text) > 5000,
            "review_required": True,
            "legal_effect_verified": False,
        }
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
        "dispositive_status": (
            "VERIFIED_PASSAGE" if dispositive_payload else "NOT_VERIFIED"
        ),
        "dispositive": dispositive_payload,
        "extraction_diagnostics": {
            "numbered_foundations_detected": len(units),
            "deciding_court_foundations": len(candidates),
            "passages_returned": len(passages),
            "dispositive_page": (
                dispositive_payload["page"] if dispositive_payload else None
            ),
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
    from app import (
        SCJ_PORTAL_URL,
        allowed_host,
        normalize_space,
        official_stream_request,
        validate_official_url,
    )

    normalized_query = normalize_space(q)
    if len(normalized_query) < 2:
        raise HTTPException(422, "q debe contener al menos dos caracteres no blancos.")
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
    response_data = {
        "query": normalized_query,
        "official_portal": portal_url,
        "search_executed": False,
        "filters": {
            "chamber": chamber,
            "matter": matter,
            "year_from": year_from,
            "year_to": year_to,
        },
    }
    results: list[dict] = []
    reported_count = 0
    unparsed_results = False
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
            response, form_body = await official_stream_request(
                client, "GET", portal_url, max_bytes=MAX_SCJ_RESPONSE_BYTES
            )
            if 300 <= response.status_code < 400:
                raise _scj_error(
                    normalized_query, portal_url, "ACCESS_FAILED",
                    "El portal SCJ redirigió la consulta inicial.",
                    http_status=response.status_code,
                )
            content_type = response.headers.get("content-type", "").lower()
            if "html" not in content_type:
                raise _scj_error(
                    normalized_query, portal_url, "EXTRACTION_FAILED",
                    "El portal SCJ no devolvió su formulario de búsqueda HTML.",
                )
            soup = BeautifulSoup(form_body, "html.parser")

            try:
                tribunal_options = _read_options(soup, "cbSala")
                matter_options = _read_options(soup, "cbMateria")
                year_options = _read_options(soup, "cbAno")
            except HTTPException as exc:
                if exc.status_code != 502:
                    raise
                raise _scj_error(
                    normalized_query, portal_url, "EXTRACTION_FAILED",
                    str(exc.detail),
                ) from exc
            tribunal_id = _option_value(tribunal_options, chamber, "chamber")
            matter_id = _option_value(matter_options, matter, "matter")

            for year in years:
                year_id = _option_value(year_options, str(year), "year") if year is not None else ""
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
                    "Contenido": normalized_query,
                }
                response, body = await official_stream_request(
                    client,
                    "POST",
                    SCJ_SEARCH_URL,
                    max_bytes=MAX_SCJ_RESPONSE_BYTES,
                    data=form,
                    headers={
                        "Referer": portal_url,
                        "Origin": "https://consultasentenciascj.poderjudicial.gob.do",
                        "X-Requested-With": "XMLHttpRequest",
                        "Accept": "application/json, text/javascript, */*; q=0.01",
                    },
                )
                if 300 <= response.status_code < 400:
                    raise _scj_error(
                        normalized_query, portal_url, "ACCESS_FAILED",
                        "La búsqueda oficial SCJ redirigió la consulta.",
                        http_status=response.status_code,
                    )
                response_data["search_executed"] = True
                try:
                    payload = json.loads(body)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    return {
                        **response_data,
                        "status": "EXTRACTION_FAILED",
                        "results": [],
                        "message": "La respuesta del portal SCJ no contiene JSON interpretable.",
                    }
                parsed = _scj_candidates(payload, allowed_host)
                if parsed is None:
                    return {
                        **response_data,
                        "status": "EXTRACTION_FAILED",
                        "results": [],
                        "message": "La respuesta SCJ no coincide con el esquema publicado de resultados.",
                    }
                candidates, count = parsed
                reported_count += count
                if not candidates and (count > 0 or payload["data"]):
                    unparsed_results = True
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
        raise _scj_error(
            normalized_query,
            portal_url,
            "ACCESS_FAILED",
            "No fue posible consultar el buscador oficial SCJ.",
            search_executed=response_data["search_executed"],
            error_type=type(exc).__name__,
            http_status=exc.response.status_code
            if isinstance(exc, httpx.HTTPStatusError) else None,
        ) from exc

    if results:
        status = "CANDIDATES_FOUND"
        message = (
            "Coincidencias localizadas; verifique cada documento oficial antes "
            "de atribuir criterios."
        )
    elif unparsed_results or reported_count:
        status = "EXTRACTION_FAILED"
        message = (
            "El portal informó registros, pero no se pudieron extraer candidatos "
            "con el esquema esperado."
        )
    else:
        archive = None
        if not any((chamber, matter, year_from, year_to)):
            archive = await _search_scj_official_archive(
                normalized_query, allowed_host, validate_official_url
            )
            response_data["search_executed"] = True
            response_data["archive_search"] = {
                "status": archive["status"],
                "search_url": archive["search_url"],
                "search_executed": archive.get("search_executed", False),
            }
            if archive["status"] == "CANDIDATES_FOUND":
                return {
                    **response_data,
                    "official_results_reported": reported_count,
                    "status": "CANDIDATES_FOUND",
                    "results": archive["results"][:limit],
                    "message": archive["message"],
                    "note": (
                        "El buscador principal no devolvió registros. Los enlaces "
                        "del sitio institucional son candidatos; verifique el PDF "
                        "oficial antes de confirmar identidad o contenido."
                    ),
                }
            archive_status = archive.get("status")
            if archive_status in {
                "NO_RESULTS_CONFIRMED",
                "ACCESS_FAILED",
                "EXTRACTION_FAILED",
            }:
                status = archive_status
                message = archive.get(
                    "message",
                    "La búsqueda alternativa no pudo confirmar resultados.",
                )
            else:
                status = "EXTRACTION_FAILED"
                message = (
                    "La búsqueda principal informó cero registros, pero la "
                    "respuesta del archivo alternativo no permitió clasificar "
                    "el resultado."
                )
        else:
            status = "NO_RESULTS_CONFIRMED"
            message = (
                "El portal SCJ informó cero registros para esta consulta y filtros; "
                "el buscador institucional alternativo no se ejecutó porque no "
                "admite esos filtros."
            )

    return {
        **response_data,
        "official_results_reported": reported_count,
        "status": status,
        "results": results[:limit],
        "message": message,
        "note": (
            "Los registros son candidatos devueltos por la búsqueda oficial. "
            "La ausencia de registros en esta respuesta no prueba que no existan "
            "sentencias; verifique cada PDF, tribunal, número y pasaje antes de "
            "atribuir criterios."
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
