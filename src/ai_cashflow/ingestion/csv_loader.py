"""CSV loaders for normalized Phase 0 sample files."""

import csv
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from ai_cashflow.models import AmazonTransaction, BankReceipt, ExpectedPayout


@dataclass(frozen=True)
class ColumnMapping:
    columns: dict[str, str]


def load_expected_payouts_csv(path: str | Path) -> list[ExpectedPayout]:
    rows = _read_csv(path)
    return [
        ExpectedPayout(
            payout_id=row["payout_id"],
            entity=row["entity"],
            marketplace=row["marketplace"],
            currency=row["currency"],
            expected_date=date.fromisoformat(row["expected_date"]),
            amount=Decimal(row["amount"]),
            reference=_optional(row.get("reference")),
        )
        for row in rows
    ]


def load_mapped_expected_payouts_csv(
    path: str | Path,
    mapping: ColumnMapping,
) -> list[ExpectedPayout]:
    rows = _read_csv(path)
    _validate_required_columns(
        rows,
        mapping,
        ["payout_id", "entity", "marketplace", "currency", "expected_date", "amount"],
    )
    return [
        ExpectedPayout(
            payout_id=_mapped(row, mapping, "payout_id"),
            entity=_mapped(row, mapping, "entity"),
            marketplace=_mapped(row, mapping, "marketplace"),
            currency=_mapped(row, mapping, "currency"),
            expected_date=date.fromisoformat(_mapped(row, mapping, "expected_date")),
            amount=Decimal(_mapped(row, mapping, "amount")),
            reference=_optional(_mapped(row, mapping, "reference", required=False)),
        )
        for row in rows
    ]


def load_bank_receipts_csv(path: str | Path) -> list[BankReceipt]:
    rows = _read_csv(path)
    return [
        BankReceipt(
            receipt_id=row["receipt_id"],
            bank_account=row["bank_account"],
            currency=row["currency"],
            receipt_date=date.fromisoformat(row["receipt_date"]),
            amount=Decimal(row["amount"]),
            reference=_optional(row.get("reference")),
        )
        for row in rows
    ]


def load_amazon_transactions_csv(
    path: str | Path,
    *,
    currency: str = "USD",
    source_id: str = "",
) -> list[AmazonTransaction]:
    source_file = Path(path).name
    rows = _read_amazon_csv(path)
    return [
        AmazonTransaction(
            posted_at=row["date/time"],
            settlement_id=row["settlement id"],
            transaction_type=row["type"],
            order_id=row["order id"],
            sku=row["sku"],
            description=row["description"],
            marketplace=row["marketplace"],
            fulfillment=row["fulfillment"],
            currency=currency,
            product_sales=_decimal(row.get("product sales")),
            selling_fees=_decimal(row.get("selling fees")),
            fba_fees=_decimal(row.get("fba fees")),
            other_transaction_fees=_decimal(row.get("other transaction fees")),
            other=_decimal(row.get("other")),
            total=_decimal(row.get("total")),
            status=row["Transaction status"],
            release_date=_optional(row.get("Transaction Release Date")),
            source_file=source_file,
            source_id=source_id,
        )
        for row in rows
    ]


def load_mapped_bank_receipts_csv(
    path: str | Path,
    mapping: ColumnMapping,
) -> list[BankReceipt]:
    rows = _read_csv(path)
    _validate_required_columns(
        rows,
        mapping,
        ["receipt_id", "bank_account", "currency", "receipt_date", "amount"],
    )
    return [
        BankReceipt(
            receipt_id=_mapped(row, mapping, "receipt_id"),
            bank_account=_mapped(row, mapping, "bank_account"),
            currency=_mapped(row, mapping, "currency"),
            receipt_date=date.fromisoformat(_mapped(row, mapping, "receipt_date")),
            amount=Decimal(_mapped(row, mapping, "amount")),
            reference=_optional(_mapped(row, mapping, "reference", required=False)),
        )
        for row in rows
    ]


def _read_csv(path: str | Path) -> list[dict[str, str]]:
    with Path(path).open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _read_amazon_csv(path: str | Path) -> list[dict[str, str]]:
    with Path(path).open(newline="", encoding="utf-8-sig") as handle:
        lines = list(handle)

    header_index = next(
        (
            index
            for index, line in enumerate(lines)
            if "date/time" in line.lower() and "settlement id" in line.lower()
        ),
        None,
    )
    if header_index is None:
        raise ValueError("Amazon transaction CSV header was not found.")

    return list(csv.DictReader(lines[header_index:]))


