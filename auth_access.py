"""OIDC access validation and persistent per-user authorization."""
from __future__ import annotations

from functools import lru_cache
import os
from typing import Literal
from urllib.parse import urlparse, urlencode

import jwt
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Security
from fastapi.security import OAuth2AuthorizationCodeBearer
from jwt import PyJWKClient
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool


AUTH0_AUDIENCE = "https://elcohe-juris-163-test.onrender.com"
REQUIRED_PERMISSION = "access:jurisprudence"


def _auth0_domain_url() -> str:
    domain = os.getenv("AUTH0_DOMAIN", "").strip()
    if not domain:
        raise HTTPException(503, "OAuth no configurado: falta AUTH0_DOMAIN.")
    if "://" not in domain:
        domain = f"https://{domain}"
    parsed = urlparse(domain)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise HTTPException(503, "AUTH0_DOMAIN debe ser un dominio HTTPS de Auth0.")
    try:
        if parsed.port is not None:
            raise HTTPException(503, "AUTH0_DOMAIN debe ser un dominio HTTPS de Auth0.")
    except ValueError as exc:
        raise HTTPException(503, "AUTH0_DOMAIN debe ser un dominio HTTPS de Auth0.") from exc
    return f"https://{parsed.netloc}"


def _oauth_url(suffix: str) -> str:
    try:
        base_url = _auth0_domain_url()
    except HTTPException:
        base_url = "https://auth0-domain-not-configured.invalid"
    if suffix == "/authorize":
        return f"{base_url}{suffix}?{urlencode({'audience': AUTH0_AUDIENCE})}"
    return f"{base_url}{suffix}"


oauth2_scheme = OAuth2AuthorizationCodeBearer(
    authorizationUrl=_oauth_url("/authorize"),
    tokenUrl=_oauth_url("/oauth/token"),
    scopes={REQUIRED_PERMISSION: "Acceso a endpoints jurisprudenciales"},
    auto_error=False,
)


class UserStatusRequest(BaseModel):
    subject: str = Field(min_length=1, max_length=255, pattern=r"^\S+$")
    status: Literal["ACTIVE", "SUSPENDED", "REVOKED"]


router = APIRouter(prefix="/v1/admin", tags=["Administración de acceso"])


def database_url() -> str:
    value = os.getenv("DATABASE_URL", "").strip()
    parsed = urlparse(value)
    if parsed.scheme not in ("postgres", "postgresql") or not parsed.hostname:
        raise HTTPException(503, "Autorización no configurada: falta un DATABASE_URL PostgreSQL válido.")
    return value


def connect_db():
    try:
        import psycopg

        return psycopg.connect(database_url(), connect_timeout=8, sslmode="require")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, f"No se pudo conectar al almacenamiento de autorización ({type(exc).__name__}).")


AUTH_SCHEMA = """
CREATE TABLE IF NOT EXISTS elcohe_authorized_users (
    subject TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('ACTIVE','SUSPENDED','REVOKED')),
    authorized_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS elcohe_access_audit (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    actor_subject TEXT NOT NULL,
    target_subject TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('ACTIVE','SUSPENDED','REVOKED')),
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_elcohe_access_audit_time ON elcohe_access_audit(occurred_at DESC);
"""


def init_auth_schema(conn) -> None:
    with conn.cursor() as cur:
        for statement in AUTH_SCHEMA.split(";"):
            if statement.strip():
                cur.execute(statement)


def get_authorized_user(subject: str) -> dict | None:
    try:
        with connect_db() as conn:
            init_auth_schema(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT subject,status,authorized_by,created_at,updated_at "
                    "FROM elcohe_authorized_users WHERE subject=%s",
                    (subject,),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                return dict(zip((column.name for column in cur.description), row))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, f"No se pudo consultar la autorización del usuario ({type(exc).__name__}).")


