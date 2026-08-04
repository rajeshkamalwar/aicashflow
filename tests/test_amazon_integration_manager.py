from pathlib import Path
from contextlib import closing
from datetime import datetime, timezone
import sqlite3
import tempfile
import unittest

from cryptography.fernet import Fernet

from ai_cashflow.integrations import AmazonIntegrationManager


class AmazonIntegrationManagerTests(unittest.TestCase):
    def make_manager(self, root: Path) -> AmazonIntegrationManager:
        return AmazonIntegrationManager(root / "operations.sqlite3", Fernet.generate_key().decode())

    def test_saves_encrypted_credentials_and_returns_only_presence_flags(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manager = self.make_manager(root)

            result = manager.save({
                "application_id": "amzn1.sp.solution.test",
                "endpoint": "https://sellingpartnerapi-na.amazon.com",
                "marketplace": "Canada",
                "sync_frequency_minutes": 60,
                "enabled": True,
                "client_id": "client-id-value",
                "client_secret": "client-secret-value",
                "refresh_token": "refresh-token-value",
            })

            self.assertTrue(result["credentials"]["client_id_configured"])
            self.assertTrue(result["credentials"]["client_secret_configured"])
            self.assertTrue(result["credentials"]["refresh_token_configured"])
            self.assertNotIn("client-id-value", str(result))
            with closing(sqlite3.connect(root / "operations.sqlite3")) as connection:
                stored = " ".join(str(value) for value in connection.execute(
                    "select client_id_encrypted, client_secret_encrypted, refresh_token_encrypted from amazon_integration"
                ).fetchone())
            self.assertNotIn("client-id-value", stored)
            self.assertNotIn("client-secret-value", stored)
            self.assertNotIn("refresh-token-value", stored)

    def test_blank_secret_fields_preserve_existing_credentials(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = self.make_manager(Path(temp_dir))
            manager.save({
                "endpoint": "https://sellingpartnerapi-na.amazon.com",
                "client_id": "client-id-value",
                "client_secret": "client-secret-value",
                "refresh_token": "refresh-token-value",
            })

            manager.save({
                "endpoint": "https://sellingpartnerapi-na.amazon.com",
                "marketplace": "Canada",
                "client_id": "",
                "client_secret": "",
                "refresh_token": "",
            })

            credentials = manager.credentials()
            self.assertEqual(credentials.client_id, "client-id-value")
            self.assertEqual(credentials.client_secret, "client-secret-value")
            self.assertEqual(credentials.refresh_token, "refresh-token-value")

    def test_rejects_non_amazon_endpoint(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = self.make_manager(Path(temp_dir))

            with self.assertRaisesRegex(ValueError, "official Amazon SP-API endpoint"):
                manager.save({"endpoint": "https://attacker.example/api"})

    def test_sync_is_idempotent_and_writes_managed_report_cache(self):
        class FakeClient:
            def list_settlement_reports(self):
                return [{"reportId": "R1", "reportDocumentId": "D1", "createdTime": "2026-07-14T00:00:00Z"}]

            def download_report_document(self, document_id):
                self.document_id = document_id
                return (
                    "settlement-id\tsettlement-start-date\tsettlement-end-date\tdeposit-date\t"
                    "total-amount\tcurrency\ttransaction-type\tamount-type\tamount-description\tamount\n"
                    "S1\t2026-07-01\t2026-07-07\t2026-07-09\t10.00\tCAD\t\t\t\t\n"
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manager = self.make_manager(root)
            first = manager.sync(FakeClient(), root / "samples")
            second = manager.sync(FakeClient(), root / "samples")

            self.assertEqual(first["reports_ingested"], 1)
            self.assertEqual(second["reports_ingested"], 0)
            self.assertTrue((root / "samples" / "marketplaces" / "api" / "amazon_sp_api_R1.txt").exists())
            self.assertEqual(len(manager.list_syncs()), 2)

    def test_scheduled_sync_requires_enabled_complete_credentials(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = self.make_manager(Path(temp_dir))
            self.assertFalse(manager.is_sync_due())
            manager.save({
                "endpoint": "https://sellingpartnerapi-na.amazon.com",
                "enabled": True,
                "client_id": "client",
                "client_secret": "secret",
                "refresh_token": "refresh",
            })
            self.assertTrue(manager.is_sync_due(datetime.now(timezone.utc)))

    def test_saving_configuration_clears_a_previous_connection_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = self.make_manager(Path(temp_dir))
            manager.record_connection_test(success=False, error="Refresh token is missing")
            manager.save({"endpoint": "https://sellingpartnerapi-na.amazon.com"})

            self.assertIsNone(manager.public()["last_error"])


if __name__ == "__main__":
    unittest.main()
