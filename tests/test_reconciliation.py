from datetime import date
from decimal import Decimal
import unittest

from ai_cashflow import BankReceipt, ExpectedPayout, reconcile_payouts


class ReconciliationTests(unittest.TestCase):
    def test_reconciles_exact_reference_match(self):
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
            receipt_date=date(2026, 5, 21),
            amount=Decimal("100.00"),
            reference="SETTLEMENT-001",
        )

        result = reconcile_payouts([payout], [receipt])

        self.assertEqual(result.matched, [(payout, receipt)])
        self.assertEqual(result.unmatched_expected, [])
        self.assertEqual(result.unmatched_receipts, [])

    def test_flags_unmatched_expected_payout(self):
        payout = ExpectedPayout(
            payout_id="payout-1",
            entity="Ergode Inc.",
            marketplace="Marketplace A",
            currency="USD",
            expected_date=date(2026, 5, 20),
            amount=Decimal("100.00"),
        )

        result = reconcile_payouts([payout], [])

        self.assertEqual(result.matched, [])
        self.assertEqual(result.unmatched_expected, [payout])
        self.assertEqual(result.unmatched_receipts, [])

    def test_flags_unmatched_bank_receipt(self):
        receipt = BankReceipt(
            receipt_id="receipt-1",
            bank_account="Operating USD",
            currency="USD",
            receipt_date=date(2026, 5, 21),
            amount=Decimal("100.00"),
        )

        result = reconcile_payouts([], [receipt])

        self.assertEqual(result.matched, [])
        self.assertEqual(result.unmatched_expected, [])
        self.assertEqual(result.unmatched_receipts, [receipt])


if __name__ == "__main__":
    unittest.main()
