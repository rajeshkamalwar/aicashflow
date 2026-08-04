import unittest
from datetime import datetime, timezone
from pathlib import Path
import tempfile
from unittest.mock import patch

from cryptography.fernet import Fernet

from ai_cashflow.api.services import Phase0Service
from ai_cashflow.config import AppConfig
from ai_cashflow.integrations import AmazonSourceRegistry


PHASE0_FIXTURES = Path(__file__).resolve().parent / "fixtures/phase0_samples"
AMAZON_REPORT = Path(__file__).resolve().parent / "fixtures/amazon_transaction_report_sample.csv"


def make_service(root: Path) -> Phase0Service:
    return Phase0Service(
        AppConfig(
            samples_dir=PHASE0_FIXTURES,
            reports_dir=root / "reports",
            database_path=root / "ops.db",
        )
    )


class Phase0ServiceTests(unittest.TestCase):
    def test_financial_position_attaches_persisted_transaction_visibility_only_when_enabled(self):
        class FakePositionClient:
            def __init__(self, _config):
                pass
            def get_financial_position(self, _marketplace_id, _usd_rate, *, expected_currency=None):
                return {
                    "status": "ready", "currency": "USD", "source_currency": "CAD",
                    "as_of": "2026-08-04T10:00:00Z", "amounts": {"standard_balance": "10.00"},
                    "source_amounts": {"standard_balance": "10.00"}, "financial_values": [],
                }

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            key = Fernet.generate_key().decode()
            registry = AmazonSourceRegistry(root / "ops.db", key)
            source = registry.upsert({"name": "Amazon CA", "enabled": True, "client_id": "a", "client_secret": "b", "refresh_token": "c"})
            registry.record_connection_test(source["id"], [{
                "marketplaceDetails": {"marketplaceId": "CA", "marketplaceName": "Amazon.ca"},
                "totalAmount": {"currencyCode": "CAD", "currencyAmount": 0},
            }])
            registry.record_transaction_snapshot(
                source["id"], "CA", "Amazon.ca", "CAD", "DEFERRED",
                [{"transactionId": "T1", "transactionStatus": "DEFERRED", "totalAmount": {"currencyCode": "CAD", "currencyAmount": "4.25"}}],
                datetime(2026, 8, 4, 10, 0, tzinfo=timezone.utc),
            )
            visible_service = Phase0Service(AppConfig(
                database_path=root / "ops.db",
                transaction_collection_enabled=True,
                transaction_visibility_enabled=True,
            ))
            hidden_service = Phase0Service(AppConfig(
                database_path=root / "ops.db",
                transaction_collection_enabled=True,
                transaction_visibility_enabled=False,
            ))
            with (
                patch.dict("os.environ", {"AI_CASHFLOW_MASTER_KEY": key}),
                patch("ai_cashflow.api.services.AmazonSpApiClient", FakePositionClient),
            ):
                visible = visible_service.get_amazon_financial_position(source["id"], "CAD")
                hidden = hidden_service.get_amazon_financial_position(source["id"], "CAD")
                visible_again = visible_service.get_amazon_financial_position(source["id"], "CAD")

        self.assertTrue(visible["transaction_visibility_enabled"])
        self.assertEqual(visible["transaction_visibility"]["deferred_amount"], "4.25")
        self.assertFalse(hidden["transaction_visibility_enabled"])
        self.assertNotIn("transaction_visibility", hidden)
        self.assertEqual(visible_again["transaction_visibility"]["deferred_amount"], "4.25")
        self.assertIsNone(next(row for row in visible["financial_values"] if row["type"] == "UPCOMING_PAYOUT")["amount"] if any(row["type"] == "UPCOMING_PAYOUT" for row in visible["financial_values"]) else None)

    def test_cashflow_proof_coverage_includes_every_enabled_source_only(self):
        class FakeRegistry:
            def __init__(self, _database_path, _key):
                pass

            def list_sources(self):
                return [
                    {
                        "id": "ENABLED",
                        "name": "Enabled source",
                        "enabled": True,
                        "status": "Authorization pending",
                    },
                    {
                        "id": "DISABLED",
                        "name": "Disabled source",
                        "enabled": False,
                        "status": "Connected",
                    },
                ]

        with tempfile.TemporaryDirectory() as temp_dir:
            service = Phase0Service(AppConfig(database_path=Path(temp_dir) / "ops.db"))
            with (
                patch.dict("os.environ", {"AI_CASHFLOW_MASTER_KEY": "key"}),
                patch("ai_cashflow.api.services.AmazonSourceRegistry", FakeRegistry),
            ):
                result = service.run_cashflow_proof()

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["source_coverage"], "0/1")

    def test_bank_statement_mapping_validates_before_commit(self):
        mapped = (
            "Credit ID,Account,CCY,Value Date,Credit,Details\n"
            "R1,OPERATING,USD,2026-07-02,37.85,GROUP-1\n"
        ).encode()
        mapping = {
            "receipt_id": "Credit ID",
            "bank_account": "Account",
            "currency": "CCY",
            "receipt_date": "Value Date",
            "amount": "Credit",
            "reference": "Details",
        }
        invalid = mapped.replace(b",USD,", b",EUR,")
        with tempfile.TemporaryDirectory() as temp_dir:
            service = Phase0Service(AppConfig(database_path=Path(temp_dir) / "ops.db"))
            with self.assertRaisesRegex(ValueError, "not configured"):
                service.import_bank_statement(
                    file_name="invalid.csv",
                    content=invalid,
                    bank_source="Main bank",
                    column_mapping=mapping,
                )
            self.assertEqual(service.store.list_bank_receipts(), [])
            imported = service.import_bank_statement(
                file_name="valid.csv",
                content=mapped,
                bank_source="Main bank",
                column_mapping=mapping,
            )

        self.assertEqual(imported["row_count"], 1)

    def test_cashflow_proof_matches_settlement_finances_and_bank_receipt(self):
        class FakeRegistry:
            def __init__(self, _database_path, _key):
                pass

            def list_sources(self):
                return [{
                    "id": "SOURCE-1",
                    "name": "Amazon US",
                    "enabled": True,
                    "status": "Connected",
                    "marketplaces": [{
                        "id": "ATVPDKIKX0DER",
                        "name": "Amazon.com",
                        "country_code": "US",
                        "currency": "USD",
                        "is_participating": True,
                    }],
                }]

            def credentials(self, _source_id):
                return object()

            def list_reports(self, _source_id):
                return [{
                    "report_id": "REPORT-1",
                    "cache_path": str(AMAZON_REPORT),
                }]

        class FakeClient:
            def __init__(self, _credentials):
                pass

            def list_financial_event_groups(self, _started_after):
                return [{
                    "FinancialEventGroupId": "26483211921",
                    "ProcessingStatus": "Closed",
                    "FundTransferDate": "2026-07-01T00:00:00Z",
                    "OriginalTotal": {
                        "CurrencyCode": "USD",
                        "CurrencyAmount": "37.85",
                    },
                }]

            def list_payment_transactions(self, *_args, **_kwargs):
                return [{
                    "totalAmount": {
                        "currencyCode": "USD",
                        "currencyAmount": "37.85",
                    },
                }]

        statement = (
            "receipt_id,bank_account,currency,receipt_date,amount,reference\n"
            "R1,OPERATING,USD,2026-07-02,37.85,26483211921\n"
        ).encode()
        with tempfile.TemporaryDirectory() as temp_dir:
            service = Phase0Service(AppConfig(database_path=Path(temp_dir) / "ops.db"))
            service.import_bank_statement(
                file_name="statement.csv",
                content=statement,
                bank_source="Main bank",
            )
            with (
                patch.dict("os.environ", {"AI_CASHFLOW_MASTER_KEY": "key"}),
                patch("ai_cashflow.api.services.AmazonSourceRegistry", FakeRegistry),
                patch("ai_cashflow.api.services.AmazonSpApiClient", FakeClient),
            ):
                result = service.run_cashflow_proof(
                    now=datetime(2026, 7, 10, tzinfo=timezone.utc)
                )
                items = service.list_cashflow_proof_items(limit=20, offset=0)

        self.assertEqual(result["window_days"], 90)
        self.assertEqual(result["source_coverage"], "1/1")
        self.assertEqual(items["items"][0]["status"], "Verified")
        self.assertEqual(items["items"][0]["bank_receipt_id"], "R1")

    def test_amazon_financial_position_is_cached_until_the_source_sync_changes(self):
        calls = 0

        class FakePositionClient:
            def __init__(self, _config):
                pass

            def get_financial_position(
                self, _marketplace_id, _usd_rate, *, expected_currency=None
            ):
                nonlocal calls
                calls += 1
                return {
                    "status": "ready",
                    "currency": "USD",
                    "source_currency": "AUD",
                    "amounts": {"funds_available": "10.00"},
                    "source_amounts": {"funds_available": "15.00"},
                }

        class FakeSyncClient:
            def list_settlement_reports(self):
                return []

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            key = Fernet.generate_key().decode()
            registry = AmazonSourceRegistry(root / "ops.db", key)
            source = registry.upsert({
                "name": "Amazon AU",
                "enabled": True,
                "client_id": "client",
                "client_secret": "secret",
                "refresh_token": "token",
            })
            registry.record_connection_test(source["id"], [{
                "marketplaceDetails": {
                    "marketplaceId": "A39IBJ37TRP1C6",
                    "marketplaceName": "Amazon.com.au",
                },
                "totalAmount": {"currencyCode": "AUD", "currencyAmount": 10},
            }])
            service = Phase0Service(AppConfig(database_path=root / "ops.db"))
            with (
                patch.dict("os.environ", {"AI_CASHFLOW_MASTER_KEY": key}),
                patch("ai_cashflow.api.services.AmazonSpApiClient", FakePositionClient),
            ):
                first = service.get_amazon_financial_position(source["id"])
                second = service.get_amazon_financial_position(source["id"])
                registry.sync_source(source["id"], FakeSyncClient(), root / "samples")
                third = service.get_amazon_financial_position(source["id"])

        self.assertEqual(first, second)
        self.assertEqual(third["amounts"]["funds_available"], "10.00")
        self.assertEqual(calls, 2)

    def test_multi_market_source_selects_marketplace_by_currency_for_funds_available(self):
        marketplace_ids = []
        expected_currencies = []

        class FakePositionClient:
            def __init__(self, _config):
                pass

            def get_financial_position(
                self, marketplace_id, _usd_rate, *, expected_currency=None
            ):
                marketplace_ids.append(marketplace_id)
                expected_currencies.append(expected_currency)
                return {
                    "status": "ready",
                    "currency": "USD",
                    "source_currency": "CAD",
                    "as_of": "2026-08-04T07:30:00Z",
                    "amounts": {"standard_balance": "55.00", "funds_available": None},
                    "source_amounts": {"standard_balance": "77.00", "funds_available": None},
                    "financial_values": [],
                }

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            key = Fernet.generate_key().decode()
            registry = AmazonSourceRegistry(root / "ops.db", key)
            source = registry.upsert({
                "name": "Amazon CA/MX",
                "enabled": True,
                "client_id": "client",
                "client_secret": "secret",
                "refresh_token": "token",
            })
            registry.record_connection_test(source["id"], [
                {
                    "marketplaceDetails": {"marketplaceId": "A2EUQ1WTGCTBG2", "marketplaceName": "Amazon.ca", "countryCode": "CA"},
                    "totalAmount": {"currencyCode": "CAD", "currencyAmount": 10},
                },
                {
                    "marketplaceDetails": {"marketplaceId": "A1AM78C64UM0Y8", "marketplaceName": "Amazon.com.mx", "countryCode": "MX"},
                    "totalAmount": {"currencyCode": "MXN", "currencyAmount": 10},
                },
            ])
            service = Phase0Service(AppConfig(database_path=root / "ops.db"))
            with (
                patch.dict("os.environ", {"AI_CASHFLOW_MASTER_KEY": key}),
                patch("ai_cashflow.api.services.AmazonSpApiClient", FakePositionClient),
            ):
                position = service.get_amazon_financial_position(source["id"], "CAD")

        self.assertIsNone(position["source_amounts"]["funds_available"])
        self.assertEqual(position["marketplace_id"], "A2EUQ1WTGCTBG2")
        self.assertEqual(marketplace_ids, ["A2EUQ1WTGCTBG2"])
        self.assertEqual(expected_currencies, ["CAD"])
        self.assertEqual(position["source_id"], source["id"])
        self.assertEqual(position["fx_conversion"]["status"], "stale")
        self.assertEqual(position["fx_conversion"]["base_currency"], "USD")
        self.assertEqual(position["fx_conversion"]["currencies_included"], ["CAD"])
        self.assertEqual(position["source_status"]["seller_central_reconciliation"]["status"], "not_verified")
        self.assertEqual(position["source_status"]["reserve_coverage"]["status"], "unavailable")

    def test_get_summary_reads_generated_report_metrics(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = make_service(Path(temp_dir))

            summary = service.get_summary()

        self.assertEqual(summary.matched_payouts, 4)
        self.assertEqual(summary.unmatched_expected_payouts, 2)
        self.assertEqual(summary.unmatched_bank_receipts, 1)
        self.assertEqual(summary.data_quality_issues, 0)

    def test_get_exceptions_reads_exception_summary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = make_service(Path(temp_dir))
            service.run_report()

            exceptions = service.get_exceptions()

            self.assertEqual(exceptions["pending_receipt"], 2)
            self.assertEqual(exceptions["unmatched_bank_receipt"], 1)

    def test_run_report_regenerates_outputs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = make_service(Path(temp_dir))

            result = service.run_report()

            self.assertTrue(result.reports_dir.exists())
            self.assertTrue((result.reports_dir / "cfo_summary.md").exists())
            self.assertTrue((result.reports_dir / "cfo_dashboard.html").exists())

    def test_summary_includes_marketplace_transaction_metrics(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_dir = root / "samples" / "marketplaces" / "portal_reports"
            source_dir.mkdir(parents=True)
            (source_dir / "amazon_report.csv").write_bytes(AMAZON_REPORT.read_bytes())
            service = Phase0Service(
                AppConfig(
                    samples_dir=root / "samples",
                    reports_dir=root / "reports",
                    database_path=root / "ops.db",
                )
            )

            summary = service.get_summary()

        self.assertEqual(summary.marketplace_transaction_records, 2)
        self.assertEqual(summary.marketplace_net_activity, "37.85")
        self.assertEqual(summary.marketplace_deferred_amount, "63.35")
        self.assertEqual(summary.marketplace_released_amount, "-25.50")

    def test_marketplace_activity_reads_generated_report(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_dir = root / "samples" / "marketplaces" / "portal_reports"
            source_dir.mkdir(parents=True)
            (source_dir / "amazon_report.csv").write_bytes(AMAZON_REPORT.read_bytes())
            service = Phase0Service(
                AppConfig(
                    samples_dir=root / "samples",
                    reports_dir=root / "reports",
                    database_path=root / "ops.db",
                )
            )

            activity = service.get_marketplace_activity()

        self.assertEqual(len(activity), 2)
        self.assertEqual(activity[0]["marketplace"], "amazon.ca")
        self.assertEqual(activity[0]["currency"], "USD")
        self.assertEqual(activity[0]["status"], "Deferred")
        self.assertEqual(activity[0]["source_file"], "amazon_report.csv")

    def test_marketplace_activity_response_skips_incomplete_rows_without_fabricating_money(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            reports = root / "reports"
            reports.mkdir()
            (reports / "marketplace_activity.csv").write_text(
                "posted_at,source_id,settlement_id,type,marketplace,currency,total,status,release_date,order_id,source_file\n"
                "2026-08-04T19:34:01Z,,,,,,,,,,\n"
                "2026-08-04T19:35:01Z,source-1,group-1,Order,amazon.ca,CAD,12.34,Deferred,,order-1,report.csv\n"
                "2026-08-04T19:36:01Z,source-1,group-2,Order,amazon.ca,CAD,,Deferred,,order-2,report.csv\n",
                encoding="utf-8",
            )
            service = Phase0Service(AppConfig(
                samples_dir=root / "samples",
                reports_dir=reports,
                database_path=root / "ops.db",
            ))

            response = service.get_marketplace_activity_response()

        self.assertEqual(response["status"], "partial")
        self.assertEqual([row["total"] for row in response["rows"]], ["12.34"])
        self.assertEqual(response["diagnostics"]["skipped_row_count"], 2)
        self.assertIn("missing_required_fields", response["diagnostics"]["validation_reasons"])
        self.assertNotIn("0", [row["total"] for row in response["rows"]])

    def test_marketplace_activity_response_reports_unavailable_file_without_raising(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = make_service(Path(temp_dir))
            with patch.object(service, "run_report", side_effect=OSError("unavailable")):
                response = service.get_marketplace_activity_response()

        self.assertEqual(response["status"], "unavailable")
        self.assertEqual(response["rows"], [])
        self.assertEqual(response["diagnostics"]["validation_reason"], "marketplace_activity_unavailable")

    def test_source_evidence_explains_loaded_file_and_brd_gaps(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_dir = root / "samples" / "marketplaces" / "portal_reports"
            source_dir.mkdir(parents=True)
            (source_dir / "amazon_report.csv").write_bytes(AMAZON_REPORT.read_bytes())
            service = Phase0Service(
                AppConfig(
                    samples_dir=root / "samples",
                    reports_dir=root / "reports",
                    database_path=root / "ops.db",
                )
            )

            evidence = service.get_source_evidence()

        self.assertEqual(evidence[0]["source"], "Amazon Payments transaction report")
        self.assertEqual(evidence[0]["file"], "amazon_report.csv")
        self.assertIn("not confirmed bank cash", evidence[0]["limitation"])
        self.assertTrue(
            any(row["source"] == "Bank/payment receipt feed" for row in evidence)
        )


if __name__ == "__main__":
    unittest.main()
