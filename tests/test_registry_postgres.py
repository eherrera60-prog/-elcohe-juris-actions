import asyncio
import hashlib
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException
from pydantic import ValidationError

from app import AnalyzeDecisionRequest, OfficialPdfRequest, VerifyRequest
from juris_163 import IntegratedResearchRequest
from juris_164 import FoundationExtractionRequest
from registry import (
    RegisterRequest,
    find_verified_decisions,
    get_verified_document,
    register_decision,
)


class RegistryPostgresTests(unittest.TestCase):
    def test_decision_number_models_accept_historical_and_modern_identifiers(self):
        common = {
            "url": "https://example.test/decision.pdf",
            "expected_court": "SCJ",
        }
        historical = RegisterRequest(**common, expected_number="36")
        historical_label = RegisterRequest(**common, expected_number="NÚM. 36")
        modern = RegisterRequest(
            **common, expected_number="SCJ-TS-24-00123"
        )

        self.assertEqual(historical.expected_number, "36")
        self.assertEqual(historical_label.expected_number, "NÚM. 36")
        self.assertEqual(modern.expected_number, "SCJ-TS-24-00123")
        self.assertEqual(
            OfficialPdfRequest(
                **common, expected_number="36"
            ).expected_number,
            "36",
        )
        self.assertEqual(
            VerifyRequest(
                url=common["url"], expected_number="SCJ-TS-24-00123"
            ).expected_number,
            "SCJ-TS-24-00123",
        )
        self.assertEqual(
            AnalyzeDecisionRequest(
                url=common["url"], expected_number="NÚM. 36"
            ).expected_number,
            "NÚM. 36",
        )
        self.assertEqual(
            FoundationExtractionRequest(
                **common, expected_number="36"
            ).expected_number,
            "36",
        )
        self.assertEqual(
            IntegratedResearchRequest(
                legal_issue="falta de base legal",
                court="SCJ",
                expected_number="36",
            ).expected_number,
            "36",
        )

        for invalid in ("", "   ", "sentencia desconocida", "TC/12/2024"):
            with self.subTest(invalid=invalid), self.assertRaises(
                ValidationError
            ):
                RegisterRequest(**common, expected_number=invalid)

    def test_register_preserves_canonical_historical_identity_and_pdf_hash(self):
        now = datetime.now(timezone.utc)
        connection = MagicMock()
        connection.__enter__.return_value = connection
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.fetchone.return_value = (36, now, now)
        connection.cursor.return_value = cursor
        document_bytes = b"%PDF-1.7 historical decision fixture"
        digest = hashlib.sha256(document_bytes).hexdigest()
        analysis = {
            "official_source_verified": True,
            "expected_court_match": True,
            "expected_number_match": True,
            "status": "VERIFIED_SOURCE",
            "metadata": {"decision_number": "NÚM. 36"},
            "jurisprudential_card": {"tribunal": "SCJ"},
            "sha256": digest,
            "page_count": 1,
            "_verified_document_bytes": document_bytes,
            "source_url": "https://example.test/decision.pdf",
            "relevant_passages": [],
            "extraction_diagnostics": {},
        }
        request = RegisterRequest(
            url="https://example.test/decision.pdf",
            expected_court="SCJ",
            expected_number="36",
        )

        with (
            patch("registry.connect", return_value=connection),
            patch("registry.init_schema"),
            patch(
                "app._analyze_official_decision",
                new=AsyncMock(return_value=analysis),
            ),
        ):
            result = asyncio.run(register_decision(request))

        self.assertEqual(result["decision_number"], "NÚM. 36")
        self.assertEqual(result["sha256"], digest)
        self.assertEqual(hashlib.sha256(document_bytes).hexdigest(), digest)
        inserted_decision = next(
            call
            for call in cursor.execute.call_args_list
            if "INSERT INTO elcohe_decisions" in call.args[0]
        )
        self.assertEqual(inserted_decision.args[1][1], "NÚM. 36")
        inserted_document = next(
            call
            for call in cursor.execute.call_args_list
            if "INSERT INTO elcohe_decision_documents" in call.args[0]
        )
        self.assertIn(document_bytes, inserted_document.args[1])

    def test_register_decision_writes_verified_record_to_postgresql(self):
        now = datetime.now(timezone.utc)
        connection = MagicMock()
        connection.__enter__.return_value = connection
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.fetchone.return_value = (7, now, now)
        connection.cursor.return_value = cursor
        document_bytes = b"%PDF-1.7 persisted document fixture"
        digest = hashlib.sha256(document_bytes).hexdigest()
        analysis = {
            "official_source_verified": True,
            "status": "VERIFIED_SOURCE",
            "expected_court_match": True,
            "expected_number_match": True,
            "metadata": {"decision_number": "TC/0001/24"},
            "jurisprudential_card": {"tribunal": "TC"},
            "sha256": digest,
            "page_count": 3,
            "_verified_document_bytes": document_bytes,
            "source_url": "https://example.test/decision.pdf",
            "relevant_passages": [],
            "extraction_diagnostics": {},
        }
        request = RegisterRequest(
            url="https://example.test/decision.pdf",
            expected_court="TC",
            expected_number="TC/0001/24",
        )

        with (
            patch("registry.connect", return_value=connection) as connect,
            patch("registry.init_schema") as init_schema,
            patch(
                "app._analyze_official_decision",
                new=AsyncMock(return_value=analysis),
            ),
        ):
            result = asyncio.run(register_decision(request))

        connect.assert_called_once_with()
        init_schema.assert_called_once_with(connection)
        insert_sql = next(
            call.args[0]
            for call in cursor.execute.call_args_list
            if "INSERT INTO elcohe_decisions" in call.args[0]
        )
        self.assertIn("ON CONFLICT (court,decision_number,sha256)", insert_sql)
        blob_insert = next(
            call for call in cursor.execute.call_args_list
            if "INSERT INTO elcohe_decision_documents" in call.args[0]
        )
        self.assertIn(document_bytes, blob_insert.args[1])
        self.assertEqual(result["status"], "STORED_VERIFIED_SOURCE")
        self.assertEqual(result["id"], 7)
        connection.__exit__.assert_called_once_with(None, None, None)

    def test_find_verified_decisions_requires_a_persisted_pdf_and_matching_identity(self):
        now = datetime.now(timezone.utc)
        fields = [
            "id", "court", "decision_number", "source_url", "sha256", "chamber",
            "decision_date", "case_number", "legal_issue", "verification_status",
            "review_required", "ratio_decidendi_verified", "metadata",
            "extraction_diagnostics", "created_at",
        ]
        row = (
            7, "SCJ", "SCJ-PS-20-00036",
            "https://transparencia.poderjudicial.gob.do/decision.pdf",
            "a" * 64, "Primera Sala", "2020-01-29", None, "seguro",
            "VERIFIED_SOURCE", True, False, {"document_type": "PDF"},
            {"document_type": "PDF"}, now,
        )
        connection = MagicMock()
        connection.__enter__.return_value = connection
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.description = [SimpleNamespace(name=name) for name in fields]
        cursor.fetchall.return_value = [row]
        connection.cursor.return_value = cursor

        with (
            patch("registry.connect", return_value=connection),
            patch("registry.init_schema"),
        ):
            results = find_verified_decisions(
                "SCJ-PS-20-00036", court="SCJ"
            )

        sql = cursor.execute.call_args.args[0]
        assert "elcohe_decision_documents" in sql
        assert "EXISTS" in sql
        assert "document_type" in sql
        assert results[0]["document_bytes_persisted"] is True
        assert results[0]["identity_status"] == "VERIFIED_IDENTITY"

    def test_retrieved_postgresql_pdf_is_hash_checked_before_return(self):
        data = b"%PDF-1.7 persisted bytes"
        digest = hashlib.sha256(data).hexdigest()
        fields = [
            "id", "court", "decision_number", "source_url", "sha256",
            "verification_status", "review_required", "ratio_decidendi_verified",
            "metadata", "extraction_diagnostics", "content_type", "page_count",
            "document_bytes",
        ]
        row = (
            7, "SCJ", "SCJ-PS-20-00036",
            "https://transparencia.poderjudicial.gob.do/decision.pdf",
            digest, "VERIFIED_SOURCE", True, False, {}, {},
            "application/pdf", 6, data,
        )
        connection = MagicMock()
        connection.__enter__.return_value = connection
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.description = [SimpleNamespace(name=name) for name in fields]
        cursor.fetchone.return_value = row
        connection.cursor.return_value = cursor

        with (
            patch("registry.connect", return_value=connection),
            patch("registry.init_schema"),
            patch(
                "app.extract_document_text",
                return_value={
                    "extraction_ok": True,
                    "page_count": 6,
                    "full_text": (
                        "SENTENCIA DEL 29 DE ENERO DE 2020 SCJ-PS-20-00036\n"
                        "SUPREMA CORTE DE JUSTICIA\nPRIMERA SALA"
                    ),
                },
            ),
            patch("app.detect_court", return_value="SCJ"),
            patch("app.allowed_host", return_value=True),
        ):
            result = get_verified_document(7)

        self.assertEqual(result["document_bytes"], data)
        self.assertEqual(result["document_type"], "PDF")
        self.assertTrue(result["document_recovered"])
        self.assertFalse(result["ratio_decidendi_verified"])

        cursor.fetchone.return_value = (*row[:-1], b"%PDF-tampered")
        with (
            patch("registry.connect", return_value=connection),
            patch("registry.init_schema"),
            patch("app.allowed_host", return_value=True),
            self.assertRaises(HTTPException) as raised,
        ):
            get_verified_document(7)
        self.assertEqual(raised.exception.status_code, 422)

    def test_historical_scj_number_36_recovers_with_identity_and_hash_verified(self):
        data = b"%PDF-1.7 historical decision bytes"
        digest = hashlib.sha256(data).hexdigest()
        fields = [
            "id", "court", "decision_number", "source_url", "sha256",
            "verification_status", "review_required", "ratio_decidendi_verified",
            "metadata", "extraction_diagnostics", "content_type", "page_count",
            "document_bytes",
        ]
        row = (
            36, "SCJ", "NÚM. 36",
            "https://transparencia.poderjudicial.gob.do/decision-36.pdf",
            digest, "VERIFIED_SOURCE", True, False, {}, {},
            "application/pdf", 1, data,
        )
        connection = MagicMock()
        connection.__enter__.return_value = connection
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.description = [SimpleNamespace(name=name) for name in fields]
        cursor.fetchone.return_value = row
        connection.cursor.return_value = cursor

        with (
            patch("registry.connect", return_value=connection),
            patch("registry.init_schema"),
            patch(
                "app.extract_document_text",
                return_value={
                    "extraction_ok": True,
                    "page_count": 1,
                    "full_text": (
                        "SENTENCIA DEL 29 DE ENERO DE 2020, NÚM. 36\n"
                        "SUPREMA CORTE DE JUSTICIA\nPRIMERA SALA"
                    ),
                },
            ),
        ):
            recovered = get_verified_document(36)

        self.assertEqual(recovered["decision_number"], "NÚM. 36")
        self.assertEqual(recovered["document_bytes"], data)
        self.assertEqual(recovered["sha256"], digest)
        self.assertEqual(
            hashlib.sha256(recovered["document_bytes"]).hexdigest(), digest
        )
        self.assertTrue(recovered["document_recovered"])
        self.assertEqual(recovered["identity_status"], "VERIFIED_IDENTITY")


if __name__ == "__main__":
    unittest.main()
