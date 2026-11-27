
"""OAuth bridge for GPT Actions; isolated from MCP and web portal."""

import os
from urllib.parse import urlencode, parse_qsl

import httpx
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import RedirectResponse, Response

from auth_access import (
    _auth0_domain_url,
    AUTH0_AUDIENCE,
    REQUIRED_PERMISSION,
)

router = APIRouter(include_in_schema=False)

NO_CACHE = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
}


def validate_client(client_id, redirect_uri):
    expected_client = os.getenv("GPT_ACTIONS_CLIENT_ID", "").strip()
    expected_callback = os.getenv("GPT_ACTIONS_CALLBACK_URL", "").strip()

    if not expected_client or not expected_callback:
        raise HTTPException(503, "GPT OAuth pendiente de configuración.")

    if client_id != expected_client:
        raise HTTPException(400, "Cliente OAuth no autorizado.")

    if redirect_uri != expected_callback:
        raise HTTPException(400, "Callback OAuth no autorizado.")


@router.get("/oauth/authorize")
async def authorize(request: Request):
    params = dict(request.query_params)

    validate_client(
        params.get("client_id"),
        params.get("redirect_uri"),
    )

    if params.get("response_type") != "code":
        raise HTTPException(400, "Flujo OAuth no permitido.")

    if not params.get("state"):
        raise HTTPException(400, "Falta el parámetro state.")

    allowed = {
        "client_id", "redirect_uri", "response_type",
        "scope", "state", "code_challenge",
        "code_challenge_method", "nonce",
    }

    params = {k: v for k, v in params.items() if k in allowed}

    params["audience"] = AUTH0_AUDIENCE
    params["scope"] = (
        "openid profile email " + REQUIRED_PERMISSION
    )

    return RedirectResponse(
        _auth0_domain_url() + "/authorize?" + urlencode(params),
        status_code=302,
        headers=NO_CACHE,
    )


@router.post("/oauth/token")
async def token(request: Request):
    content_type = request.headers.get("content-type", "")

    if not content_type.startswith(
        "application/x-www-form-urlencoded"
    ):
        raise HTTPException(415, "Formato OAuth no permitido.")

    body = bytearray()

    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 16384:
            raise HTTPException(413, "Solicitud demasiado extensa.")

    try:
        pairs = parse_qsl(
            body.decode("utf-8"),
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=20,
        )
    except (ValueError, UnicodeError):
        raise HTTPException(400, "Solicitud OAuth inválida.")

    params = dict(pairs)

    if len(params) != len(pairs):
        raise HTTPException(400, "Parámetros OAuth duplicados.")

    if params.get("grant_type") != "authorization_code":
        raise HTTPException(400, "Flujo OAuth no permitido.")

    validate_client(
        params.get("client_id"),
        params.get("redirect_uri"),
    )

    if not params.get("client_secret") or not params.get("code"):
        raise HTTPException(400, "Solicitud OAuth incompleta.")

    allowed = {
        "grant_type", "client_id", "client_secret",
        "code", "redirect_uri", "code_verifier",
    }

    params = {k: v for k, v in params.items() if k in allowed}

    try:
        async with httpx.AsyncClient(
            timeout=20,
            follow_redirects=False,
        ) as client:
            result = await client.post(
                _auth0_domain_url() + "/oauth/token",
                data=params,
            )
    except httpx.HTTPError:
        raise HTTPException(502, "Proveedor OAuth no disponible.")

    if result.status_code != 200:
        raise HTTPException(400, "Intercambio OAuth rechazado.")

    try:
        payload = result.json()
        if not isinstance(payload, dict) or not payload.get("access_token"):
            raise ValueError()
    except ValueError:
        raise HTTPException(502, "Respuesta OAuth inválida.")

    return Response(
        content=result.content,
        media_type="application/json",
        headers=NO_CACHE,
    )