def set_user_status(subject: str, status: str, actor_subject: str) -> None:
    try:
        with connect_db() as conn:
            init_auth_schema(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO elcohe_authorized_users (subject,status,authorized_by)
                    VALUES (%s,%s,%s)
                    ON CONFLICT (subject) DO UPDATE SET
                        status=EXCLUDED.status,
                        authorized_by=EXCLUDED.authorized_by,
                        updated_at=NOW()
                    """,
                    (subject, status, actor_subject),
                )
                cur.execute(
                    "INSERT INTO elcohe_access_audit (actor_subject,target_subject,action) "
                    "VALUES (%s,%s,%s)",
                    (actor_subject, subject, status),
                )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, f"No se pudo actualizar la autorización del usuario ({type(exc).__name__}).")


def list_authorized_users(limit: int) -> list[dict]:
    try:
        with connect_db() as conn:
            init_auth_schema(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT subject,status,authorized_by,created_at,updated_at "
                    "FROM elcohe_authorized_users ORDER BY updated_at DESC LIMIT %s",
                    (limit,),
                )
                fields = [column.name for column in cur.description]
                return [dict(zip(fields, row)) for row in cur.fetchall()]
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, f"No se pudo consultar la lista de usuarios ({type(exc).__name__}).")


def list_access_audit(limit: int) -> list[dict]:
    try:
        with connect_db() as conn:
            init_auth_schema(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT actor_subject,target_subject,action,occurred_at "
                    "FROM elcohe_access_audit ORDER BY occurred_at DESC,id DESC LIMIT %s",
                    (limit,),
                )
                fields = [column.name for column in cur.description]
                return [dict(zip(fields, row)) for row in cur.fetchall()]
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, f"No se pudo consultar la auditoría de acceso ({type(exc).__name__}).")


def bootstrap_admin_subjects() -> frozenset[str]:
    return frozenset(
        subject.strip()
        for subject in os.getenv("ELCOHE_BOOTSTRAP_ADMIN_SUBJECTS", "").split(",")
        if subject.strip()
    )


@lru_cache(maxsize=4)
def _jwks_client(url: str) -> PyJWKClient:
    return PyJWKClient(url, cache_keys=True)


def decode_access_token(token: str) -> dict:
    domain_url = _auth0_domain_url()
    issuer = f"{domain_url}/"
    jwks_url = f"{domain_url}/.well-known/jwks.json"
    try:
        signing_key = _jwks_client(jwks_url).get_signing_key_from_jwt(token).key
    except jwt.PyJWKClientConnectionError as exc:
        raise HTTPException(503, "No se pudo consultar las claves públicas del proveedor de identidad.") from exc
    except jwt.PyJWKClientError as exc:
        raise HTTPException(401, "Token OAuth inválido.") from exc
    try:
        claims = jwt.decode(
            token,
            signing_key,
            algorithms=["RS256"],
            audience=AUTH0_AUDIENCE,
            issuer=issuer,
            options={"require": ["exp", "iat", "sub"]},
        )
    except jwt.InvalidTokenError as exc:
        raise HTTPException(
            401, "Token OAuth inválido o expirado.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject or len(subject) > 255:
        raise HTTPException(401, "El token no contiene una identidad válida.")
    return claims


def _claim_values(value: object) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, (list, tuple, set)):
        return {item for item in value if isinstance(item, str)}
    return set()


def _has_required_access(claims: dict) -> bool:
    permissions = _claim_values(claims.get("permissions"))
    return REQUIRED_PERMISSION in permissions


async def require_active_user(
    request: Request,
    token: str | None = Security(oauth2_scheme, scopes=[REQUIRED_PERMISSION]),
) -> dict:
    if request.url.path == "/health":
        return {"sub": None, "role": "public"}
    if not token:
        raise HTTPException(
            401,
            "Se requiere autenticación OAuth.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    claims = await run_in_threadpool(decode_access_token, token)
    if not _has_required_access(claims):
        raise HTTPException(403, "El token no incluye el permiso requerido.")
    subject = claims["sub"]
    if subject in bootstrap_admin_subjects():
        return {"sub": subject, "role": "admin"}
    user = await run_in_threadpool(get_authorized_user, subject)
    if not user or user["status"] != "ACTIVE":
        raise HTTPException(403, "Usuario no autorizado, suspendido o revocado.")
    return {"sub": subject, "role": "user"}


def require_admin(user: dict = Depends(require_active_user)) -> dict:
    if user.get("role") != "admin":
        raise HTTPException(403, "Se requiere permiso administrativo.")
    return user


def check_database_connection() -> dict[str, str]:
    try:
        with connect_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                if cur.fetchone() != (1,):
                    raise RuntimeError("Respuesta inesperada de PostgreSQL.")
        return {"status": "DATABASE_CONNECTED"}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            503, f"No se pudo verificar la conexión PostgreSQL ({type(exc).__name__})."
        ) from exc


@router.get(
    "/database/health",
    operation_id="checkDatabaseConnection",
    include_in_schema=False,
)
def admin_database_health(_admin: dict = Depends(require_admin)):
    return check_database_connection()


@router.get("/users", operation_id="listAuthorizedUsers", include_in_schema=False)
def admin_list_users(
    limit: int = Query(default=100, ge=1, le=500),
    _admin: dict = Depends(require_admin),
):
    return {"users": list_authorized_users(limit)}


@router.put("/users/status", operation_id="setAuthorizedUserStatus", include_in_schema=False)
def admin_set_user_status(
    req: UserStatusRequest,
    admin: dict = Depends(require_admin),
):
    set_user_status(req.subject, req.status, admin["sub"])
    return {"subject": req.subject, "status": req.status}


@router.get("/audit", operation_id="listAccessAudit", include_in_schema=False)
def admin_list_audit(
    limit: int = Query(default=100, ge=1, le=500),
    _admin: dict = Depends(require_admin),
):
    return {"events": list_access_audit(limit)}
