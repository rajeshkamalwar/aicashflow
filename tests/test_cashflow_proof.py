import tempfile
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from ai_cashflow.models import BankReceipt
from ai_cashflow.proof import evaluate_amazon_proof, match_bank_receipts
from ai_cashflow.storage import OperationalStore


class CashflowProofTests(unittest.TestCase):
    def test_exact_closed_payment_is_amazon_verified(self):
        result = evaluate_amazon_proof(
            {
                "FinancialEventGroupId": "GROUP-1",
                "ProcessingStatus": "Closed",
                "FundTransferDate": "2026-07-01T00:00:00Z",
                "OriginalTotal": {
                    "CurrencyCode": "USD",
                    "CurrencyAmount": "100.00",
                },
            },
            [
                {"totalAmount": {"currencyCode": "USD", "currencyAmount": "60.00"}},
                {"totalAmount": {"currencyCode": "USD", "currencyAmount": "40.00"}},
            ],
            settlement_id="GROUP-1",
            settlement_total=Decimal("100.00"),
            tolerance=Decimal("0"),
            now=datetime(2026, 7, 10, tzinfo=timezone.utc),
        )

        self.assertEqual(result["status"], "Amazon verified")
        self.assertEqual(result["amazon_payment_amount"], "100.00")
        self.assertEqual(result["finances_total"], "100.00")
        self.assertEqual(result["settlement_total"], "100.00")
        self.assertEqual(result["amazon_variance"], "0.00")

    def test_mismatched_settlement_is_a_variance(self):
        result = evaluate_amazon_proof(
            {
                "FinancialEventGroupId": "GROUP-1",
                "ProcessingStatus": "Closed",
                "FundTransferDate": "2026-07-01T00:00:00Z",
                "OriginalTotal": {
                    "CurrencyCode": "USD",
                    "CurrencyAmount": "100.00",
                },
            },
            [{"totalAmount": {"currencyCode": "USD", "currencyAmount": "100.00"}}],
            settlement_id="GROUP-1",
            settlement_total=Decimal("99.00"),
            tolerance=Decimal("0"),
            now=datetime(2026, 7, 10, tzinfo=timezone.utc),
        )

        self.assertEqual(result["status"], "Variance")
        self.assertEqual(result["amazon_variance"], "1.00")

    def test_missing_recent_evidence_stays_pending_for_48_hours(self):
        result = evaluate_amazon_proof(
            {
                "FinancialEventGroupId": "GROUP-1",
                "ProcessingStatus": "Closed",
                "FundTransferDate": "2026-07-09T00:00:00Z",
                "OriginalTotal": {
                    "CurrencyCode": "USD",
                    "CurrencyAmount": "100.00",
                },
            },
            [],
            settlement_id=None,
            settlement_total=None,
            tolerance=Decimal("0"),
            now=datetime(2026, 7, 10, tzinfo=timezone.utc),
        )

        self.assertEqual(result["status"], "Pending")

    def test_matching_bank_receipt_upgrades_amazon_proof_to_verified(self):
        proofs = [{
            "status": "Amazon verified",
            "payment_group_id": "GROUP-1",
            "currency": "USD",
            "amazon_payment_amount": "100.00",
            "payment_date": "2026-07-01",
        }]
        receipts = [BankReceipt(
            receipt_id="R1",
            bank_account="OPERATING",
            currency="USD",
            receipt_date=date(2026, 7, 2),
            amount=Decimal("100.00"),
            reference="GROUP-1",
        )]

        matched = match_bank_receipts(
            proofs,
            receipts,
            date_tolerance_days=3,
            amount_tolerance=Decimal("0"),
        )

        self.assertEqual(matched[0]["status"], "Verified")
        self.assertEqual(matched[0]["bank_receipt_id"], "R1")
        self.assertEqual(matched[0]["bank_variance"], "0.00")

    def test_wrong_bank_reference_remains_an_explicit_exception(self):
        proof = {
            "status": "Amazon verified",
            "payment_group_id": "GROUP-1",
            "currency": "USD",
            "amazon_payment_amount": "100.00",
            "payment_date": "2026-07-01",
        }
        receipt = BankReceipt(
            receipt_id="R1",
            bank_account="OPERATING",
            currency="USD",
            receipt_date=date(2026, 7, 2),
            amount=Decimal("100.00"),
            reference="WRONG",
        )

        result = match_bank_receipts(
            [proof],
            [receipt],
            date_tolerance_days=3,
            amount_tolerance=Decimal("0"),
        )

        self.assertEqual(result[0]["status"], "Amazon verified")
        self.assertIn("No bank receipt matched", result[0]["last_error"])


