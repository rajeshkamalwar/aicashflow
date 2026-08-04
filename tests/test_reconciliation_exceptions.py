from datetime import date
from decimal import Decimal
import unittest

from ai_cashflow import BankReceipt, ExpectedPayout, reconcile_payouts


class ReconciliationExceptionTests(unittest.TestCase):
    def test_matches_with_date_and_amount_tolerance(self):
        payout = ExpectedPayout(
            payout_id="payout-1",
            entity="Ergode Inc.",
            marketplace="Marketplace A",
            currency="USD",
            expected_date=date(2026, 5, 20),
            amount=Decimal("100.00"),
            reference="SETTLEMENT-001",
        )
        receipt = BankReceipt(
            receipt_id="receipt-1",
            bank_account="Operating USD",
            currency="USD",
            receipt_date=date(2026, 5, 22),
            amount=Decimal("99.50"),
            reference="SETTLEMENT-001",
        )

        result = reconcile_payouts(
            [payout],
            [receipt],
            date_tolerance_days=3,
            amount_tolerance=Decimal("1.00"),
        )

        self.assertEqual(result.matched, [(payout, receipt)])
        self.assertEqual(result.exceptions, [])

    def test_classifies_short_paid_expected_payout(self):
        payout = ExpectedPayout(
            payout_id="payout-1",
            entity="Ergode Inc.",
            marketplace="Marketplace A",
            currency="USD",
            expected_date=date(2026, 5, 20),
            amount=Decimal("100.00"),
            reference="SETTLEMENT-001",
        )
        receipt = BankReceipt(
            receipt_id="receipt-1",
            bank_account="Operating USD",
            currency="USD",
            receipt_date=date(2026, 5, 20),
            amount=Decimal("95.00"),
            reference="SETTLEMENT-001",
        )

        result = reconcile_payouts(
            [payout],
            [receipt],
            amount_tolerance=Decimal("1.00"),
        )

        self.assertEqual(result.unmatched_expected, [payout])
        self.assertEqual(result.unmatched_receipts, [receipt])
        self.assertEqual(result.exceptions[0].exception_type, "short_paid")

    def test_classifies_delayed_expected_payout(self):
        payout = ExpectedPayout(
            payout_id="payout-1",
            entity="Ergode Inc.",
            marketplace="Marketplace A",
            currency="USD",
            expected_date=date(2026, 5, 20),
            amount=Decimal("100.00"),
            reference="SETTLEMENT-001",
        )

        result = reconcile_payouts([payout], [], as_of_date=date(2026, 5, 24))

        self.assertEqual(result.exceptions[0].exception_type, "delayed")

    def test_classifies_pending_expected_payout(self):
        payout = ExpectedPayout(
            payout_id="payout-1",
            entity="Ergode Inc.",
            marketplace="Marketplace A",
            currency="USD",
            expected_date=date(2026, 5, 24),
            amount=Decimal("100.00"),
            reference="SETTLEMENT-001",
        )

        result = reconcile_payouts([payout], [], as_of_date=date(2026, 5, 24))

        self.assertEqual(result.exceptions[0].exception_type, "pending_receipt")

    def test_classifies_possible_reference_mismatch(self):
        payout = ExpectedPayout(
            payout_id="payout-1",
            entity="Ergode Inc.",
            marketplace="Marketplace A",
            currency="USD",
            expected_date=date(2026, 5, 20),
            amount=Decimal("100.00"),
            reference="SETTLEMENT-001",
        )
        receipt = BankReceipt(
            receipt_id="receipt-1",
            bank_account="Operating USD",
            currency="USD",
            receipt_date=date(2026, 5, 20),
            amount=Decimal("100.00"),
            reference="DIFFERENT-REF",
        )

        result = reconcile_payouts([payout], [receipt])

        self.assertEqual(result.exceptions[0].exception_type, "possible_reference_mismatch")

    def test_classifies_duplicate_receipt(self):
        payout = ExpectedPayout(
            payout_id="payout-1",
            entity="Ergode Inc.",
            marketplace="Marketplace A",
            currency="USD",
            expected_date=date(2026, 5, 20),
            amount=Decimal("100.00"),
            reference="SETTLEMENT-001",
        )
        receipts = [
            BankReceipt(
                receipt_id="receipt-1",
                bank_account="Operating USD",
                currency="USD",
                receipt_date=date(2026, 5, 20),
                amount=Decimal("100.00"),
                reference="SETTLEMENT-001",
            ),
            BankReceipt(
                receipt_id="receipt-2",
                bank_account="Operating USD",
                currency="USD",
                receipt_date=date(2026, 5, 20),
                amount=Decimal("100.00"),
                reference="SETTLEMENT-001",
            ),
        ]

        result = reconcile_payouts([payout], receipts)

        self.assertEqual(len(result.matched), 1)
        self.assertEqual(result.unmatched_receipts, [receipts[1]])
        self.assertEqual(result.exceptions[0].exception_type, "duplicate_receipt")


if __name__ == "__main__":
    unittest.main()
