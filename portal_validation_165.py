import json
import secrets
from html import escape
from urllib.parse import parse_qs

import httpx
from fastapi import HTTPException
from fastapi.responses import HTMLResponse
from web_client_165 import (
    router, active_session, HEADERS
)

OPERATIONS = {
    "tc_tema": (
        "/v1/tc/search",
        {"q": "tutela judicial efectiva", "limit": 5}
    ),
    "tc_sentencia": (
        "/v1/tc/search",
        {"q": "TC/0019/24", "limit": 3}
    ),
    "scj_tema": (
        "/v1/scj/search",
        {
            "q": "responsabilidad civil por el hecho de las cosas",
            "chamber": "Primera Sala",
            "matter": "Civil",
            "year_from": 2020,
            "year_to": 2026,
            "limit": 5
        }
    ),
    "scj_sentencia": (
        "/v1/scj/search",
        {"q": "SCJ-PS-22-1231", "limit": 3}
    ),
    "database": (
        "/v1/admin/database/health", {}
    )
}

from fastapi import Request

@router.get("/portal/validation", include_in_schema=False)
async def validation_page(request: Request):
    session = await active_session(request)
    if not session:
        raise HTTPException(401, "Sesión requerida.")

    buttons = "".join(
        f"<button name='operation' value='{name}'>"
        f"{escape(name)}</button><br><br>"
        for name in OPERATIONS
    )

    return HTMLResponse(
        "<html lang='es'><meta charset='utf-8'>"
        "<h1>Validación funcional ELCOHE-JURIS RD</h1>"
        "<form method='post' action='/portal/validation'>"
        f"<input type='hidden' name='csrf' "
        f"value='{escape(session['csrf'], quote=True)}'>"
        + buttons +
        "</form><a href='/portal'>Volver al portal</a></html>",
        headers=HEADERS
    )

@router.post("/portal/validation", include_in_schema=False)
async def execute_validation(request: Request):
    session = await active_session(request)
    if not session:
        raise HTTPException(401, "Sesión requerida.")

    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 2048:
            raise HTTPException(400, "Solicitud inválida.")

    try:
        form = parse_qs(raw.decode())
    except UnicodeError:
        raise HTTPException(400, "Solicitud inválida.")

    csrf = form.get("csrf", [""])[0]
    if not secrets.compare_digest(session["csrf"], csrf):
        raise HTTPException(403, "Solicitud no autorizada.")

    operation = form.get("operation", [""])[0]
    if operation not in OPERATIONS:
        raise HTTPException(400, "Operación no permitida.")

    path, params = OPERATIONS[operation]

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=request.app),
            base_url="https://internal.local",
            timeout=90,
            follow_redirects=False
        ) as client:
            import asyncio
            result = await asyncio.wait_for(
                client.get(
                    path,
                    params=params,
                    headers={
                        "Authorization":
                            "Bearer " + session["token"]
                    }
                ),
                timeout=90
            )
        status = result.status_code
        try:
            output = json.dumps(
                result.json(), ensure_ascii=False, indent=2
            )
        except ValueError:
            output = "Respuesta no JSON."
    except (httpx.HTTPError, TimeoutError):
        status = 504
        output = "No pudo completarse la operación."

    return HTMLResponse(
        "<html lang='es'><meta charset='utf-8'>"
        f"<h1>Resultado: {escape(operation)}</h1>"
        f"<p>HTTP {status}</p>"
        f"<pre>{escape(output[:60000])}</pre>"
        "<a href='/portal/validation'>Volver a validación</a>"
        "</html>",
        headers=HEADERS
    )
