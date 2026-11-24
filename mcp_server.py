from __future__ import annotations

import base64
import re
from contextlib import asynccontextmanager
from contextvars import ContextVar
from urllib.parse import urlparse

from fastapi import HTTPException
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

import auth_access
from decision_number import OfficialDecisionNumber


APP_VERSION = "1.6.5"
MCP_PATH = auth_access.MCP_PATH
METADATA_PATHS = frozenset({
    "/.well-known/oauth-protected-resource",
    "/.well-known/oauth-protected-resource/mcp",
})

mcp = MCPServer(
    "ELCOHE-JURIS RD",
    instructions=(
        "Herramientas de investigación jurisprudencial dominicana. "
        "Los resultados son candidatos; no atribuya ratio decidendi hasta "
        "verificar y leer el documento oficial."
    ),
    version=APP_VERSION,
)
_authenticated_mcp_user: ContextVar[dict | None] = ContextVar(
    "authenticated_mcp_user", default=None
)


def _resource_url(request: Request) -> str:
    return auth_access.mcp_resource_url(request)


def _transport_security_settings() -> TransportSecuritySettings:
    origin = auth_access.external_origin()
    if origin:
        parsed = urlparse(origin)
        hostname = parsed.hostname or ""
        host_pattern = f"[{hostname}]:*" if ":" in hostname else f"{hostname}:*"
        origin_pattern = f"{parsed.scheme}://{host_pattern}"
        allowed_hosts = [parsed.netloc]
        allowed_origins = [origin]
        if parsed.port is None:
            allowed_hosts.append(host_pattern)
            allowed_origins.append(origin_pattern)
    else:
        allowed_hosts = [
            "127.0.0.1",
            "127.0.0.1:*",
            "localhost",
            "localhost:*",
            "[::1]",
            "[::1]:*",
        ]
        allowed_origins = [
            "http://127.0.0.1",
            "http://127.0.0.1:*",
            "http://localhost",
            "http://localhost:*",
            "http://[::1]",
            "http://[::1]:*",
        ]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )


class OAuthProtectedResourceMetadataMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        if request.method != "GET" or request.url.path not in METADATA_PATHS:
            return await call_next(request)

        domain_url = auth_access._auth0_domain_url()
        return JSONResponse({
            "resource": _resource_url(request),
            "authorization_servers": [f"{domain_url}/"],
            "scopes_supported": [auth_access.REQUIRED_PERMISSION],
            "bearer_methods_supported": ["header"],
        })


class MCPAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        authorization = request.headers.get("authorization", "")
        scheme, separator, credentials = authorization.partition(" ")
        token = credentials.strip() if separator and scheme.lower() == "bearer" else None

        try:
            user = await auth_access.require_active_user(request, token)
        except HTTPException as exc:
            headers = dict(exc.headers or {})
            if exc.status_code == 401:
                origin = (
                    auth_access.external_origin()
                    or str(request.base_url).rstrip("/")
                )
                resource_metadata = (
                    f"{origin}"
                    "/.well-known/oauth-protected-resource/mcp"
                )
                headers["WWW-Authenticate"] = (
                    f'Bearer resource_metadata="{resource_metadata}", '
                    f'scope="{auth_access.REQUIRED_PERMISSION}"'
                )
            return JSONResponse(
                {"detail": exc.detail},
                status_code=exc.status_code,
                headers=headers,
            )
        user_token = _authenticated_mcp_user.set(user)
        try:
            return await call_next(request)
        finally:
            _authenticated_mcp_user.reset(user_token)


