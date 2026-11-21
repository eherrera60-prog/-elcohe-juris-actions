
from __future__ import annotations

import base64
import hashlib
import os
import secrets
import time
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from auth_access import (
    AUTH0_AUDIENCE,
    REQUIRED_PERMISSION,
    _auth0_domain_url,
    require_active_user,
    decode_access_token,
)

router = APIRouter()

BASE = "https://elcohe-juris-165-produccion.onrender.com"
COOKIE = "__Host-elcohe-session"
FLOW_COOKIE = "__Host-elcohe-oauth"

FLOWS = {}
SESSIONS = {}

MAX_FLOWS = 1000
MAX_SESSIONS = 1000
SESSION_TTL = 3600

HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy":
        "default-src 'none'; form-action 'self'; "
        "frame-ancestors 'none'; base-uri 'none'",
}


def configuration():
    client = os.getenv("AUTH0_CLIENT_ID", "").strip()
    secret = os.getenv("AUTH0_CLIENT_SECRET", "").strip()
    audience = os.getenv("AUTH0_AUDIENCE", AUTH0_AUDIENCE).strip()

    if not client or not secret:
        raise HTTPException(503, "Cliente OAuth no configurado.")

    if audience != AUTH0_AUDIENCE:
        raise HTTPException(503, "Audiencia OAuth incompatible.")

    return client, secret, audience


def cleanup():
    now = time.time()
    for store in (FLOWS, SESSIONS):
        for key, value in list(store.items()):
            if value["expires"] <= now:
                store.pop(key, None)


def cookie(response, name, value, ttl):
    response.set_cookie(
        name, value,
        max_age=ttl,
        secure=True,
        httponly=True,
        samesite="lax",
        path="/",
    )


def clear_cookie(response, name):
    response.delete_cookie(
        name, path="/",
        secure=True, httponly=True, samesite="lax",
    )


def redirect(url):
    return RedirectResponse(url, status_code=303, headers=HEADERS)


@router.get("/", include_in_schema=False)
async def home():
    return redirect("/portal")


def _callback_url():
    base = os.getenv("RENDER_EXTERNAL_URL", BASE).strip()
    try:
        parsed = urlparse(base)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
            or parsed.port not in (None, 443)
        ):
            raise HTTPException(503, "URL pública del servicio no válida.")
    except ValueError as exc:
        raise HTTPException(503, "URL pública del servicio no válida.") from exc
    return base.rstrip("/") + "/auth/callback"


@router.get("/auth/login", include_in_schema=False)
async def login(request: Request):
    client, _, audience = configuration()
    cleanup()

    if len(FLOWS) >= MAX_FLOWS:
        raise HTTPException(503, "Intente nuevamente más tarde.")

    identifier = secrets.token_urlsafe(32)
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)

    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()

    FLOWS[identifier] = {
        "state": state,
        "verifier": verifier,
        "expires": time.time() + 300,
    }

    params = {
        "response_type": "code",
        "client_id": client,
        "redirect_uri": _callback_url(),
        "audience": audience,
        "scope": REQUIRED_PERMISSION,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }

    response = redirect(
        _auth0_domain_url() + "/authorize?" + urlencode(params)
    )

    cookie(response, FLOW_COOKIE, identifier, 300)
    return response


@router.get("/auth/callback", include_in_schema=False)
async def callback(request: Request):
    cleanup()

    flow = FLOWS.pop(
        request.cookies.get(FLOW_COOKIE, ""), None
    )

    state = request.query_params.get("state", "")
    code = request.query_params.get("code", "")

    if (
        not flow or not code or not state
        or not secrets.compare_digest(flow["state"], state)
        or request.query_params.get("error")
    ):
        raise HTTPException(400, "Respuesta OAuth inválida o expirada.")

    client, secret, _ = configuration()

    try:
        async with httpx.AsyncClient(
            timeout=20, follow_redirects=False
        ) as http:
            result = await http.post(
                _auth0_domain_url() + "/oauth/token",
                data={
                    "grant_type": "authorization_code",
                    "client_id": client,
                    "client_secret": secret,
                    "code": code,
                    "redirect_uri": _callback_url(),
                    "code_verifier": flow["verifier"],
                },
            )
        result.raise_for_status()
        token = result.json()["access_token"]

        if not isinstance(token, str) or not token:
            raise ValueError("Token inválido")

    except (httpx.HTTPError, KeyError, ValueError, TypeError):
        raise HTTPException(502, "No se pudo completar OAuth.")

    await require_active_user(request, token=token)
    claims = await run_in_threadpool(decode_access_token, token)

    ttl = min(SESSION_TTL, int(claims["exp"] - time.time()))

    if ttl <= 0:
        raise HTTPException(401, "Token expirado.")

    cleanup()

    if len(SESSIONS) >= MAX_SESSIONS:
        raise HTTPException(503, "Capacidad de sesiones alcanzada.")

    SESSIONS.pop(request.cookies.get(COOKIE, ""), None)

    identifier = secrets.token_urlsafe(32)
    SESSIONS[identifier] = {
        "token": token,
        "csrf": secrets.token_urlsafe(32),
        "expires": time.time() + ttl,
    }

    response = redirect("/portal")
    clear_cookie(response, FLOW_COOKIE)
    cookie(response, COOKIE, identifier, ttl)
    return response


