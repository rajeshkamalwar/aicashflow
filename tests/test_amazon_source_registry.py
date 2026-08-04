from pathlib import Path
import tempfile
import unittest

from cryptography.fernet import Fernet

from ai_cashflow.integrations import AmazonSourceRegistry


class AmazonSourceRegistryTests(unittest.TestCase):
    def make_registry(self, root: Path) -> AmazonSourceRegistry:
        return AmazonSourceRegistry(root / "operations.sqlite3", Fernet.generate_key().decode())

    def test_stores_multiple_sources_without_returning_secrets(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = self.make_registry(Path(temp_dir))
            canada = registry.upsert({
                "name": "Books Amazon.Ca/Mx",
                "endpoint": "https://sellingpartnerapi-na.amazon.com",
                "client_id": "client-ca",
                "client_secret": "secret-ca",
                "refresh_token": "token-ca",
            })
            europe = registry.upsert({
                "name": "Books Amazon.UK & EU region",
                "endpoint": "https://sellingpartnerapi-eu.amazon.com",
                "client_id": "client-eu",
                "client_secret": "secret-eu",
                "refresh_token": "token-eu",
            })

            sources = registry.list_sources()

            self.assertEqual(len(sources), 2)
            self.assertNotEqual(canada["id"], europe["id"])
            self.assertNotIn("token-ca", str(sources))
            self.assertEqual(registry.credentials(canada["id"]).refresh_token, "token-ca")
            self.assertEqual(registry.credentials(europe["id"]).refresh_token, "token-eu")

    def test_blank_secret_fields_preserve_source_credentials(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = self.make_registry(Path(temp_dir))
            source = registry.upsert({
                "name": "Renewed CA/US/Mx",
                "endpoint": "https://sellingpartnerapi-na.amazon.com",
                "client_id": "client",
                "client_secret": "secret",
                "refresh_token": "token",
            })

            registry.upsert({"name": "Renewed CA/US/Mx", "refresh_token": ""}, source["id"])

            self.assertEqual(registry.credentials(source["id"]).refresh_token, "token")

    def test_connection_test_records_only_safe_payment_marketplace_identity(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = self.make_registry(Path(temp_dir))
            source = registry.upsert({
                "name": "Books Amazon.Ca/Mx",
                "client_id": "client",
                "client_secret": "secret",
                "refresh_token": "token",
            })

            result = registry.record_connection_test(source["id"], [{
                "marketplaceDetails": {
                    "marketplaceId": "A2EUQ1WTGCTBG2",
                    "marketplaceName": "Amazon.ca",
                    "unsafe": "drop",
                },
                "sellingPartnerMetadata": {
                    "sellingPartnerId": "A3SELLERACCOUNT",
                    "marketplaceId": "A2EUQ1WTGCTBG2",
                },
                "totalAmount": {"currencyCode": "CAD", "currencyAmount": 10},
            }])

            self.assertEqual(result["status"], "Connected")
            self.assertEqual(result["seller_id"], "A3SELLERACCOUNT")
            self.assertEqual(result["marketplaces"], [{
                "id": "A2EUQ1WTGCTBG2", "country_code": "", "name": "Amazon.ca",
                "currency": "CAD",
                "is_participating": True, "has_suspended_listings": False,
            }])

    def test_sync_keeps_reports_namespaced_by_source(self):
        class FakeClient:
            def list_settlement_reports(self):
                return [{"reportId": "R1", "reportDocumentId": "D1"}]

            def download_report_document(self, _document_id):
                return "settlement-id\tsettlement-start-date\nS1\t2026-07-01\n"

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            registry = self.make_registry(root)
            first = registry.upsert({"name": "Canada One", "client_id": "a", "client_secret": "b", "refresh_token": "c"})
            second = registry.upsert({"name": "Canada Two", "client_id": "a", "client_secret": "b", "refresh_token": "d"})

            one = registry.sync_source(first["id"], FakeClient(), root / "samples")
            two = registry.sync_source(second["id"], FakeClient(), root / "samples")

            self.assertEqual(one["reports_ingested"], 1)
            self.assertEqual(two["reports_ingested"], 1)
            self.assertTrue((root / "samples" / "marketplaces" / "api" / first["id"] / "amazon_sp_api_R1.txt").exists())
            self.assertTrue((root / "samples" / "marketplaces" / "api" / second["id"] / "amazon_sp_api_R1.txt").exists())

    def test_connection_test_rejects_a_different_seller_for_an_existing_source(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = self.make_registry(Path(temp_dir))
            source = registry.upsert({
                "name": "Books Amazon.Ca/Mx",
                "client_id": "client",
                "client_secret": "secret",
                "refresh_token": "token",
            })
            registry.record_connection_test(source["id"], [{
                "sellingPartnerMetadata": {
                    "sellingPartnerId": "A3ORIGINALSELLER",
                    "marketplaceId": "A2EUQ1WTGCTBG2",
                },
            }])

            with self.assertRaisesRegex(ValueError, "different Amazon seller account"):
                registry.record_connection_test(source["id"], [{
                    "sellingPartnerMetadata": {
                        "sellingPartnerId": "A3DIFFERENTSELLER",
                        "marketplaceId": "A2EUQ1WTGCTBG2",
                    },
                }])

    def test_only_enabled_complete_sources_are_due(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = self.make_registry(Path(temp_dir))
            due = registry.upsert({
                "name": "Enabled", "enabled": True,
                "sync_frequency_minutes": 5,
                "client_id": "a", "client_secret": "b", "refresh_token": "c",
            })
            registry.upsert({
                "name": "Disabled", "enabled": False,
                "client_id": "a", "client_secret": "b", "refresh_token": "d",
            })
            registry.upsert({"name": "Incomplete", "enabled": True})

            self.assertEqual(registry.due_source_ids(), [due["id"]])
            self.assertEqual(due["sync_frequency_minutes"], 5)


if __name__ == "__main__":
    unittest.main()
