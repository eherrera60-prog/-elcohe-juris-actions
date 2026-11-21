from __future__ import annotations

from contextlib import asynccontextmanager
import os
from urllib.parse import urlparse

from fastapi import HTTPException
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

import auth_access


APP_VERSION = "1.6.5"
MCP_PATH = "/mcp"
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


def _external_origin() -> str | None:
    external_url = os.getenv("RENDER_EXTERNAL_URL", "").strip()
    if not external_url:
        return None
    parsed = urlparse(external_url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise HTTPException(503, "RENDER_EXTERNAL_URL debe ser una URL HTTPS de origen.")
    return f"https://{parsed.netloc}"


def _resource_url(request: Request) -> str:
    origin = _external_origin()
    if origin:
        return f"{origin}{MCP_PATH}"
    return f"{str(request.base_url).rstrip('/')}{MCP_PATH}"


def _transport_security_settings() -> TransportSecuritySettings:
    origin = _external_origin()
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
            await auth_access.require_active_user(request, token)
        except HTTPException as exc:
            headers = dict(exc.headers or {})
            if exc.status_code == 401:
                origin = _external_origin() or str(request.base_url).rstrip("/")
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
        return await call_next(request)


def install_mcp(
    app,
    *,
    search_tc,
    prepare_scj,
    verify_document,
    verify_request_model,
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
        return await search_tc(q=q, year=year, limit=limit)

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
    async def verify_official_document(
        url: str,
        expected_court: str | None = None,
        expected_number: str | None = None,
    ) -> dict:
        """Verifica una fuente oficial mediante el endpoint REST existente; no determina la ratio decidendi."""
        request = verify_request_model(
            url=url,
            expected_court=expected_court,
            expected_number=expected_number,
        )
        return await verify_document(request)

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