async def active_session(request: Request):
    cleanup()
    identifier = request.cookies.get(COOKIE, "")
    session = SESSIONS.get(identifier)

    if not session:
        return None

    try:
        user = await require_active_user(
            request, token=session["token"]
        )
    except HTTPException:
        SESSIONS.pop(identifier, None)
        raise

    session["role"] = user["role"]
    return session


@router.get("/portal", include_in_schema=False)
async def portal(request: Request):
    session = await active_session(request)

    if not session:
        return HTMLResponse(
            "<h1>ELCOHE-JURIS RD 1.6.5</h1>"
            "<p>Portal de investigación jurisprudencial.</p>"
            "<a href='/auth/login'>Iniciar sesión</a>",
            headers=HEADERS,
        )

    csrf = session["csrf"]
    admin_action = ""
    if session.get("role") == "admin":
        admin_action = (
            "<form method='post' action='/portal/admin/activate-m2m'>"
            f"<input type='hidden' name='csrf' value='{csrf}'>"
            "<button type='submit'>Activar cliente M2M</button></form>"
        )

    return HTMLResponse(
        "<!doctype html><html lang='es'><meta charset='utf-8'>"
        "<title>ELCOHE-JURIS RD</title>"
        "<h1>ELCOHE-JURIS RD 1.6.5</h1>"
        "<p>Acceso autorizado al portal.</p>"
        "<p>Sesión jurisprudencial activa.</p>"
        "<p><a href='/portal/validation'>"
        "Validación funcional</a></p>"
        f"{admin_action}"
        "<form method='post' action='/auth/logout'>"
        f"<input type='hidden' name='csrf' value='{csrf}'>"
        "<button>Cerrar sesión</button></form></html>",
        headers=HEADERS,
    )


async def validate_csrf(request: Request, expected: str) -> None:
    raw = await request.body()
    if len(raw) > 2048:
        raise HTTPException(400, "Solicitud inválida.")

    try:
        csrf = parse_qs(raw.decode()).get("csrf", [""])[0]
    except UnicodeError:
        raise HTTPException(400, "Solicitud inválida.")

    if not secrets.compare_digest(expected, csrf):
        raise HTTPException(403, "Solicitud no autorizada.")


@router.post("/auth/logout", include_in_schema=False)
async def logout(request: Request):
    cleanup()

    identifier = request.cookies.get(COOKIE, "")
    session = SESSIONS.get(identifier)

    if not session:
        response = redirect("/portal")
        clear_cookie(response, COOKIE)
        return response

    await validate_csrf(request, session["csrf"])

    SESSIONS.pop(identifier, None)

    response = redirect("/portal")
    clear_cookie(response, COOKIE)
    return response


@router.post("/portal/admin/activate-m2m", include_in_schema=False)
async def activate_m2m(request: Request):
    session = await active_session(request)
    if not session:
        raise HTTPException(401, "Se requiere una sesión autenticada.")
    if session.get("role") != "admin":
        raise HTTPException(403, "Se requiere permiso administrativo.")

    await validate_csrf(request, session["csrf"])

    client_id = os.getenv("AUTH0_CLIENT_ID", "").strip()
    if not client_id or len(client_id) > 247 or any(
        character.isspace() for character in client_id
    ):
        raise HTTPException(503, "Cliente M2M no configurado.")

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=request.app, raise_app_exceptions=False
            ),
            base_url="https://internal.local",
            timeout=20,
            follow_redirects=False,
        ) as http:
            result = await http.put(
                "/v1/admin/users/status",
                json={
                    "subject": f"{client_id}@clients",
                    "status": "ACTIVE",
                },
                headers={"Authorization": f"Bearer {session['token']}"},
            )
    except httpx.HTTPError as exc:
        raise HTTPException(502, "No se pudo completar la autorización M2M.") from exc

    return HTMLResponse(
        f"HTTP {result.status_code}",
        status_code=result.status_code,
        headers=HEADERS,
    )
