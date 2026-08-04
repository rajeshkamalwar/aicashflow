from pathlib import Path
import tempfile
import unittest

from ai_cashflow.ingestion import (
    ColumnMapping,
    load_mapped_bank_receipts_csv,
    load_mapped_expected_payouts_csv,
)


class MappedCsvLoaderTests(unittest.TestCase):
    def test_loads_payouts_from_marketplace_specific_columns(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "marketplace_export.csv"
            path.write_text(
                "\n".join(
                    [
                        "Settlement ID,Company,Channel,Settle Currency,Deposit Date,Net Payout,Ref",
                        "P-001,Ergode Inc.,Marketplace X,USD,2026-05-20,123.45,ABC-001",
                    ]
                ),
                encoding="utf-8",
            )

            payouts = load_mapped_expected_payouts_csv(
                path,
                ColumnMapping(
                    {
                        "payout_id": "Settlement ID",
                        "entity": "Company",
                        "marketplace": "Channel",
                        "currency": "Settle Currency",
                        "expected_date": "Deposit Date",
                        "amount": "Net Payout",
                        "reference": "Ref",
                    }
                ),
            )

            self.assertEqual(len(payouts), 1)
            self.assertEqual(payouts[0].payout_id, "P-001")
            self.assertEqual(payouts[0].marketplace, "Marketplace X")
            self.assertEqual(str(payouts[0].amount), "123.45")

    def test_loads_bank_receipts_from_bank_specific_columns(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "bank_export.csv"
            path.write_text(
                "\n".join(
                    [
                        "Txn ID,Account,CCY,Post Date,Credit Amount,Memo",
                        "B-001,BOA Operating,USD,2026-05-21,123.45,ABC-001",
                    ]
                ),
                encoding="utf-8",
            )

            receipts = load_mapped_bank_receipts_csv(
                path,
                ColumnMapping(
                    {
                        "receipt_id": "Txn ID",
                        "bank_account": "Account",
                        "currency": "CCY",
                        "receipt_date": "Post Date",
                        "amount": "Credit Amount",
                        "reference": "Memo",
                    }
                ),
            )

            self.assertEqual(len(receipts), 1)
            self.assertEqual(receipts[0].receipt_id, "B-001")
            self.assertEqual(receipts[0].bank_account, "BOA Operating")
            self.assertEqual(str(receipts[0].amount), "123.45")

    def test_missing_required_mapping_column_raises_clear_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "bad_export.csv"
            path.write_text("Settlement ID,Company\nP-001,Ergode Inc.\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "Missing required source columns"):
                load_mapped_expected_payouts_csv(
                    path,
                    ColumnMapping(
                        {
                            "payout_id": "Settlement ID",
                            "entity": "Company",
                            "marketplace": "Channel",
                            "currency": "Settle Currency",
                            "expected_date": "Deposit Date",
                            "amount": "Net Payout",
                        }
                    ),
                )


if __name__ == "__main__":
    unittest.main()
