from pathlib import Path
from decimal import Decimal
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from ai_cashflow.reporting import discover_source_files, generate_phase0_reports


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "tests" / "fixtures" / "phase0_samples"
AMAZON_REPORT = ROOT / "tests" / "fixtures" / "amazon_transaction_report_sample.csv"


class ReportingTests(unittest.TestCase):
    def test_canonical_payouts_are_unavailable_until_a_group_sync_completes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db = root / "operations.sqlite3"
            connection = sqlite3.connect(db)
            try:
                connection.executescript("""
                create table amazon_transaction_state (source_id text, marketplace_id text, transaction_id text, marketplace_name text, currency text, status text, retrieved_at text, fingerprint text, conflict integer, included_in_totals integer, total_amount text, transaction_type text, posted_date text, composition_json text);
                create table bank_receipts (receipt_id text, bank_account text, currency text, receipt_date text, amount text, reference text);
                create table amazon_financial_event_group_state (source_id text, financial_event_group_id text, currency text, original_total text, processing_status text, fund_transfer_status text, fund_transfer_date text, financial_event_group_start text, financial_event_group_end text, retrieved_at text, source_sync_run_id text, fingerprint text, classification text, included_as_completed_payout integer);
                create table amazon_financial_event_group_sync_state (source_id text primary key, initialized integer not null, last_successful_sync_at text, last_successful_run_id text, retrieved_at text not null);
                insert into amazon_transaction_state values ('s','m','current','Amazon.ca','CAD','DEFERRED','2026-08-12','fp',0,1,'10.00','Order','2026-08-01','{}');
                """)
            finally:
                connection.close()

            output = generate_phase0_reports(root / "samples", root / "reports", database_path=db, source_mode="CANONICAL_DATABASE", usd_exchange_rates={"USD": Decimal("1"), "CAD": Decimal("1")})
            metadata = (output / "report_metadata.json").read_text(encoding="utf-8")

        self.assertIn('"status": "DATA_UNAVAILABLE"', metadata)
        self.assertIn("CANONICAL_PAYOUT_STATE_NOT_INITIALIZED", metadata)
        self.assertNotIn('"completed_payout_data": "DATA_AVAILABLE_EMPTY"', metadata)

    def test_initialized_empty_canonical_payout_state_is_available_empty(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db = root / "operations.sqlite3"
            connection = sqlite3.connect(db)
            try:
                connection.executescript("""
                create table amazon_transaction_state (source_id text, marketplace_id text, transaction_id text, marketplace_name text, currency text, status text, retrieved_at text, fingerprint text, conflict integer, included_in_totals integer, total_amount text, transaction_type text, posted_date text, composition_json text);
                create table bank_receipts (receipt_id text, bank_account text, currency text, receipt_date text, amount text, reference text);
                create table amazon_financial_event_group_state (source_id text, financial_event_group_id text, currency text, original_total text, processing_status text, fund_transfer_status text, fund_transfer_date text, financial_event_group_start text, financial_event_group_end text, retrieved_at text, source_sync_run_id text, fingerprint text, classification text, included_as_completed_payout integer);
                create table amazon_financial_event_group_sync_state (source_id text primary key, initialized integer not null, last_successful_sync_at text, last_successful_run_id text, retrieved_at text not null);
                insert into amazon_transaction_state values ('s','m','current','Amazon.ca','CAD','DEFERRED','2026-08-12','fp',0,1,'10.00','Order','2026-08-01','{}');
                insert into amazon_financial_event_group_sync_state values ('s',1,'2026-08-12T00:00:00Z','run-1','2026-08-12T00:00:00Z');
                """)
            finally:
                connection.close()

            output = generate_phase0_reports(root / "samples", root / "reports", database_path=db, source_mode="CANONICAL_DATABASE", usd_exchange_rates={"USD": Decimal("1"), "CAD": Decimal("1")})
            metadata = (output / "report_metadata.json").read_text(encoding="utf-8")

        self.assertIn('"status": "DATA_AVAILABLE_EMPTY"', metadata)

    def test_initialized_canonical_completed_payout_state_is_available(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db = root / "operations.sqlite3"
            connection = sqlite3.connect(db)
            try:
                connection.executescript("""
                create table amazon_transaction_state (source_id text, marketplace_id text, transaction_id text, marketplace_name text, currency text, status text, retrieved_at text, fingerprint text, conflict integer, included_in_totals integer, total_amount text, transaction_type text, posted_date text, composition_json text);
                create table bank_receipts (receipt_id text, bank_account text, currency text, receipt_date text, amount text, reference text);
                create table amazon_financial_event_group_state (source_id text, financial_event_group_id text, currency text, original_total text, processing_status text, fund_transfer_status text, fund_transfer_date text, financial_event_group_start text, financial_event_group_end text, retrieved_at text, source_sync_run_id text, fingerprint text, classification text, included_as_completed_payout integer);
                create table amazon_financial_event_group_sync_state (source_id text primary key, initialized integer not null, last_successful_sync_at text, last_successful_run_id text, retrieved_at text not null);
                insert into amazon_transaction_state values ('s','m','current','Amazon.ca','CAD','DEFERRED','2026-08-12','fp',0,1,'10.00','Order','2026-08-01','{}');
                insert into amazon_financial_event_group_sync_state values ('s',1,'2026-08-12T00:00:00Z','run-1','2026-08-12T00:00:00Z');
                insert into amazon_financial_event_group_state values ('s','group-1','CAD','25.00','CLOSED','SUCCEEDED','2026-08-13','2026-08-01','2026-08-12','2026-08-12','run-1','fp','COMPLETED_PAYOUT',1);
                """)
            finally:
                connection.close()

            output = generate_phase0_reports(root / "samples", root / "reports", database_path=db, source_mode="CANONICAL_DATABASE", usd_exchange_rates={"USD": Decimal("1"), "CAD": Decimal("1")})
            metadata = (output / "report_metadata.json").read_text(encoding="utf-8")
            payouts = (output / "unmatched_expected_payouts.csv").read_text(encoding="utf-8")

        self.assertIn('"status": "DATA_AVAILABLE"', metadata)
        self.assertIn("group-1", payouts)

    def test_canonical_reporting_does_not_load_legacy_payout_sources(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db = root / "operations.sqlite3"
            connection = sqlite3.connect(db)
            try:
                connection.executescript("""
                create table amazon_transaction_state (source_id text, marketplace_id text, transaction_id text, marketplace_name text, currency text, status text, retrieved_at text, fingerprint text, conflict integer, included_in_totals integer, total_amount text, transaction_type text, posted_date text, composition_json text);
                create table bank_receipts (receipt_id text, bank_account text, currency text, receipt_date text, amount text, reference text);
                create table amazon_financial_event_group_state (source_id text, financial_event_group_id text, currency text, original_total text, processing_status text, fund_transfer_status text, fund_transfer_date text, financial_event_group_start text, financial_event_group_end text, retrieved_at text, source_sync_run_id text, fingerprint text, classification text, included_as_completed_payout integer);
                create table amazon_financial_event_group_sync_state (source_id text primary key, initialized integer not null, last_successful_sync_at text, last_successful_run_id text, retrieved_at text not null);
                insert into amazon_transaction_state values ('s','m','current','Amazon.ca','CAD','DEFERRED','2026-08-12','fp',0,1,'10.00','Order','2026-08-01','{}');
                """)
            finally:
                connection.close()

            with patch("ai_cashflow.reporting._load_payouts", side_effect=AssertionError("legacy payout fallback")), patch("ai_cashflow.reporting._load_flat_file_v2", side_effect=AssertionError("legacy SP-API fallback")):
                generate_phase0_reports(root / "samples", root / "reports", database_path=db, source_mode="CANONICAL_DATABASE", usd_exchange_rates={"USD": Decimal("1"), "CAD": Decimal("1")})
    def test_canonical_database_uses_one_current_included_state_row(self):
        for _ in range(50):
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                db = root / "operations.sqlite3"
                connection = sqlite3.connect(db)
                try:
                    connection.executescript("""
                    create table amazon_transaction_state (source_id text, marketplace_id text, transaction_id text, marketplace_name text, currency text, status text, retrieved_at text, fingerprint text, conflict integer, included_in_totals integer, total_amount text, transaction_type text, posted_date text, composition_json text);
                    create table bank_receipts (receipt_id text, bank_account text, currency text, receipt_date text, amount text, reference text);
                    create table amazon_financial_event_group_state (source_id text, financial_event_group_id text, currency text, original_total text, processing_status text, fund_transfer_status text, fund_transfer_date text, financial_event_group_start text, financial_event_group_end text, retrieved_at text, source_sync_run_id text, fingerprint text, classification text, included_as_completed_payout integer);
                    create table amazon_financial_event_group_sync_state (source_id text primary key, initialized integer not null, last_successful_sync_at text, last_successful_run_id text, retrieved_at text not null);
                    insert into amazon_transaction_state values ('s','m','current','Amazon.ca','CAD','DEFERRED','2026-08-12','fp',0,1,'10.00','Order','2026-08-01','{}');
                    insert into amazon_transaction_state values ('s','m','excluded','Amazon.ca','CAD','RELEASED','2026-08-12','fp2',0,0,'99.00','Order','2026-08-01','{}');
                    """)
                finally:
                    connection.close()
                output = generate_phase0_reports(root / "samples", root / "reports", database_path=db, source_mode="CANONICAL_DATABASE", usd_exchange_rates={"USD": Decimal("1"), "CAD": Decimal("1")})
                activity = (output / "marketplace_activity.csv").read_text(encoding="utf-8")
                metadata = (output / "report_metadata.json").read_text(encoding="utf-8")
                self.assertIn("current", activity)
                self.assertNotIn("excluded", activity)
                self.assertIn('"transaction_count": 1', metadata)
                self.assertIn('"source_mode": "CANONICAL_DATABASE"', metadata)
                self.assertIn('"data_version":', metadata)
                self.assertIn("NO_BANK_RECEIPTS_IMPORTED", metadata)
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