class BankStatementStorageTests(unittest.TestCase):
    def test_import_rejects_duplicate_file_hash_and_receipt_id(self):
        receipt = BankReceipt(
            receipt_id="R1",
            bank_account="OPERATING",
            currency="USD",
            receipt_date=date(2026, 7, 2),
            amount=Decimal("100.00"),
            reference="GROUP-1",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            store = OperationalStore(Path(temp_dir) / "ops.db")
            imported = store.import_bank_statement(
                file_name="statement.csv",
                file_sha256="abc123",
                bank_source="Main bank",
                receipts=[receipt],
                imported_at="2026-07-10T00:00:00+00:00",
            )

            with self.assertRaisesRegex(ValueError, "already imported"):
                store.import_bank_statement(
                    file_name="statement-copy.csv",
                    file_sha256="abc123",
                    bank_source="Main bank",
                    receipts=[receipt],
                    imported_at="2026-07-10T01:00:00+00:00",
                )
            with self.assertRaisesRegex(ValueError, "receipt ID"):
                store.import_bank_statement(
                    file_name="different.csv",
                    file_sha256="def456",
                    bank_source="Main bank",
                    receipts=[receipt],
                    imported_at="2026-07-10T01:00:00+00:00",
                )

            rows = store.list_bank_receipts()

        self.assertEqual(imported["row_count"], 1)
        self.assertEqual(rows[0]["receipt_id"], "R1")
        self.assertEqual(rows[0]["amount"], "100.00")

    def test_proof_records_are_idempotent_and_summarized(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = OperationalStore(Path(temp_dir) / "ops.db")
            run_id = store.start_proof_run(
                window_days=90,
                source_count=1,
                started_at="2026-07-10T00:00:00+00:00",
            )
            proof = {
                "source_id": "SOURCE-1",
                "source_name": "Amazon US",
                "marketplace_id": "ATVPDKIKX0DER",
                "payment_group_id": "GROUP-1",
                "settlement_id": "GROUP-1",
                "report_id": "REPORT-1",
                "currency": "USD",
                "amazon_payment_amount": "100.00",
                "finances_total": "100.00",
                "settlement_total": "100.00",
                "amazon_variance": "0.00",
                "payment_date": "2026-07-01",
                "bank_receipt_id": None,
                "bank_received_amount": None,
                "bank_variance": None,
                "status": "Amazon verified",
                "evidence_hash": "hash-1",
                "checked_at": "2026-07-10T00:00:00+00:00",
                "last_error": None,
            }
            store.upsert_cashflow_proof(proof)
            proof.update({
                "status": "Verified",
                "bank_receipt_id": "R1",
                "bank_received_amount": "100.00",
                "bank_variance": "0.00",
            })
            store.upsert_cashflow_proof(proof)
            store.finish_proof_run(
                run_id,
                status="completed",
                item_count=1,
                source_success_count=1,
                completed_at="2026-07-10T00:01:00+00:00",
                message="Verified 1 payment.",
            )

            items = store.list_cashflow_proofs(limit=20, offset=0)
            summary = store.get_cashflow_proof_summary(enabled_source_count=1)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["status"], "Verified")
        self.assertEqual(items[0]["bank_receipt_id"], "R1")
        self.assertEqual(summary["source_coverage"], "1/1")
        self.assertEqual(summary["verified_count"], 1)
        self.assertEqual(summary["verified_amounts"], {"USD": "100.00"})
        self.assertEqual(summary["unexplained_variances"], {"USD": "0.00"})


if __name__ == "__main__":
    unittest.main()
