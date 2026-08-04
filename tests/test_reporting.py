from pathlib import Path
from decimal import Decimal
import shutil
import tempfile
import unittest

from ai_cashflow.reporting import discover_source_files, generate_phase0_reports


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "tests" / "fixtures" / "phase0_samples"
AMAZON_REPORT = ROOT / "tests" / "fixtures" / "amazon_transaction_report_sample.csv"


class ReportingTests(unittest.TestCase):
    def test_discovers_phase0_source_files(self):
        sources = discover_source_files(SAMPLES)

        self.assertEqual(len(sources.payout_files), 3)
        self.assertEqual(len(sources.receipt_files), 2)

    def test_excludes_managed_reports_from_inactive_amazon_sources(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for source_id in ("active-source", "inactive-source"):
                folder = root / "marketplaces" / "api" / source_id
                folder.mkdir(parents=True)
                (folder / "amazon_sp_api_report.txt").write_text(
                    "settlement-id\tsettlement-start-date\n",
                    encoding="utf-8",
                )

            sources = discover_source_files(
                root,
                active_amazon_source_ids={"active-source"},
            )

        self.assertEqual(
            [path.parent.name for path in sources.flat_file_v2_files],
            ["active-source"],
        )

    def test_generates_phase0_report_outputs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = generate_phase0_reports(SAMPLES, temp_dir)

            self.assertTrue((output / "cfo_summary.md").exists())
            self.assertTrue((output / "matched_payouts.csv").exists())
            self.assertTrue((output / "unmatched_expected_payouts.csv").exists())
            self.assertTrue((output / "unmatched_bank_receipts.csv").exists())
            self.assertTrue((output / "exception_summary.csv").exists())
            self.assertTrue((output / "data_quality_report.csv").exists())
            self.assertTrue((output / "cfo_dashboard.html").exists())

            summary = (output / "cfo_summary.md").read_text(encoding="utf-8")
            self.assertIn("Matched payouts: 4", summary)
            self.assertIn("Unmatched expected payouts: 2", summary)
            self.assertIn("Unmatched bank/payment receipts: 1", summary)
            self.assertIn("pending_receipt", summary)
            self.assertIn("Data quality issues: 0", summary)

            dashboard = (output / "cfo_dashboard.html").read_text(encoding="utf-8")
            self.assertIn("Cash Position Report", dashboard)
            self.assertIn("Matched Payouts", dashboard)
            self.assertIn("Data Quality Issues", dashboard)
            self.assertIn("pending_receipt", dashboard)
            self.assertIn("39650.75", dashboard)
            self.assertNotIn("Phase 0", dashboard)

    def test_generates_amazon_transaction_report_metrics(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_dir = root / "samples" / "marketplaces" / "api" / "amazon-source-a"
            source_dir.mkdir(parents=True)
            shutil.copy(AMAZON_REPORT, source_dir / "amazon_report.csv")

            output = generate_phase0_reports(root / "samples", root / "reports")

            summary = (output / "cfo_summary.md").read_text(encoding="utf-8")
            self.assertIn("Marketplace sample files: 1", summary)
            self.assertIn("Expected payout records: 0", summary)
            self.assertIn("Bank/payment receipt records: 0", summary)
            self.assertIn("Marketplace transaction records: 2", summary)
            self.assertIn("Marketplace net activity: 37.85", summary)
            self.assertIn("Marketplace deferred amount: 63.35", summary)
            self.assertIn("Marketplace released amount: -25.50", summary)
            self.assertTrue((output / "marketplace_activity.csv").exists())
            self.assertTrue((output / "source_evidence.csv").exists())

            activity = (output / "marketplace_activity.csv").read_text(
                encoding="utf-8"
            )
            evidence = (output / "source_evidence.csv").read_text(encoding="utf-8")
            self.assertIn("source_file", activity)
            self.assertIn("source_id", activity)
            self.assertIn("amazon-source-a", activity)
            self.assertIn("amazon_report.csv", activity)
            self.assertIn("Amazon Payments transaction report", evidence)
            self.assertIn("Bank/payment receipt feed", evidence)
            self.assertIn("Missing", evidence)
            self.assertIn("not confirmed bank cash", evidence)

    def test_converts_source_amounts_to_usd_before_aggregation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "samples" / "marketplaces" / "api" / "settlement.txt"
            source.parent.mkdir(parents=True)
            source.write_text(
                "settlement-id\tsettlement-start-date\tsettlement-end-date\t"
                "currency\ttotal-amount\tdeposit-date\t"
                "transaction-type\tamount-type\tamount-description\tamount\n"
                "SET-1\t2026-07-01\t2026-07-15\tCAD\t100.00\t2026-07-16\t\t\t\t\n"
                "SET-1\t\t\tCAD\t\t\tOrder\tItemPrice\tPrincipal\t100.00\n",
                encoding="utf-8",
            )

            output = generate_phase0_reports(
                root / "samples",
                root / "reports",
                usd_exchange_rates={"USD": Decimal("1"), "CAD": Decimal("0.75")},
            )

            summary = (output / "cfo_summary.md").read_text(encoding="utf-8")
            activity = (output / "marketplace_activity.csv").read_text(
                encoding="utf-8"
            )
            self.assertIn("Total expected payouts: 75.00", summary)
            self.assertIn("Marketplace net activity: 75.00", summary)
            self.assertIn(",USD,75.00,", activity)


if __name__ == "__main__":
    unittest.main()