def _rank_ratio_candidates(evidence: dict, legal_issue: str | None) -> list[dict]:
    from app import fold, tokenize

    issue_tokens = tokenize(legal_issue)
    dispositive = evidence.get("dispositive") or {}
    dispositive_text = (
        dispositive.get("text", "")
        if dispositive.get("status") == "VERIFIED_PASSAGE"
        else ""
    )
    dispositive_tokens = tokenize(dispositive_text)
    if not issue_tokens or not dispositive_text:
        return []

    excluded_origins = {
        "PARTY_ARGUMENT",
        "LOWER_COURT_QUOTATION",
        "CITED_PRECEDENT",
        "CITED_PRECEDENT_OR_MIXED_REVIEW",
        "NON_DECIDING_SECTION",
        "STATUTORY_QUOTATION",
    }
    excluded_attributions = (
        "LOWER_COURT",
        "PARTY_ARGUMENT",
        "NON_DECIDING",
    )
    outcome_link = re.compile(
        r"\b(?:por tanto|en consecuencia|por consiguiente|procede|"
        r"corresponde|se impone|se anula|se casa|se rechaza|"
        r"se acoge|determina)\b",
        re.IGNORECASE,
    )
    generic_outcome_terms = set().union(*(
        tokenize(term)
        for term in (
            "recurso",
            "sentencia",
            "decisión",
            "tribunal",
            "casación",
            "costas",
            "caso",
            "demanda",
            "parte",
            "proceso",
        )
    ))
    candidates = []
    for passage in evidence.get("foundations", []):
        origin = passage.get("reasoning_origin")
        indicators = passage.get("origin_classification_indicators", [])
        attribution = passage.get("attribution") or ""
        text = passage.get("passage", "")
        normalized_text = fold(text)
        if (
            passage.get("status") != "VERIFIED_PASSAGE"
            or attribution not in {
                "POSSIBLE_JUDICIAL_REASONING_REVIEW",
                "DECIDING_COURT_SECTION_REVIEW",
                "DECIDING_COURT_NARRATIVE_REVIEW",
            }
            or origin in excluded_origins
            or attribution.startswith(excluded_attributions)
            or "explicit_precedent_reference" in indicators
            or len(text) < 100
            or not text.strip()
            or re.match(
                r"^\s*\d+(?:\.\d+)*[.)]?\s*"
                r"(?:s[ií]ntesis del conflicto|antecedentes|"
                r"relaci[oó]n de hechos|hechos probados)\b",
                normalized_text,
            )
        ):
            continue
        passage_tokens = tokenize(text)
        matched_issue = issue_tokens & passage_tokens
        minimum_issue_matches = min(2, len(issue_tokens))
        issue_relevance = len(matched_issue) / len(issue_tokens)
        if (
            len(matched_issue) < minimum_issue_matches
            or issue_relevance < 0.35
        ):
            continue
        shared_outcome = (
            passage_tokens & dispositive_tokens
        ) - generic_outcome_terms
        context = " ".join(
            (text, passage.get("following_context") or "")
        )
        has_decisional_link = bool(outcome_link.search(context))
        if not shared_outcome and not has_decisional_link:
            continue
        candidates.append({
            **passage,
            "issue_terms_matched": sorted(matched_issue),
            "issue_relevance": round(issue_relevance, 3),
            "dispositive_relationship": (
                "SHARED_TEXT_WITH_VERIFIED_DISPOSITIVE"
                if shared_outcome
                else "DECISIONAL_LINK_REQUIRES_HUMAN_REVIEW"
            ),
            "_ranking": (
                issue_relevance,
                len(shared_outcome),
            ),
        })
    candidates.sort(key=lambda item: (
        -item["_ranking"][0], -item["_ranking"][1]
    ))
    for candidate in candidates:
        candidate.pop("_ranking")
    return candidates