def _optional(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _validate_required_columns(
    rows: list[dict[str, str]],
    mapping: ColumnMapping,
    required_fields: list[str],
) -> None:
    if not rows:
        return
    source_columns = set(rows[0].keys())
    missing = [
        mapping.columns[field]
        for field in required_fields
        if field not in mapping.columns or mapping.columns[field] not in source_columns
    ]
    if missing:
        raise ValueError(
            "Missing required source columns: " + ", ".join(sorted(missing))
        )


def _mapped(
    row: dict[str, str],
    mapping: ColumnMapping,
    field: str,
    *,
    required: bool = True,
) -> str | None:
    source_column = mapping.columns.get(field)
    if source_column is None:
        if required:
            raise ValueError(f"Missing mapping for required field: {field}")
        return None
    return row.get(source_column)


def _decimal(value: str | None) -> Decimal:
    if value is None:
        return Decimal("0")
    normalized = value.replace("$", "").replace(",", "").strip()
    if not normalized:
        return Decimal("0")
    try:
        return Decimal(normalized)
    except InvalidOperation:
        return Decimal("0")


# ---------------------------------------------------------------------------
# Amazon Flat File V2 (tab-separated settlement report from Seller Central)
# ---------------------------------------------------------------------------

def _amazon_flat_file_header_index(path: str | Path) -> int | None:
    try:
        with Path(path).open(newline="", encoding="utf-8-sig", errors="replace") as fh:
            for index in range(20):
                line = fh.readline().lower()
                if not line:
                    break
                if "settlement-id" in line and "settlement-start-date" in line:
                    return index
    except Exception:
        pass
    return None


def is_amazon_flat_file_v2(path: str | Path) -> bool:
    """Return True if the file looks like an Amazon Flat File V2 settlement report."""
    return _amazon_flat_file_header_index(path) is not None


def _parse_amz_date(value: str) -> date:
    """Parse dates in 'DD.MM.YYYY ...' or 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM:SS UTC' format."""
    v = (value or "").strip()
    # DD.MM.YYYY HH:MM:SS UTC
    m = re.match(r"(\d{2})\.(\d{2})\.(\d{4})", v)
    if m:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    # YYYY-MM-DD
    m2 = re.match(r"(\d{4})-(\d{2})-(\d{2})", v)
    if m2:
        return date(int(m2.group(1)), int(m2.group(2)), int(m2.group(3)))
    raise ValueError(f"Cannot parse date: {v!r}")


def load_amazon_flat_file_v2(
    path: str | Path,
    *,
    entity: str = "Unknown",
    source_id: str = "",
) -> tuple[list[ExpectedPayout], list[AmazonTransaction]]:
    """
    Parse an Amazon Flat File V2 settlement report (tab-separated .txt).

    Returns:
        (expected_payouts, transactions)
        - expected_payouts: one entry per settlement (the deposit)
        - transactions: one entry per transaction line item
    """
    source_file = Path(path).name

    header_index = _amazon_flat_file_header_index(path) or 0
    with Path(path).open(newline="", encoding="utf-8-sig", errors="replace") as fh:
        for _ in range(header_index):
            fh.readline()
        reader = csv.DictReader(fh, delimiter="\t")
        rows = list(reader)

    if not rows:
        return [], []

    # First row is the settlement summary (has total-amount, deposit-date)
    summary = rows[0]
    settlement_id = summary.get("settlement-id", "").strip()
    currency = summary.get("currency", "CAD").strip() or "CAD"
    total_amount_str = summary.get("total-amount", "").strip()
    deposit_date_str = summary.get("deposit-date", "").strip()
    marketplace = next(
        (
            row.get("marketplace-name", "").strip()
            for row in rows
            if row.get("marketplace-name", "").strip()
        ),
        "Amazon",
    )

    payouts: list[ExpectedPayout] = []
    if settlement_id and total_amount_str and deposit_date_str:
        try:
            payouts.append(ExpectedPayout(
                payout_id=settlement_id,
                entity=entity,
                marketplace=marketplace,
                currency=currency,
                expected_date=_parse_amz_date(deposit_date_str),
                amount=_decimal(total_amount_str),
                reference=f"Amazon settlement {settlement_id}",
                source_id=source_id,
            ))
        except Exception:
            pass

    # Remaining rows are individual transaction line items
    transactions: list[AmazonTransaction] = []
    for row in rows[1:]:
        txn_type = row.get("transaction-type", "").strip()
        if not txn_type:
            continue

        order_id = row.get("order-id", "").strip()
        sku = row.get("sku", "").strip()
        amount_type = row.get("amount-type", "").strip()
        amount_desc = row.get("amount-description", "").strip()
        amount_val = _decimal(row.get("amount", ""))
        mkt = row.get("marketplace-name", marketplace).strip() or marketplace
        posted = row.get("posted-date-time", row.get("posted-date", "")).strip()

        # Categorise amount into financial buckets
        at = amount_type.lower()
        ad = amount_desc.lower()
        product_sales = amount_val if at == "itemprice" and ad == "principal" else Decimal("0")
        selling_fees = amount_val if at == "itemfees" and "commission" in ad else Decimal("0")
        fba_fees = amount_val if at == "itemfees" and "fba" in ad else Decimal("0")
        other_fees = (
            amount_val
            if at == "itemfees" and "commission" not in ad and "fba" not in ad
            else Decimal("0")
        )
        other = amount_val if at not in ("itemprice", "itemfees") else Decimal("0")
        total = amount_val

        transactions.append(AmazonTransaction(
            posted_at=posted,
            settlement_id=settlement_id,
            transaction_type=txn_type,
            order_id=order_id,
            sku=sku,
            description=f"{amount_type}: {amount_desc}",
            marketplace=mkt,
            fulfillment=row.get("fulfillment-id", "").strip(),
            currency=currency,
            product_sales=product_sales,
            selling_fees=selling_fees,
            fba_fees=fba_fees,
            other_transaction_fees=other_fees,
            other=other,
            total=total,
            status="settled",
            release_date=None,
            source_file=source_file,
            source_id=source_id,
        ))

    return payouts, transactions
