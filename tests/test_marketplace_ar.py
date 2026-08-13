import csv
from datetime import date
from pathlib import Path
import tempfile
import unittest

from ai_cashflow.marketplace_ar import MARKETPLACE_AR_FIELDS, summarize_marketplace_ar


class MarketplaceArTests(unittest.TestCase):
    def test_quarantines_duplicate_rows_and_flags_unapproved_source_gaps(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            registry = root / "source_registry.csv"
            with registry.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
                        "source_id", "source_name", "active", "connector_type",
                        "connector_status", "verification_status",
                    ],
                )
                writer.writeheader()
                writer.writerows([
                    {
                        "source_id": "amazon-ca", "source_name": "Amazon CA",
                        "active": "true", "connector_type": "amazon_sp_api",
                        "connector_status": "ready",
                        "verification_status": "approved",
                    },
                    {
                        "source_id": "alibris", "source_name": "Alibris",
                        "active": "true", "connector_type": "manual_csv",
                        "connector_status": "ready",
                        "verification_status": "pending",
                    },
                    {
                        "source_id": "missing", "source_name": "Missing source",
                        "active": "true", "connector_type": "manual_csv",
                        "connector_status": "ready",
                        "verification_status": "approved",
                    },
                ])
            with (root / "snapshot.csv").open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
                        "snapshot_date", "rate_date", "rate_source", "source_id",
                        "account_id", "venue", "group", "local_amount", "currency",
                        "usd_rate",
                    ],
                )
                writer.writeheader()
                writer.writerows([
                    {
                        "snapshot_date": "2026-07-13", "rate_date": "2026-07-09",
                        "rate_source": "ECB euro reference rates", "source_id": "amazon-ca",
                        "account_id": "amazon-ca", "venue": "Amazon CA", "group": "Amazon",
                        "local_amount": "100", "currency": "CAD", "usd_rate": "0.70",
                    },
                    {
                        "snapshot_date": "2026-07-13", "rate_date": "2026-07-09",
                        "rate_source": "Fixed USD", "source_id": "alibris",
                        "account_id": "alibris", "venue": "Alibris",
                        "group": "Own Webstores / Other", "local_amount": "50",
                        "currency": "USD", "usd_rate": "1",
                    },
                    {
                        "snapshot_date": "2026-07-13", "rate_date": "2026-07-09",
                        "rate_source": "Fixed USD", "source_id": "alibris",
                        "account_id": "alibris", "venue": "Alibris",
                        "group": "Own Webstores / Other", "local_amount": "50",
                        "currency": "USD", "usd_rate": "1",
                    },
                ])

            summary = summarize_marketplace_ar(
                root,
                registry_path=registry,
                as_of_date=date(2026, 7, 13),
            )

        self.assertEqual(summary["currency"], "USD")
        self.assertEqual(summary["gross_received_ar_usd"], "170.00")
        self.assertEqual(summary["total_ar_usd"], "170.00")
        self.assertEqual(summary["controlled_total_ar_usd"], "120.00")
        self.assertEqual(summary["fx_exposed_ar_usd"], "70.00")
        self.assertEqual(summary["marketplace_count"], 3)
        self.assertEqual(summary["unique_marketplace_count"], 2)
        self.assertEqual(summary["counted_row_count"], 2)
        self.assertEqual(summary["quarantined_row_count"], 1)
        self.assertEqual(summary["received_source_count"], 2)
        self.assertEqual(summary["expected_source_count"], 3)
        self.assertEqual(summary["missing_sources"], ["Missing source"])
        self.assertFalse(summary["coverage_verified"])
        self.assertEqual(summary["pending_source_approvals"], ["Alibris"])
        self.assertEqual(summary["pending_connector_count"], 0)
        self.assertEqual(summary["duplicate_rows"], 1)
        self.assertEqual(summary["duplicate_value_usd"], "50.00")
        self.assertEqual(summary["top_balances"][0]["venue"], "Amazon CA")

    def test_quarantines_all_conflicting_rows_for_one_account(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with (root / "snapshot.csv").open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
                        "snapshot_date", "rate_date", "rate_source", "source_id",
                        "account_id", "venue", "group", "local_amount", "currency",
                        "usd_rate",
                    ],
                )
                writer.writeheader()
                for amount in ("100", "110"):
                    writer.writerow({
                        "snapshot_date": "2026-07-13", "rate_date": "2026-07-09",
                        "rate_source": "ECB euro reference rates", "source_id": "amazon-ca",
                        "account_id": "amazon-ca", "venue": "Amazon CA",
                        "group": "Amazon", "local_amount": amount,
                        "currency": "CAD", "usd_rate": "0.70",
                    })

            summary = summarize_marketplace_ar(
                root,
                as_of_date=date(2026, 7, 13),
            )

        self.assertEqual(summary["gross_received_ar_usd"], "147.00")
        self.assertEqual(summary["total_ar_usd"], "147.00")
        self.assertEqual(summary["controlled_total_ar_usd"], "0.00")
        self.assertEqual(summary["quarantined_row_count"], 2)
        self.assertEqual(summary["conflicting_rows"], 2)

    def test_reference_snapshot_matches_excel_dashboard_logic(self):
        fixture_root = Path(__file__).resolve().parent / "fixtures" / "marketplace_ar"

        summary = summarize_marketplace_ar(
            fixture_root,
            registry_path=fixture_root / "source_registry.csv",
            fx_path=fixture_root / "config" / "fx_rates.csv",
            as_of_date=date(2026, 7, 17),
        )

        self.assertEqual(summary["currency"], "USD")
        self.assertEqual(summary["total_ar_usd"], "360.00")
        self.assertEqual(summary["controlled_total_ar_usd"], "360.00")
        self.assertEqual(summary["marketplace_count"], 5)
        self.assertEqual(summary["unique_marketplace_count"], 5)
        self.assertEqual(summary["fx_exposed_ar_usd"], "0.00")
        self.assertFalse(summary["display_ready"])
        self.assertEqual(
            {row["name"]: row["amount_usd"] for row in summary["channel_breakdown"]},
            {
                "Amazon": "100.00",
                "Own Webstores / Other": "200.00",
                "OnBuy": "30.00",
                "Walmart": "20.00",
                "MercadoLibre": "10.00",
            },
        )

    def test_api_only_mode_ignores_non_api_snapshots(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            registry = root / "source_registry.csv"
            with registry.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
                        "source_id", "source_name", "active", "connector_type",
                        "connector_status", "verification_status",
                    ],
                )
                writer.writeheader()
                writer.writerow({
                    "source_id": "alibris", "source_name": "Alibris",
                    "active": "true", "connector_type": "api",
                    "connector_status": "ready", "verification_status": "approved",
                })
            rows = [{
                "snapshot_date": "2026-07-13", "rate_date": "2026-07-09",
                "rate_source": "Fixed USD", "source_id": "alibris",
                "account_id": "alibris", "venue": "Alibris",
                "group": "Own Webstores / Other", "local_amount": "100",
                "currency": "USD", "usd_rate": "1",
            }]
            for name, amount in (
                ("client_snapshot.csv", "100"),
                ("api_alibris_marketplace_ar.csv", "25"),
            ):
                rows[0]["local_amount"] = amount
                with (root / name).open("w", newline="", encoding="utf-8") as handle:
                    writer = csv.DictWriter(handle, fieldnames=MARKETPLACE_AR_FIELDS)
                    writer.writeheader()
                    writer.writerows(rows)

            summary = summarize_marketplace_ar(
                root,
                registry_path=registry,
                as_of_date=date(2026, 7, 13),
                ingest_mode="api_only",
            )

        self.assertEqual(summary["total_ar_usd"], "25.00")
        self.assertEqual(summary["ingest_mode"], "api_only")
        self.assertTrue(summary["api_only_active"])
        self.assertTrue(summary["api_cutover_ready"])
        self.assertTrue(summary["display_ready"])
        self.assertEqual(summary["api_received_source_count"], 1)

    def test_rejects_fx_rates_after_the_snapshot_date(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with (root / "snapshot.csv").open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
                        "snapshot_date", "rate_date", "rate_source", "source_id",
                        "account_id", "venue", "group", "local_amount", "currency",
                        "usd_rate",
                    ],
                )
                writer.writeheader()
                writer.writerow({
                    "snapshot_date": "2026-07-13", "rate_date": "2026-07-14",
                    "rate_source": "ECB euro reference rates", "source_id": "amazon-ca",
                    "account_id": "amazon-ca", "venue": "Amazon CA",
                    "group": "Amazon", "local_amount": "100",
                    "currency": "CAD", "usd_rate": "0.70",
                })

            with self.assertRaisesRegex(ValueError, "rate_date cannot be after snapshot_date"):
                summarize_marketplace_ar(root, as_of_date=date(2026, 7, 13))

    def test_rejects_unapproved_fx_rate_source(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with (root / "snapshot.csv").open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=MARKETPLACE_AR_FIELDS)
                writer.writeheader()
                writer.writerow({
                    "snapshot_date": "2026-07-13", "rate_date": "2026-07-09",
                    "rate_source": "Unverified feed", "source_id": "amazon-ca",
                    "account_id": "amazon-ca", "venue": "Amazon CA",
                    "group": "Amazon", "local_amount": "100",
                    "currency": "CAD", "usd_rate": "0.70",
                })

            with self.assertRaisesRegex(ValueError, "must use ECB euro reference rates"):
                summarize_marketplace_ar(root, as_of_date=date(2026, 7, 13))

    def test_blocks_currency_mismatch_against_the_source_registry(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            registry = root / "source_registry.csv"
            with registry.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
                        "source_id", "source_name", "active", "connector_type",
                        "connector_status", "verification_status",
                        "expected_currency",
                    ],
                )
                writer.writeheader()
                writer.writerow({
                    "source_id": "amazon-ca", "source_name": "Amazon CA",
                    "active": "true", "connector_type": "api",
                    "connector_status": "ready", "verification_status": "approved",
                    "expected_currency": "USD",
                })
            with (root / "snapshot.csv").open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=MARKETPLACE_AR_FIELDS)
                writer.writeheader()
                writer.writerow({
                    "snapshot_date": "2026-07-13", "rate_date": "2026-07-09",
                    "rate_source": "ECB euro reference rates", "source_id": "amazon-ca",
                    "account_id": "amazon-ca", "venue": "Amazon CA",
                    "group": "Amazon", "local_amount": "100",
                    "currency": "CAD", "usd_rate": "0.70",
                })

            summary = summarize_marketplace_ar(
                root,
                registry_path=registry,
                as_of_date=date(2026, 7, 13),
            )

        self.assertEqual(summary["status"], "blocked")
        self.assertEqual(summary["currency_mismatches"], ["Amazon CA: expected USD, received CAD"])

    def test_reports_unavailable_when_no_snapshot_exists(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            summary = summarize_marketplace_ar(Path(temp_dir))

        self.assertEqual(summary["status"], "unavailable")
        self.assertEqual(summary["currency"], "USD")


if __name__ == "__main__":
    unittest.main()
