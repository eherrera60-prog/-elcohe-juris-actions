import asyncio
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from registry import RegisterRequest, register_decision


class RegistryPostgresTests(unittest.TestCase):
    def test_register_decision_writes_verified_record_to_postgresql(self):
        now = datetime.now(timezone.utc)
        connection = MagicMock()
        connection.__enter__.return_value = connection
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.fetchone.return_value = (7, now, now)
        connection.cursor.return_value = cursor
        analysis = {
            "official_source_verified": True,
            "status": "VERIFIED_SOURCE",
            "metadata": {"decision_number": "TC-001-2024"},
            "jurisprudential_card": {"tribunal": "TC"},
            "sha256": "a" * 64,
            "source_url": "https://example.test/decision.pdf",
            "relevant_passages": [],
            "extraction_diagnostics": {},
        }
        request = RegisterRequest(
            url="https://example.test/decision.pdf",
            expected_court="TC",
            expected_number="TC-001-2024",
        )

        with (
            patch("registry.connect", return_value=connection) as connect,
            patch("registry.init_schema") as init_schema,
            patch(
                "app.analyze_official_decision",
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
        self.assertEqual(result["status"], "STORED_VERIFIED_SOURCE")
        self.assertEqual(result["id"], 7)
        connection.__exit__.assert_called_once_with(None, None, None)


if __name__ == "__main__":
    unittest.main()
