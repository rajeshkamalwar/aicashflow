from pathlib import Path
import unittest

from ai_cashflow.ingestion import (
    load_amazon_flat_file_v2,
    load_amazon_transactions_csv,
    load_bank_receipts_csv,
    load_expected_payouts_csv,
)
from ai_cashflow.ingestion.csv_loader import is_amazon_flat_file_v2
from ai_cashflow.reconciliation import reconcile_payouts


ROOT = Path(__file__).resolve().parents[1]
PHASE0_FIXTURES = ROOT / "tests/fixtures/phase0_samples"


class CsvLoaderTests(unittest.TestCase):
    def test_loads_expected_payouts_sample(self):
        payouts = load_expected_payouts_csv(
            PHASE0_FIXTURES
            / "marketplaces/api/ergode_api_source_1_payouts_sample.csv"
        )

        self.assertEqual(len(payouts), 3)
        self.assertEqual(payouts[0].entity, "Ergode Inc.")
        self.assertEqual(payouts[0].reference, "SETTLEMENT-API1-001")

    def test_loads_bank_receipts_sample(self):
        receipts = load_bank_receipts_csv(
            PHASE0_FIXTURES / "banks/bank_of_america_receipts_sample.csv"
        )

        self.assertEqual(len(receipts), 4)
        self.assertEqual(receipts[-1].reference, "UNMATCHED-BANK-001")

    def test_loads_amazon_transaction_report_with_intro_rows(self):
        transactions = load_amazon_transactions_csv(
            ROOT / "tests/fixtures/amazon_transaction_report_sample.csv",
            source_id="amazon-source-a",
        )

        self.assertEqual(len(transactions), 2)
        self.assertEqual(transactions[0].settlement_id, "26483211921")
        self.assertEqual(transactions[0].marketplace, "amazon.ca")
        self.assertEqual(transactions[0].currency, "USD")
        self.assertEqual(str(transactions[0].product_sales), "75.71")
        self.assertEqual(str(transactions[0].selling_fees), "-12.36")
        self.assertEqual(str(transactions[0].total), "63.35")
        self.assertEqual(transactions[0].status, "Deferred")
        self.assertIsNone(transactions[0].release_date)
        self.assertEqual(transactions[1].status, "Released")
        self.assertEqual(transactions[1].release_date, "2026-06-01")
        self.assertEqual(transactions[0].source_id, "amazon-source-a")

    def test_flat_file_v2_keeps_the_amazon_source_id(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp_dir:
            report = Path(temp_dir) / "settlement.txt"
            report.write_text(
                "settlement-id\tcurrency\ttotal-amount\tdeposit-date\ttransaction-type\tamount-type\tamount-description\tamount\tmarketplace-name\tposted-date-time\n"
                "SET-1\tCAD\t125.50\t2026-07-15\t\t\t\t\t\t\n"
                "SET-1\tCAD\t\t\tOrder\tItemPrice\tPrincipal\t125.50\tAmazon.ca\t15.07.2026 10:00:00 UTC\n",
                encoding="utf-8",
            )

            payouts, transactions = load_amazon_flat_file_v2(
                report, entity="Ergode Inc.", source_id="amazon-source-a"
            )

        self.assertEqual(payouts[0].source_id, "amazon-source-a")
        self.assertEqual(transactions[0].source_id, "amazon-source-a")

    def test_flat_file_v2_accepts_amazon_payments_preamble(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp_dir:
            report = Path(temp_dir) / "settlement.txt"
            report.write_text(
                "Amazon Payments Singapore Private Limited\n\n\n\n\n\n"
                "settlement-id\tsettlement-start-date\tcurrency\ttotal-amount\tdeposit-date\ttransaction-type\tamount-type\tamount-description\tamount\tmarketplace-name\tposted-date-time\n"
                "SET-SG\t2026-07-01\tSGD\t125.50\t2026-07-15\t\t\t\t\t\t\n"
                "SET-SG\t\tSGD\t\t\tOrder\tItemPrice\tPrincipal\t125.50\tAmazon.sg\t2026-07-10\n",
                encoding="utf-8",
            )

            detected = is_amazon_flat_file_v2(report)
            payouts, transactions = load_amazon_flat_file_v2(report)

        self.assertTrue(detected)
        self.assertEqual(payouts[0].payout_id, "SET-SG")
        self.assertEqual(payouts[0].marketplace, "Amazon.sg")
        self.assertEqual(transactions[0].currency, "SGD")

    def test_reconciles_synthetic_phase0_samples(self):
        payouts = []
        payouts.extend(
            load_expected_payouts_csv(
                PHASE0_FIXTURES
                / "marketplaces/api/ergode_api_source_1_payouts_sample.csv"
            )
        )
        payouts.extend(
            load_expected_payouts_csv(
                PHASE0_FIXTURES
                / "marketplaces/portal_reports/ergode_portal_source_1_payouts_sample.csv"
            )
        )
        payouts.extend(
            load_expected_payouts_csv(
                PHASE0_FIXTURES
                / "marketplaces/emails/ergode_email_source_1_payouts_sample.csv"
            )
        )

        receipts = []
        receipts.extend(
            load_bank_receipts_csv(
                PHASE0_FIXTURES / "banks/bank_of_america_receipts_sample.csv"
            )
        )
        receipts.extend(
            load_bank_receipts_csv(
                PHASE0_FIXTURES / "banks/payoneer_receipts_sample.csv"
            )
        )

        result = reconcile_payouts(payouts, receipts)

        self.assertEqual(len(result.matched), 4)
        self.assertEqual(len(result.unmatched_expected), 2)
        self.assertEqual(len(result.unmatched_receipts), 1)


if __name__ == "__main__":
    unittest.main()
