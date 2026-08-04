"""Data quality checks for Phase 0 source files."""

import csv
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path


@dataclass(frozen=True)
class DataQualityIssue:
    source_file: str
    row_number: int
    record_type: str
    record_id: str
    field: str
    issue_code: str
    message: str


def validate_expected_payouts_csv(
    path: str | Path,
    *,
    supported_currencies: set[str],
) -> list[DataQualityIssue]:
    return _validate_csv(
        path,
        record_type="expected_payout",
        id_field="payout_id",
        date_field="expected_date",
        amount_field="amount",
        required_fields=[
            "payout_id",
            "entity",
            "marketplace",
            "currency",
            "expected_date",
            "amount",
        ],
        supported_currencies=supported_currencies,
    )


def validate_bank_receipts_csv(
    path: str | Path,
    *,
    supported_currencies: set[str],
) -> list[DataQualityIssue]:
    return _validate_csv(
        path,
        record_type="bank_receipt",
        id_field="receipt_id",
        date_field="receipt_date",
        amount_field="amount",
        required_fields=[
            "receipt_id",
            "bank_account",
            "currency",
            "receipt_date",
            "amount",
        ],
        supported_currencies=supported_currencies,
    )


def write_data_quality_report(
    path: str | Path,
    issues: list[DataQualityIssue],
) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "source_file",
                "row_number",
                "record_type",
                "record_id",
                "field",
                "issue_code",
                "message",
            ]
        )
        for issue in issues:
            writer.writerow(
                [
                    issue.source_file,
                    issue.row_number,
                    issue.record_type,
                    issue.record_id,
                    issue.field,
                    issue.issue_code,
                    issue.message,
                ]
            )


def _validate_csv(
    path: str | Path,
    *,
    record_type: str,
    id_field: str,
    date_field: str,
    amount_field: str,
    required_fields: list[str],
    supported_currencies: set[str],
) -> list[DataQualityIssue]:
    source_path = Path(path)
    rows = _read_csv(source_path)
    issues: list[DataQualityIssue] = []
    seen_ids: set[str] = set()

    for index, row in enumerate(rows, start=2):
        record_id = _value(row, id_field)
        for field in required_fields:
            if not _value(row, field):
                issues.append(
                    _issue(
                        source_path,
                        index,
                        record_type,
                        record_id,
                        field,
                        "missing_required_field",
                        f"Required field '{field}' is blank or missing.",
                    )
                )

        if record_id:
            if record_id in seen_ids:
                issues.append(
                    _issue(
                        source_path,
                        index,
                        record_type,
                        record_id,
                        id_field,
                        "duplicate_id",
                        f"Duplicate {id_field} '{record_id}'.",
                    )
                )
            seen_ids.add(record_id)

        raw_date = _value(row, date_field)
        if raw_date and not _is_iso_date(raw_date):
            issues.append(
                _issue(
                    source_path,
                    index,
                    record_type,
                    record_id,
                    date_field,
                    "invalid_date",
                    f"Date '{raw_date}' must use YYYY-MM-DD format.",
                )
            )

        raw_amount = _value(row, amount_field)
        if raw_amount and not _is_decimal(raw_amount):
            issues.append(
                _issue(
                    source_path,
                    index,
                    record_type,
                    record_id,
                    amount_field,
                    "invalid_amount",
                    f"Amount '{raw_amount}' is not a valid decimal.",
                )
            )

        currency = _value(row, "currency")
        if currency and currency not in supported_currencies:
            issues.append(
                _issue(
                    source_path,
                    index,
                    record_type,
                    record_id,
                    "currency",
                    "unsupported_currency",
                    f"Currency '{currency}' is not in supported currencies.",
                )
            )

        if "reference" in row and not _value(row, "reference"):
            issues.append(
                _issue(
                    source_path,
                    index,
                    record_type,
                    record_id,
                    "reference",
                    "blank_reference",
                    "Reference is blank; matching may require amount/date fallback.",
                )
            )

    return issues


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _issue(
    source_path: Path,
    row_number: int,
    record_type: str,
    record_id: str,
    field: str,
    issue_code: str,
    message: str,
) -> DataQualityIssue:
    return DataQualityIssue(
        source_file=source_path.name,
        row_number=row_number,
        record_type=record_type,
        record_id=record_id,
        field=field,
        issue_code=issue_code,
        message=message,
    )


def _value(row: dict[str, str], field: str) -> str:
    return (row.get(field) or "").strip()


def _is_iso_date(value: str) -> bool:
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _is_decimal(value: str) -> bool:
    try:
        Decimal(value)
    except InvalidOperation:
        return False
    return True