def install_mcp(
    app,
    *,
    search_tc,
    search_scj,
    prepare_scj,
    verify_document,
    resolve_official_pdf,
    verify_official_pdf,
    official_pdf_request_model,
    extract_passages,
    foundation_request_model,
    find_verified_decisions,
    get_verified_document,
    verify_request_model,
    register_tc_reference,
    tc_reference_request_model,
    register_decision,
    register_request_model,
) -> None:
    @mcp.tool()
    def health_check() -> dict:
        """Comprueba el estado del servicio MCP ELCOHE-JURIS RD."""
        return {
            "ok": True,
            "service": "ELCOHE-JURIS RD MCP",
            "version": APP_VERSION,
        }

    @mcp.tool()
    async def search_tc_decisions(
        q: str,
        year: int | None = None,
        limit: int = 10,
    ) -> dict:
        """Busca candidatos de decisiones del Tribunal Constitucional mediante el servicio REST existente."""
        if not 2 <= len(q.strip()) <= 240 or not 1 <= limit <= 20:
            raise ValueError("q debe tener entre 2 y 240 caracteres y limit entre 1 y 20.")
        return await search_tc(q=q, year=year, limit=limit)

    @mcp.tool()
    async def search_scj_decisions(
        q: str,
        chamber: str | None = None,
        matter: str | None = None,
        year_from: int | None = None,
        year_to: int | None = None,
        limit: int = 10,
    ) -> dict:
        """Ejecuta la búsqueda oficial SCJ; los resultados son candidatos, no decisiones verificadas."""
        if not 2 <= len(q.strip()) <= 240 or not 1 <= limit <= 20:
            raise ValueError("q debe tener entre 2 y 240 caracteres y limit entre 1 y 20.")
        result = await search_scj(
            q=q,
            chamber=chamber,
            matter=matter,
            year_from=year_from,
            year_to=year_to,
            limit=limit,
        )
        return {
            **result,
            "candidate_located": bool(result.get("results")),
            "document_recovered": False,
            "official_source_verified": False,
            "source_status": "NOT_VERIFIED",
            "identity_status": "NOT_VERIFIED",
            "passage_status": "NOT_VERIFIED",
            "ratio_decidendi_verified": False,
            "review_required": True,
        }

    @mcp.tool()
    def prepare_scj_search(
        q: str,
        chamber: str | None = None,
        matter: str | None = None,
        year_from: int | None = None,
        year_to: int | None = None,
    ) -> dict:
        """Prepara una consulta de jurisprudencia SCJ mediante el servicio REST existente; no ejecuta la búsqueda."""
        return prepare_scj(
            q=q,
            chamber=chamber,
            matter=matter,
            year_from=year_from,
            year_to=year_to,
        )

    @mcp.tool()
    async def recover_official_pdf(
        url: str,
        expected_court: str,
        expected_number: OfficialDecisionNumber,
    ) -> dict:
        """Localiza un PDF en una ficha oficial, lo recupera y verifica separadamente fuente e identidad."""
        request = official_pdf_request_model(
            url=url,
            expected_court=expected_court,
            expected_number=expected_number,
        )
        resolution = await resolve_official_pdf(request)
        candidates = resolution.get("candidates") or []
        if not candidates:
            return {
                "status": resolution.get("status", "PDF_NOT_DISCOVERED"),
                "candidate_located": False,
                "document_recovered": False,
                "official_source_verified": False,
                "source_status": "NOT_VERIFIED",
                "identity_verified": False,
                "identity_status": "NOT_VERIFIED",
                "passage_status": "NOT_VERIFIED",
                "resolution": resolution,
                "candidate_count": 0,
                "attempts": [],
                "review_required": True,
            }

        attempts = []
        for candidate_url in candidates[:3]:
            candidate_request = official_pdf_request_model(
                url=candidate_url,
                expected_court=expected_court,
                expected_number=expected_number,
            )
            result = await verify_official_pdf(candidate_request)
            attempt = {
                "candidate_url": candidate_url,
                "status": result.get("status"),
                "document_type": result.get("document_type"),
                "page_count": result.get("page_count"),
                "official_source_verified": result.get(
                    "official_source_verified", False
                ),
                "identity_verified": result.get("identity_verified", False),
                "sha256_pdf": result.get("sha256_pdf"),
            }
            attempts.append(attempt)
            if result.get("identity_verified"):
                return {
                    "status": "IDENTITY_VERIFIED",
                    "candidate_located": True,
                    "document_recovered": True,
                    "official_source_verified": result[
                        "official_source_verified"
                    ],
                    "source_status": (
                        "VERIFIED_SOURCE"
                        if result["official_source_verified"]
                        else "NOT_VERIFIED"
                    ),
                    "identity_verified": True,
                    "identity_status": "VERIFIED_IDENTITY",
                    "passage_status": "NOT_VERIFIED",
                    "resolution": resolution,
                    "candidate_count": len(candidates),
                    "remaining_candidates_unattempted": (
                        len(candidates) - len(attempts)
                    ),
                    "document": result,
                    "attempts": attempts,
                    "review_required": True,
                }

        last_result = result
        recovered = any(
            attempt["document_type"] == "PDF" for attempt in attempts
        )
        return {
            "status": (
                "DOCUMENT_RECOVERED_IDENTITY_UNVERIFIED"
                if recovered else "CANDIDATE_LOCATED_NOT_RECOVERED"
            ),
            "candidate_located": True,
            "document_recovered": recovered,
            "official_source_verified": any(
                attempt["official_source_verified"] for attempt in attempts
            ),
            "source_status": (
                "VERIFIED_SOURCE"
                if any(
                    attempt["official_source_verified"] for attempt in attempts
                )
                else "NOT_VERIFIED"
            ),
            "identity_verified": False,
            "identity_status": "NOT_VERIFIED",
            "passage_status": "NOT_VERIFIED",
            "resolution": resolution,
            "candidate_count": len(candidates),
            "remaining_candidates_unattempted": (
                len(candidates) - len(attempts)
            ),
            "document": last_result,
            "attempts": attempts,
            "review_required": True,
        }

    @mcp.tool()
    async def extract_decision_passages(
        url: str,
        expected_court: str,
        expected_number: OfficialDecisionNumber,
        legal_issue: str | None = None,
        limit: int = 12,
    ) -> dict:
        """Extrae fundamentos y dispositivo con páginas y contexto desde un PDF oficial revalidado."""
        request = foundation_request_model(
            url=url,
            expected_court=expected_court,
            expected_number=expected_number,
            legal_issue=legal_issue,
            limit=limit,
        )
        return await extract_passages(request)

    @mcp.tool()
    async def propose_ratio_decidendi(
        url: str,
        expected_court: str,
        expected_number: OfficialDecisionNumber,
        legal_issue: str | None = None,
        limit: int = 12,
    ) -> dict:
        """Propone pasajes literales candidatos a ratio; nunca certifica criterio y exige revisión jurídica humana."""
        request = foundation_request_model(
            url=url,
            expected_court=expected_court,
            expected_number=expected_number,
            legal_issue=legal_issue,
            limit=limit,
        )
        evidence = await extract_passages(request)
        if evidence.get("identity_status") != "VERIFIED_IDENTITY":
            return {
                "status": "PROPOSAL_UNAVAILABLE_IDENTITY_UNVERIFIED",
                "search_executed": False,
                "candidate_located": False,
                "document_recovered": evidence.get(
                    "document_recovered", False
                ),
                "official_source_verified": (
                    evidence.get("source_status") == "VERIFIED_SOURCE"
                ),
                "source_status": evidence.get("source_status"),
                "identity_status": evidence.get("identity_status"),
                "passage_status": evidence.get("passage_status"),
                "supporting_passages": [],
                "ratio_candidate_located": False,
                "proposed_ratio_decidendi": None,
                "ratio_review_status": "PENDING_HUMAN_REVIEW",
                "ratio_decidendi_verified": False,
                "review_required": True,
            }

        candidates = _rank_ratio_candidates(evidence, legal_issue)

        proposed = None
        if candidates:
            passage = candidates[0]
            proposed = {
                "formulation": (
                    f"Valorar si el fundamento {passage.get('foundation_number')} "
                    f"de {passage.get('tribunal')} expresa la regla decisoria: "
                    f"«{passage['passage']}»"
                ),
                "formulation_basis": "VERBATIM_VERIFIED_PASSAGE",
                "decision_court": passage.get("tribunal"),
                "decision_number": passage.get("decision_number"),
                "page": passage.get("page"),
                "foundation_number": passage.get("foundation_number"),
                "reasoning_origin": passage.get("reasoning_origin"),
                "attribution_verified": False,
                "issue_terms_matched": passage["issue_terms_matched"],
                "issue_relevance": passage["issue_relevance"],
                "dispositive_relationship": passage[
                    "dispositive_relationship"
                ],
            }
        return {
            "status": (
                "PROVISIONAL_RATIO_CANDIDATE"
                if proposed else "NO_ATTRIBUTABLE_PASSAGE_CANDIDATE"
            ),
            "search_executed": False,
            "candidate_located": False,
            "document_recovered": evidence.get("document_recovered", True),
            "official_source_verified": (
                evidence.get("source_status") == "VERIFIED_SOURCE"
            ),
            "source_status": evidence.get("source_status"),
            "identity_status": evidence.get("identity_status"),
            "passage_status": evidence.get("passage_status"),
            "decision_court": evidence.get("deciding_court"),
            "decision_number": (evidence.get("metadata") or {}).get(
                "decision_number"
            ),
            "ratio_candidate_located": bool(candidates),
            "proposed_ratio_decidendi": proposed,
            "supporting_passages": candidates,
            "ratio_review_status": "PENDING_HUMAN_REVIEW",
            "ratio_decidendi_verified": False,
            "review_required": True,
            "human_review_notice": (
                "La propuesta es una hipótesis basada en texto literal; la "
                "autoría judicial y la ratio decidendi requieren revisión "
                "jurídica humana."
            ),
        }

    @mcp.tool()
    async def register_verified_decision(
        url: str,
        expected_court: str,
        expected_number: OfficialDecisionNumber,
        legal_issue: str | None = None,
        max_passages: int = 6,
    ) -> dict:
        """Registra explícitamente un PDF oficial verificado en PostgreSQL; operación exclusiva de administradores."""
        user = _authenticated_mcp_user.get()
        if not user or user.get("role") != "admin":
            raise HTTPException(403, "Se requiere permiso administrativo.")
        if expected_court == "TC":
            request = tc_reference_request_model(
                url=url,
                expected_number=expected_number,
                legal_issue=legal_issue,
            )
            return await register_tc_reference(request)
        if expected_court != "SCJ":
            raise ValueError("expected_court debe ser TC o SCJ.")
        request = register_request_model(
            url=url,
            expected_court=expected_court,
            expected_number=expected_number,
            legal_issue=legal_issue,
            max_passages=max_passages,
        )
        return await register_decision(request)

    @mcp.tool()
    def get_verified_decisions(
        q: str,
        court: str | None = None,
        limit: int = 10,
    ) -> dict:
        """Recupera del PostgreSQL existente solo documentos PDF previamente verificados."""
        if court is not None and court not in {"SCJ", "TC"}:
            raise ValueError("court debe ser SCJ o TC.")
        if not q.strip():
            raise ValueError("q no puede estar vacío.")
        items = find_verified_decisions(query=q, court=court, limit=limit)
        return {
            "status": "VERIFIED_DOCUMENTS_FOUND" if items else "NO_VERIFIED_DOCUMENTS",
            "query": q,
            "items": items,
            "count": len(items),
            "search_executed": True,
            "candidate_located": bool(items),
            "document_recovered": False,
            "official_source_verified": bool(items),
            "identity_status": (
                "VERIFIED_IDENTITY" if items else "NOT_VERIFIED"
            ),
            "passage_status": "NOT_VERIFIED",
            "retrieval_mode": "POSTGRESQL_VERIFIED_DOCUMENTS",
        }

    @mcp.tool()
    def get_verified_decision_document(decision_id: int) -> dict:
        """Recupera bytes PDF almacenados en PostgreSQL tras verificar nuevamente su SHA-256."""
        document = get_verified_document(decision_id)
        document_bytes = document.pop("document_bytes")
        document.update({
            "status": "DOCUMENT_RECOVERED_FROM_POSTGRESQL",
            "search_executed": False,
            "candidate_located": True,
            "official_source_verified": True,
            "identity_verified": True,
        })
        document["document_bytes_base64"] = base64.b64encode(
            document_bytes
        ).decode("ascii")
        document["retrieval_mode"] = "POSTGRESQL_PERSISTED_PDF"
        return document

    @mcp.tool()
    async def verify_official_document(
        url: str,
        expected_court: str | None = None,
        expected_number: OfficialDecisionNumber | None = None,
    ) -> dict:
        """Verifica una fuente oficial mediante el endpoint REST existente; no determina la ratio decidendi."""
        request = verify_request_model(
            url=url,
            expected_court=expected_court,
            expected_number=expected_number,
        )
        result = await verify_document(request)
        identity_verified = bool(
            expected_court
            and expected_number
            and result.get("expected_court_match")
            and result.get("expected_number_match")
        )
        return {
            **result,
            "document_recovered": result.get("document_type") == "PDF",
            "official_source_verified": bool(result.get("official_domain")),
            "identity_verified": identity_verified,
            "identity_status": (
                "VERIFIED_IDENTITY" if identity_verified else "NOT_VERIFIED"
            ),
            "passage_status": "NOT_VERIFIED",
            "ratio_decidendi_verified": False,
            "review_required": True,
        }

    mcp_app = mcp.streamable_http_app(
        json_response=True,
        stateless_http=True,
        transport_security=_transport_security_settings(),
    )
    mcp_app.add_middleware(MCPAuthMiddleware)
    app.add_middleware(OAuthProtectedResourceMetadataMiddleware)
    app.router.routes.append(
        Route(MCP_PATH, endpoint=mcp_app, include_in_schema=False)
    )

    parent_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan_context(application):
        async with parent_lifespan(application):
            async with mcp_app.router.lifespan_context(mcp_app):
                yield

    app.router.lifespan_context = lifespan_context
