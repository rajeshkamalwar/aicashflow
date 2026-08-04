from pathlib import Path
import tempfile
import unittest

from ai_cashflow.data_quality import (
    validate_bank_receipts_csv,
    validate_expected_payouts_csv,
    write_data_quality_report,
)


class DataQualityTests(unittest.TestCase):
    def test_validates_expected_payout_quality_issues(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "bad_payouts.csv"
            path.write_text(
                "\n".join(
                    [
                        "payout_id,entity,marketplace,currency,expected_date,amount,reference",
                        "P-001,Ergode Inc.,Marketplace A,USD,2026-05-20,100.00,REF-001",
                        "P-001,Ergode Inc.,Marketplace A,USD,bad-date,abc,",
                        ",Ergode Inc.,Marketplace A,GBP,2026-05-22,50.00,REF-003",
                    ]
                ),
                encoding="utf-8",
            )

            issues = validate_expected_payouts_csv(path, supported_currencies={"USD"})
            issue_codes = [issue.issue_code for issue in issues]

            self.assertIn("duplicate_id", issue_codes)
            self.assertIn("invalid_date", issue_codes)
            self.assertIn("invalid_amount", issue_codes)
            self.assertIn("blank_reference", issue_codes)
            self.assertIn("missing_required_field", issue_codes)
            self.assertIn("unsupported_currency", issue_codes)
            self.assertTrue(all(issue.source_file == "bad_payouts.csv" for issue in issues))

    def test_validates_bank_receipt_quality_issues(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "bad_receipts.csv"
            path.write_text(
                "\n".join(
                    [
                        "receipt_id,bank_account,currency,receipt_date,amount,reference",
                        "R-001,BOA Operating,USD,2026-05-20,100.00,REF-001",
                        "R-001,BOA Operating,EUR,not-a-date,not-money,",
                    ]
                ),
                encoding="utf-8",
            )

            issues = validate_bank_receipts_csv(path, supported_currencies={"USD"})
            issue_codes = [issue.issue_code for issue in issues]

            self.assertIn("duplicate_id", issue_codes)
            self.assertIn("invalid_date", issue_codes)
            self.assertIn("invalid_amount", issue_codes)
            self.assertIn("blank_reference", issue_codes)
            self.assertIn("unsupported_currency", issue_codes)

    def test_writes_data_quality_report(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "bad_payouts.csv"
            source.write_text(
                "\n".join(
                    [
                        "payout_id,entity,marketplace,currency,expected_date,amount,reference",
                        "P-001,Ergode Inc.,Marketplace A,USD,bad-date,100.00,REF-001",
                    ]
                ),
                encoding="utf-8",
            )
            output = Path(temp_dir) / "data_quality_report.csv"

            issues = validate_expected_payouts_csv(source, supported_currencies={"USD"})
            write_data_quality_report(output, issues)

            report = output.read_text(encoding="utf-8")
            self.assertIn("source_file,row_number,record_type,record_id,field,issue_code,message", report)
            self.assertIn("invalid_date", report)


if __name__ == "__main__":
    unittest.main()
