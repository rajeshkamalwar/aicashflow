from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

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

    def test_transaction_observations_are_append_only_and_latest_state_wins(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = self.make_registry(Path(temp_dir))
            source = registry.upsert({"name": "Canada", "client_id": "a", "client_secret": "b", "refresh_token": "c"})
            first = datetime(2026, 8, 4, 10, 0, tzinfo=timezone.utc)
            second = datetime(2026, 8, 4, 10, 5, tzinfo=timezone.utc)
            registry.record_transaction_snapshot(
                source["id"], "A2EUQ1WTGCTBG2", "Amazon.ca", "CAD", "DEFERRED",
                [{"transactionId": "T1", "transactionStatus": "DEFERRED", "transactionType": "Shipment", "totalAmount": {"currencyCode": "CAD", "currencyAmount": "10.10"}}],
                first,
            )
            registry.record_transaction_snapshot(
                source["id"], "A2EUQ1WTGCTBG2", "Amazon.ca", "CAD", "DEFERRED_RELEASED",
                [{"transactionId": "T1", "transactionStatus": "DEFERRED_RELEASED", "transactionType": "Shipment", "totalAmount": {"currencyCode": "CAD", "currencyAmount": "10.10"}}],
                second,
            )

            result = registry.transaction_visibility(source["id"], "CAD")

            self.assertEqual(result["observation_count"], 2)
            self.assertEqual(result["deferred_count"], 0)
            self.assertEqual(result["deferred_released_count"], 1)
            self.assertEqual(result["deferred_released_amount"], "10.10")
            self.assertEqual(result["classification"], "AMAZON_TRANSACTION_DERIVED")

    def test_progressed_transaction_does_not_regress_to_deferred(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = self.make_registry(Path(temp_dir))
            source = registry.upsert({"name": "Canada", "client_id": "a", "client_secret": "b", "refresh_token": "c"})
            first = datetime(2026, 8, 4, 10, 0, tzinfo=timezone.utc)
            second = datetime(2026, 8, 4, 10, 5, tzinfo=timezone.utc)
            common = {"transactionId": "T1", "transactionType": "Shipment", "totalAmount": {"currencyCode": "CAD", "currencyAmount": "4.25"}}
            registry.record_transaction_snapshot(source["id"], "A2EUQ1WTGCTBG2", "Amazon.ca", "CAD", "DEFERRED_RELEASED", [{**common, "transactionStatus": "DEFERRED_RELEASED"}], first)
            registry.record_transaction_snapshot(source["id"], "A2EUQ1WTGCTBG2", "Amazon.ca", "CAD", "DEFERRED", [{**common, "transactionStatus": "DEFERRED"}], second)

            result = registry.transaction_visibility(source["id"], "CAD")

            self.assertEqual(result["deferred_count"], 0)
            self.assertEqual(result["deferred_released_count"], 0)
            self.assertEqual(result["conflict_count"], 1)

    def test_successful_empty_status_snapshot_removes_stale_current_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = self.make_registry(Path(temp_dir))
            source = registry.upsert({"name": "Canada", "client_id": "a", "client_secret": "b", "refresh_token": "c"})
            first = datetime(2026, 8, 4, 10, 0, tzinfo=timezone.utc)
            second = datetime(2026, 8, 4, 10, 5, tzinfo=timezone.utc)
            registry.record_transaction_snapshot(source["id"], "CA", "Amazon.ca", "CAD", "DEFERRED", [{"transactionId": "T1", "transactionStatus": "DEFERRED", "totalAmount": {"currencyCode": "CAD", "currencyAmount": "4.25"}}], first)
            registry.record_transaction_snapshot(source["id"], "CA", "Amazon.ca", "CAD", "DEFERRED", [], second)

            result = registry.transaction_visibility(source["id"], "CAD")

            self.assertEqual(result["deferred_count"], 0)
            self.assertEqual(result["observation_count"], 1)

    def test_internal_reconciliation_report_requires_three_snapshots(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = self.make_registry(Path(temp_dir))
            source = registry.upsert({"name": "Canada", "client_id": "a", "client_secret": "b", "refresh_token": "c"})
            for minute, amount in ((0, "4.25"), (5, "5.25"), (10, "6.25")):
                registry.record_transaction_snapshot(
                    source["id"], "CA", "Amazon.ca", "CAD", "DEFERRED",
                    [{"transactionId": "T1", "transactionStatus": "DEFERRED", "totalAmount": {"currencyCode": "CAD", "currencyAmount": amount}}],
                    datetime(2026, 8, 4, 10, minute, tzinfo=timezone.utc),
                )

            report = registry.transaction_reconciliation_report(source["id"])

            self.assertEqual(report["status"], "ready")
            self.assertEqual(report["snapshot_count"], 3)
            self.assertEqual(report["snapshots"][-1]["totals"], {"CAD:DEFERRED": "6.25"})

    def test_transaction_visibility_keeps_currencies_separate_and_ignores_total_amount_for_composition(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = self.make_registry(Path(temp_dir))
            source = registry.upsert({"name": "North America", "client_id": "a", "client_secret": "b", "refresh_token": "c"})
            retrieved = datetime(2026, 8, 4, 10, 0, tzinfo=timezone.utc)
            registry.record_transaction_snapshot(source["id"], "CA", "Amazon.ca", "CAD", "DEFERRED", [{
                "transactionId": "CAD-1", "transactionStatus": "DEFERRED", "transactionType": "Shipment",
                "totalAmount": {"currencyCode": "CAD", "currencyAmount": "12.00"},
                "breakdowns": [{"breakdownType": "Principal", "breakdownAmount": {"currencyCode": "CAD", "currencyAmount": "15.00"}}, {"breakdownType": "AmazonFees", "breakdownAmount": {"currencyCode": "CAD", "currencyAmount": "-3.00"}}],
            }], retrieved)
            registry.record_transaction_snapshot(source["id"], "US", "Amazon.com", "USD", "DEFERRED", [{"transactionId": "USD-1", "transactionStatus": "DEFERRED", "transactionType": "Shipment", "totalAmount": {"currencyCode": "USD", "currencyAmount": "7.00"}}], retrieved)

            all_currencies = registry.transaction_visibility(source["id"])
            cad = registry.transaction_visibility(source["id"], "CAD")

            self.assertEqual(all_currencies["by_currency"]["CAD"]["deferred_amount"], cad["deferred_amount"])
            self.assertEqual(cad["composition"], {"Product sales": "15.00", "Amazon fees": "-3.00"})
            self.assertNotIn("Other", cad["composition"])

    def test_transaction_visibility_reports_exact_partial_coverage(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = self.make_registry(Path(temp_dir))
            source = registry.upsert({"name": "Canada", "client_id": "a", "client_secret": "b", "refresh_token": "c"})
            start = datetime(2026, 8, 4, 14, 42, tzinfo=timezone.utc)
            end = datetime(2026, 8, 4, 20, 48, tzinfo=timezone.utc)
            registry.record_transaction_snapshot(
                source["id"], "CA", "Amazon.ca", "CAD", "DEFERRED",
                [{"transactionId": "T1", "transactionStatus": "DEFERRED", "totalAmount": {"currencyCode": "CAD", "currencyAmount": "21.29"}}],
                end, coverage_start=start, coverage_end=end,
            )

            coverage = registry.transaction_visibility(source["id"], "CAD")["coverage"]

            self.assertEqual(coverage["completed_start"], start.isoformat())
            self.assertEqual(coverage["completed_end"], end.isoformat())
            self.assertEqual(coverage["last_successful_collection_at"], end.isoformat())
            self.assertEqual(coverage["classification"], "PARTIAL_COVERAGE")
            self.assertFalse(coverage["is_complete"])
            self.assertTrue(coverage["has_gaps"])
            self.assertIsNone(coverage["coverage_percentage"])
            self.assertEqual(coverage["completed_slice_count"], 0)

    def test_incremental_overlap_does_not_claim_historical_coverage(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = self.make_registry(Path(temp_dir))
            source = registry.upsert({"name": "Canada", "client_id": "a", "client_secret": "b", "refresh_token": "c"})
            start = datetime(2026, 8, 4, 14, 42, tzinfo=timezone.utc)
            end = datetime(2026, 8, 4, 20, 48, tzinfo=timezone.utc)
            registry.record_transaction_snapshot(
                source["id"], "CA", "Amazon.ca", "CAD", "DEFERRED", [], end,
                coverage_start=start, coverage_end=end,
            )
            registry.create_transaction_backfill(
                source_id=source["id"], marketplace_id="CA", marketplace_name="Amazon.ca",
                currency="CAD", transaction_status="DEFERRED",
                overall_start=start, overall_end=start.replace(hour=15, minute=42), slice_hours=1,
            )

            coverage = registry.transaction_visibility(source["id"], "CAD")["coverage"]

            self.assertIsNone(coverage["requested_start"])
            self.assertEqual(coverage["classification"], "PARTIAL_COVERAGE")
            self.assertFalse(coverage["is_complete"])

    def test_transaction_failure_keeps_report_sync_available_and_marks_partial(self):
        class FakeClient:
            def list_settlement_reports(self):
                return []
            def list_transactions_by_status(self, _marketplace_id, status, _posted_after):
                if status == "RELEASED":
                    raise RuntimeError("temporary upstream error")
                return []

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            registry = self.make_registry(root)
            source = registry.upsert({"name": "Canada", "client_id": "a", "client_secret": "b", "refresh_token": "c"})
            registry.record_connection_test(source["id"], [{
                "marketplaceDetails": {"marketplaceId": "CA", "marketplaceName": "Amazon.ca"},
                "totalAmount": {"currencyCode": "CAD", "currencyAmount": 0},
            }])
            registry.record_transaction_snapshot(
                source["id"], "CA", "Amazon.ca", "CAD", "RELEASED",
                [{
                    "transactionId": "T1",
                    "transactionStatus": "RELEASED",
                    "totalAmount": {"currencyCode": "CAD", "currencyAmount": "4.25"},
                }],
                datetime(2026, 8, 4, 9, 55, tzinfo=timezone.utc),
            )

            with patch.dict("os.environ", {
                "AI_CASHFLOW_TRANSACTION_COLLECTION_ENABLED": "true",
                "AI_CASHFLOW_TRANSACTION_VISIBILITY_ENABLED": "false",
            }):
                result = registry.sync_source(source["id"], FakeClient(), root / "samples")

            self.assertEqual(result["status"], "partial")
            self.assertEqual(result["open_balance_sync_status"], "completed")
            self.assertEqual(result["transaction_collection_status"], "partial")
            self.assertEqual(result["transaction_errors"], 1)
            self.assertEqual(registry.get_source(source["id"])["status"], "Connected")
            visibility = registry.transaction_visibility(source["id"], "CAD")
            self.assertEqual(visibility["released_amount"], "4.25")
            self.assertTrue(visibility["partial_failure"])
            self.assertEqual(visibility["marketplaces"], ["Amazon.ca"])

    def test_collection_flag_collects_and_checkpoints_while_visibility_is_disabled(self):
        class FakeClient:
            statuses = []

            def list_settlement_reports(self):
                return []

            def list_transactions_by_status(self, _marketplace_id, status, _posted_after):
                self.statuses.append(status)
                if status != "DEFERRED":
                    return []
                return [{
                    "transactionId": "T1",
                    "transactionStatus": "DEFERRED",
                    "totalAmount": {"currencyCode": "CAD", "currencyAmount": "4.25"},
                }]

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            registry = self.make_registry(root)
            source = registry.upsert({"name": "Canada", "client_id": "a", "client_secret": "b", "refresh_token": "c"})
            registry.record_connection_test(source["id"], [{
                "marketplaceDetails": {"marketplaceId": "CA", "marketplaceName": "Amazon.ca"},
                "totalAmount": {"currencyCode": "CAD", "currencyAmount": 0},
            }])
            client = FakeClient()

            with patch.dict("os.environ", {
                "AI_CASHFLOW_TRANSACTION_COLLECTION_ENABLED": "true",
                "AI_CASHFLOW_TRANSACTION_VISIBILITY_ENABLED": "false",
            }):
                result = registry.sync_source(source["id"], client, root / "samples")

            visibility = registry.transaction_visibility(source["id"], "CAD")

        self.assertCountEqual(client.statuses, ["DEFERRED", "RELEASED", "DEFERRED_RELEASED"])
        self.assertEqual(result["transaction_collection_status"], "completed")
        self.assertEqual(result["transaction_observations"], 1)
        self.assertEqual(visibility["deferred_amount"], "4.25")
        self.assertIsNotNone(visibility["retrieved_at"])
        self.assertTrue(visibility["pagination_complete"])

    def test_visibility_flag_alone_does_not_call_amazon_transactions(self):
        class FakeClient:
            def list_settlement_reports(self):
                return []

            def list_transactions_by_status(self, *_args):
                raise AssertionError("visibility must not trigger Amazon collection")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            registry = self.make_registry(root)
            source = registry.upsert({"name": "Canada", "client_id": "a", "client_secret": "b", "refresh_token": "c"})
            registry.record_connection_test(source["id"], [{
                "marketplaceDetails": {"marketplaceId": "CA", "marketplaceName": "Amazon.ca"},
                "totalAmount": {"currencyCode": "CAD", "currencyAmount": 0},
            }])

            with patch.dict("os.environ", {
                "AI_CASHFLOW_TRANSACTION_COLLECTION_ENABLED": "false",
                "AI_CASHFLOW_TRANSACTION_VISIBILITY_ENABLED": "false",
            }):
                disabled = registry.sync_source(source["id"], FakeClient(), root / "samples")
            with patch.dict("os.environ", {
                "AI_CASHFLOW_TRANSACTION_COLLECTION_ENABLED": "false",
                "AI_CASHFLOW_TRANSACTION_VISIBILITY_ENABLED": "true",
            }):
                result = registry.sync_source(source["id"], FakeClient(), root / "samples")

        self.assertEqual(disabled["transaction_collection_status"], "disabled")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["transaction_collection_status"], "disabled")
        self.assertEqual(result["transaction_observations"], 0)


if __name__ == "__main__":
    unittest.main()
