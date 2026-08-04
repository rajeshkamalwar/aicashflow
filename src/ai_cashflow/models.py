"""Normalized Phase 0 data models."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass(frozen=True)
class ExpectedPayout:
    payout_id: str
    entity: str
    marketplace: str
    currency: str
    expected_date: date
    amount: Decimal
    reference: str | None = None
    source_id: str = ""


@dataclass(frozen=True)
class BankReceipt:
    receipt_id: str
    bank_account: str
    currency: str
    receipt_date: date
    amount: Decimal
    reference: str | None = None


@dataclass(frozen=True)
class AmazonTransaction:
    posted_at: str
    settlement_id: str
    transaction_type: str
    order_id: str
    sku: str
    description: str
    marketplace: str
    fulfillment: str
    currency: str
    product_sales: Decimal
    selling_fees: Decimal
    fba_fees: Decimal
    other_transaction_fees: Decimal
    other: Decimal
    total: Decimal
    status: str
    release_date: str | None = None
    source_file: str = ""
    source_id: str = ""


@dataclass(frozen=True)
class ReconciliationResult:
    matched: list[tuple[ExpectedPayout, BankReceipt]]
    unmatched_expected: list[ExpectedPayout]
    unmatched_receipts: list[BankReceipt]
    exceptions: list["ReconciliationException"]


@dataclass(frozen=True)
class ReconciliationException:
    exception_type: str
    payout: ExpectedPayout | None = None
    receipt: BankReceipt | None = None
    message: str = ""
