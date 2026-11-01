import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import app
from auth_access import (
    AUTH0_AUDIENCE,
    REQUIRED_PERMISSION,
    check_database_connection,
    decode_access_token,
    require_active_user,
)


class IndividualAccessTests(unittest.TestCase):
    def setUp(self):
        app.dependency_overrides.clear()
        self.users = {}
        self.environment = patch.dict(
            os.environ,
            {
                "ELCOHE_BOOTSTRAP_ADMIN_SUBJECTS": "auth0|bootstrap-admin",
                "AUTH0_DOMAIN": "identity.example",
            },
        )
        self.environment.start()
        authorized_claims = {
            "permissions": [REQUIRED_PERMISSION],
        }
        token_claims = {
            token: {"sub": subject, **authorized_claims}
            for token, subject in {
                "admin-token": "auth0|bootstrap-admin",
                "member-token": "auth0|member",
                "unknown-token": "auth0|unknown",
                "machine-token": "test-client-id@clients",
            }.items()
        }
        token_claims["missing-permission-token"] = {
            "sub": "auth0|member",
        }
        def decode_mock(token):
            if token == "invalid-token":
                raise HTTPException(401, "Token OAuth inválido.")
            return token_claims[token]

        self.decode = patch(
            "auth_access.decode_access_token",
            side_effect=decode_mock,
        )
        self.lookup = patch(
            "auth_access.get_authorized_user",
            side_effect=lambda subject: self.users.get(subject),
        )
        self.update = patch(
            "auth_access.set_user_status",
            side_effect=lambda subject, status, actor: self.users.update(
                {subject: {"subject": subject, "status": status, "authorized_by": actor}}
            ),
        )
        self.decode.start()
        self.lookup.start()
        self.update.start()
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.update.stop()
        self.lookup.stop()
        self.decode.stop()
        self.environment.stop()
        app.dependency_overrides.clear()

    @staticmethod
    def bearer(token):
        return {"Authorization": f"Bearer {token}"}

    def test_jurisprudence_requires_authentication_and_authorized_user(self):
        self.assertEqual(self.client.get("/health").json(), {"ok": True})
        denied = self.client.get("/v1/scj/search-plan", params={"q": "contrato"})
        self.assertEqual(denied.status_code, 401)

        unknown = self.client.get(
            "/v1/scj/search-plan",
            params={"q": "contrato"},
            headers=self.bearer("unknown-token"),
        )
        self.assertEqual(unknown.status_code, 403)

        invalid = self.client.get(
            "/v1/scj/search-plan",
            params={"q": "contrato"},
            headers=self.bearer("invalid-token"),
        )
        self.assertEqual(invalid.status_code, 401)

        denied_claims = self.client.get(
            "/v1/scj/search-plan",
            params={"q": "contrato"},
            headers=self.bearer("missing-permission-token"),
        )
        self.assertEqual(denied_claims.status_code, 403)

        self.users["auth0|member"] = {
            "subject": "auth0|member",
            "status": "ACTIVE",
        }
        allowed = self.client.get(
            "/v1/scj/search-plan",
            params={"q": "contrato"},
            headers=self.bearer("member-token"),
        )
        self.assertEqual(allowed.status_code, 200)

        self.users["test-client-id@clients"] = {
            "subject": "test-client-id@clients",
            "status": "ACTIVE",
        }
        machine_access = self.client.get(
            "/v1/scj/search-plan",
            params={"q": "prueba m2m"},
            headers=self.bearer("machine-token"),
        )
        self.assertEqual(machine_access.status_code, 200)

    def test_admin_permissions_and_individual_revocation(self):
        body = {"subject": "auth0|member", "status": "ACTIVE"}
        member_admin_attempt = self.client.put(
            "/v1/admin/users/status",
            json=body,
            headers=self.bearer("member-token"),
        )
        self.assertEqual(member_admin_attempt.status_code, 403)

        authorized = self.client.put(
            "/v1/admin/users/status",
            json=body,
            headers=self.bearer("admin-token"),
        )
        self.assertEqual(authorized.status_code, 200)
        member_headers = self.bearer("member-token")
        suspended = self.client.put(
            "/v1/admin/users/status",
            json={"subject": "auth0|member", "status": "SUSPENDED"},
            headers=self.bearer("admin-token"),
        )
        self.assertEqual(suspended.status_code, 200)
        suspended_access = self.client.get(
            "/v1/scj/search-plan",
            params={"q": "contrato"},
            headers=member_headers,
        )
        self.assertEqual(suspended_access.status_code, 403)

        reauthorized = self.client.put(
            "/v1/admin/users/status",
            json=body,
            headers=self.bearer("admin-token"),
        )
        self.assertEqual(reauthorized.status_code, 200)
        access = self.client.get(
            "/v1/scj/search-plan",
            params={"q": "contrato"},
            headers=member_headers,
        )
        self.assertEqual(access.status_code, 200)

        revoked = self.client.put(
            "/v1/admin/users/status",
            json={"subject": "auth0|member", "status": "REVOKED"},
            headers=self.bearer("admin-token"),
        )
        self.assertEqual(revoked.status_code, 200)
        after_revocation = self.client.get(
            "/v1/scj/search-plan",
            params={"q": "contrato"},
            headers=member_headers,
        )
        self.assertEqual(after_revocation.status_code, 403)

    def test_oauth_token_signature_audience_issuer_and_algorithm_are_checked(self):
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwks = type(
            "FakeJwks",
            (),
            {"get_signing_key_from_jwt": lambda _self, _token: type(
                "SigningKey", (), {"key": private_key.public_key()}
            )()},
        )()
        with patch("auth_access._jwks_client", return_value=jwks) as jwks_factory:
            now = datetime.now(timezone.utc)
            valid = jwt.encode(
                {
                    "sub": "test-client-id@clients",
                    "iss": "https://identity.example/",
                    "aud": AUTH0_AUDIENCE,
                    "iat": int(now.timestamp()),
                    "exp": int((now + timedelta(minutes=5)).timestamp()),
                },
                private_key,
                algorithm="RS256",
            )
            self.assertEqual(decode_access_token(valid)["sub"], "test-client-id@clients")
            jwks_factory.assert_called_with(
                "https://identity.example/.well-known/jwks.json"
            )

            wrong_audience = jwt.encode(
                {
                    "sub": "auth0|member",
                    "iss": "https://identity.example/",
                    "aud": "https://other.example/",
                    "iat": int(now.timestamp()),
                    "exp": int((now + timedelta(minutes=5)).timestamp()),
                },
                private_key,
                algorithm="RS256",
            )
            with self.assertRaises(HTTPException) as error:
                decode_access_token(wrong_audience)
            self.assertEqual(error.exception.status_code, 401)

            wrong_algorithm = jwt.encode(
                {
                    "sub": "auth0|member",
                    "iss": "https://identity.example/",
                    "aud": AUTH0_AUDIENCE,
                    "iat": int(now.timestamp()),
                    "exp": int((now + timedelta(minutes=5)).timestamp()),
                },
                "not-a-secret",
                algorithm="HS256",
            )
            with self.assertRaises(HTTPException) as error:
                decode_access_token(wrong_algorithm)
            self.assertEqual(error.exception.status_code, 401)

            wrong_issuer = jwt.encode(
                {
                    "sub": "auth0|member",
                    "iss": "https://untrusted.example/",
                    "aud": AUTH0_AUDIENCE,
                    "iat": int(now.timestamp()),
                    "exp": int((now + timedelta(minutes=5)).timestamp()),
                },
                private_key,
                algorithm="RS256",
            )
            with self.assertRaises(HTTPException) as error:
                decode_access_token(wrong_issuer)
            self.assertEqual(error.exception.status_code, 401)

            expired = jwt.encode(
                {
                    "sub": "auth0|member",
                    "iss": "https://identity.example/",
                    "aud": AUTH0_AUDIENCE,
                    "iat": int((now - timedelta(minutes=10)).timestamp()),
                    "exp": int((now - timedelta(minutes=5)).timestamp()),
                },
                private_key,
                algorithm="RS256",
            )
            with self.assertRaises(HTTPException) as error:
                decode_access_token(expired)
            self.assertEqual(error.exception.status_code, 401)

            other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            invalid_signature = jwt.encode(
                {
                    "sub": "auth0|member",
                    "iss": "https://identity.example/",
                    "aud": AUTH0_AUDIENCE,
                    "iat": int(now.timestamp()),
                    "exp": int((now + timedelta(minutes=5)).timestamp()),
                },
                other_key,
                algorithm="RS256",
            )
            with self.assertRaises(HTTPException) as error:
                decode_access_token(invalid_signature)
            self.assertEqual(error.exception.status_code, 401)

    def test_action_schema_declares_oauth_for_jurisprudence_routes(self):
        response = self.client.get("/openapi.json")
        self.assertEqual(response.status_code, 200)
        schema = response.json()
        operation = schema["paths"]["/v1/scj/search-plan"]["get"]
        self.assertTrue(operation["security"])
        required_scopes = next(iter(operation["security"][0].values()))
        self.assertIn(REQUIRED_PERMISSION, required_scopes)
        self.assertTrue(schema["components"]["securitySchemes"])
        self.assertEqual(schema["paths"]["/health"]["get"]["security"], [])
        self.assertNotIn("/v1/admin/users", schema["paths"])
        self.assertNotIn("post", schema["paths"]["/v1/registry/decisions"])


