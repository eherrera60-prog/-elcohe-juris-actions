"""Restricted indexed discovery for official Tribunal Constitucional documents."""
from __future__ import annotations

import json
import os
import re
from urllib.parse import urlsplit, urlunsplit

import httpx


BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
MAX_INDEX_RESPONSE_BYTES = 2 * 1024 * 1024
TC_DECISION_NUMBER = re.compile(r"\bTC\s*/\s*(\d{1,4})\s*/\s*(\d{2})\b", re.I)
TC_HOST = "tribunalconstitucional.gob.do"
TC_STORAGE_HOST = "tribunalsitestorage.blob.core.windows.net"


def brave_search_enabled() -> bool:
    return os.getenv("BRAVE_SEARCH_ENABLED", "").strip().casefold() in {
        "1", "true", "yes", "on",
    }


def is_official_tc_host(host: str | None) -> bool:
    if not host:
        return False
    normalized = host.lower().strip(".")
    return (
        normalized == TC_HOST
        or normalized.endswith("." + TC_HOST)
        or normalized == TC_STORAGE_HOST
    )


def official_tc_url(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = urlsplit(value.strip())
        host = (parsed.hostname or "").lower().rstrip(".")
        if (
            parsed.scheme != "https"
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
            or not is_official_tc_host(host)
        ):
            return None
    except ValueError:
        return None
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


def _canonical_decision_number(text: str) -> str | None:
    match = TC_DECISION_NUMBER.search(text)
    if not match:
        match = re.search(r"\bTC[-_](\d{1,4})[-_](\d{2})\b", text, re.I)
    if not match:
        return None
    digits, year = match.groups()
    return f"TC/{digits.zfill(4)}/{year}"


def _search_expression(query: str) -> str:
    clean = re.sub(r"[^\w\s/.-]", " ", query, flags=re.UNICODE)
    clean = re.sub(r"\b(?:AND|OR|NOT|SITE)\b", " ", clean, flags=re.I)
    clean = " ".join(clean.split())
    return (
        f"(site:{TC_HOST} OR site:{TC_STORAGE_HOST}) {clean}"
    )


def _indexed_candidates(
    payload: object,
    query: str,
    year: int | None,
    limit: int,
) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    web = payload.get("web")
    rows = web.get("results") if isinstance(web, dict) else None
    if not isinstance(rows, list):
        return []

    requested_number = _canonical_decision_number(query)
    results = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        url = official_tc_url(row.get("url"))
        if not url or url in seen:
            continue
        title = row.get("title") if isinstance(row.get("title"), str) else ""
        snippet = row.get("description") if isinstance(row.get("description"), str) else ""
        number = _canonical_decision_number(
            " ".join((title, snippet, url))
        )
        if requested_number and number != requested_number:
            continue
        if year is not None:
            if number:
                if int(number[-2:]) != year % 100:
                    continue
            elif not re.search(rf"\b{year}\b", f"{title} {snippet} {url}"):
                continue

        seen.add(url)
        results.append({
            "status": "CANDIDATE",
            "court": "TC",
            "decision_number": number,
            "source_url": url,
            "indexed_title": title[:500],
            "indexed_snippet": snippet[:1200],
            "search_source": "BRAVE_SEARCH_INDEX",
            "retrieval_mode": "INDEXED_OFFICIAL_SEARCH",
            "source_status": "NOT_VERIFIED",
            "identity_status": "NOT_VERIFIED",
            "passage_status": "NOT_VERIFIED",
            "official_document_verified": False,
            "ratio_decidendi_verified": False,
            "review_required": True,
            "relevance_status": "NOT_VERIFIED",
            "legal_relevance_verified": False,
        })
        if len(results) >= limit:
            break
    return results


async def search_tc_index(
    query: str,
    year: int | None = None,
    limit: int = 10,
) -> dict:
    """Search Brave only after explicit opt-in, accepting official TC sources."""
    if not brave_search_enabled():
        return {
            "status": "INDEX_SEARCH_DISABLED",
            "provider": "BRAVE_SEARCH_INDEX",
            "search_executed": False,
            "results": [],
            "message": (
                "La búsqueda oficial del TC permanece disponible sin coste ni "
                "credenciales. Brave está desactivado; habilítelo expresamente "
                "con BRAVE_SEARCH_ENABLED=true si desea usar esa integración opcional."
            ),
        }

    api_key = os.getenv("BRAVE_SEARCH_API_KEY", "").strip()
    if not api_key:
        return {
            "status": "INDEX_SEARCH_NOT_CONFIGURED",
            "provider": "BRAVE_SEARCH_INDEX",
            "search_executed": False,
            "results": [],
            "message": (
                "La búsqueda indexada requiere configurar BRAVE_SEARCH_API_KEY "
                "en el servicio; no se consultaron ni repitieron las rutas TC "
                "que respondieron HTTP 406."
            ),
        }

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(20, connect=8),
            follow_redirects=False,
            headers={
                "Accept": "application/json",
                "User-Agent": "ELCOHE-JURIS-RD/1.6.5",
                "X-Subscription-Token": api_key,
            },
            trust_env=False,
        ) as client:
            async with client.stream(
                "GET",
                BRAVE_SEARCH_URL,
                params={
                    "q": _search_expression(
                        f"{query} {year}" if year is not None else query
                    ),
                    "count": min(max(limit * 2, 10), 20),
                },
            ) as response:
                status_code = response.status_code
                if status_code != 200:
                    return {
                        "status": "INDEX_SEARCH_ACCESS_FAILED",
                        "provider": "BRAVE_SEARCH_INDEX",
                        "search_executed": True,
                        "http_status": status_code,
                        "results": [],
                        "message": (
                            "El proveedor de búsqueda indexada no completó la consulta; "
                            "no se confirma ausencia de sentencias."
                        ),
                    }
                claimed_size = response.headers.get("content-length")
                if (
                    claimed_size
                    and claimed_size.isdigit()
                    and int(claimed_size) > MAX_INDEX_RESPONSE_BYTES
                ):
                    return {
                        "status": "INDEX_SEARCH_EXTRACTION_FAILED",
                        "provider": "BRAVE_SEARCH_INDEX",
                        "search_executed": True,
                        "results": [],
                        "message": "La respuesta del índice excedió el límite de lectura permitido.",
                    }
                chunks = []
                response_size = 0
                async for chunk in response.aiter_bytes():
                    response_size += len(chunk)
                    if response_size > MAX_INDEX_RESPONSE_BYTES:
                        return {
                            "status": "INDEX_SEARCH_EXTRACTION_FAILED",
                            "provider": "BRAVE_SEARCH_INDEX",
                            "search_executed": True,
                            "results": [],
                            "message": "La respuesta del índice excedió el límite de lectura permitido.",
                        }
                    chunks.append(chunk)
                response_body = b"".join(chunks)
    except httpx.HTTPError as exc:
        return {
            "status": "INDEX_SEARCH_ACCESS_FAILED",
            "provider": "BRAVE_SEARCH_INDEX",
            "search_executed": False,
            "results": [],
            "error_type": type(exc).__name__,
            "message": "No fue posible consultar el índice de búsqueda configurado.",
        }

    try:
        payload = json.loads(response_body)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
        return {
            "status": "INDEX_SEARCH_EXTRACTION_FAILED",
            "provider": "BRAVE_SEARCH_INDEX",
            "search_executed": True,
            "results": [],
            "message": "El proveedor respondió, pero su contenido no es JSON interpretable.",
        }

    web = payload.get("web") if isinstance(payload, dict) else None
    rows = web.get("results") if isinstance(web, dict) else None
    if not isinstance(rows, list):
        return {
            "status": "INDEX_SEARCH_EXTRACTION_FAILED",
            "provider": "BRAVE_SEARCH_INDEX",
            "search_executed": True,
            "results": [],
            "message": "La respuesta del índice no contiene la lista de resultados esperada.",
        }
    results = _indexed_candidates(payload, query, year, limit)
    return {
        "status": "CANDIDATES_FOUND" if results else "NO_INDEX_RESULTS",
        "provider": "BRAVE_SEARCH_INDEX",
        "search_executed": True,
        "query": query,
        "scoped_domains": [TC_HOST, TC_STORAGE_HOST],
        "results": results,
        "message": (
            "El índice localizó fuentes oficiales candidatas; los resultados "
            "no verifican por sí mismos el PDF, su identidad ni su pertinencia."
            if results else
            "El índice no devolvió resultados TC verificables para esta consulta; "
            "esto no demuestra que no existan sentencias."
        ),
    }