class DatabaseConnectionTests(unittest.TestCase):
    def test_connection_uses_database_url_and_requires_tls(self):
        database_url = "postgresql://test-user:test-password@db.example/test-db"
        with patch.dict(os.environ, {"DATABASE_URL": database_url}), patch(
            "psycopg.connect"
        ) as connect:
            from auth_access import connect_db

            connect_db()

        connect.assert_called_once_with(
            database_url, connect_timeout=8, sslmode="require"
        )

    def test_database_probe_runs_read_only_query(self):
        connection = MagicMock()
        connection.__enter__.return_value = connection
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.fetchone.return_value = (1,)
        connection.cursor.return_value = cursor

        with patch("auth_access.connect_db", return_value=connection):
            self.assertEqual(
                check_database_connection(), {"status": "DATABASE_CONNECTED"}
            )

        cursor.execute.assert_called_once_with("SELECT 1")

    def test_database_probe_sanitizes_connection_errors(self):
        secret_dsn = "postgresql://test-user:test-password@db.example/test-db"
        with patch(
            "auth_access.connect_db",
            side_effect=RuntimeError(secret_dsn),
        ):
            with self.assertRaises(HTTPException) as error:
                check_database_connection()

        self.assertEqual(error.exception.status_code, 503)
        self.assertNotIn(secret_dsn, error.exception.detail)

    def test_database_health_endpoint_is_admin_only(self):
        client = TestClient(app)
        try:
            denied = client.get("/v1/admin/database/health")
            self.assertEqual(denied.status_code, 401)

            app.dependency_overrides[require_active_user] = lambda: {
                "sub": "auth0|bootstrap-admin",
                "role": "admin",
            }
            with patch(
                "auth_access.check_database_connection",
                return_value={"status": "DATABASE_CONNECTED"},
            ):
                allowed = client.get("/v1/admin/database/health")

            self.assertEqual(allowed.status_code, 200)
            self.assertEqual(allowed.json(), {"status": "DATABASE_CONNECTED"})
        finally:
            app.dependency_overrides.pop(require_active_user, None)
            client.close()
