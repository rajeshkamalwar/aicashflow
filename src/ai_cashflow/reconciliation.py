"""Deterministic reconciliation helpers for Phase 0."""

from datetime import date
from decimal import Decimal

from .models import (
    BankReceipt,
    ExpectedPayout,
    ReconciliationException,
    ReconciliationResult,
)


def reconcile_payouts(
    expected_payouts: list[ExpectedPayout],
    bank_receipts: list[BankReceipt],
    *,
    date_tolerance_days: int = 3,
    amount_tolerance: Decimal = Decimal("0"),
    as_of_date: date | None = None,
) -> ReconciliationResult:
    """Match payouts and receipts and classify Phase 0 exceptions."""
    unmatched_receipts = list(bank_receipts)
    matched: list[tuple[ExpectedPayout, BankReceipt]] = []
    unmatched_expected: list[ExpectedPayout] = []
    exceptions: list[ReconciliationException] = []

    for payout in expected_payouts:
        match = _find_receipt_match(
            payout,
            unmatched_receipts,
            date_tolerance_days=date_tolerance_days,
            amount_tolerance=amount_tolerance,
        )
        if match is None:
            unmatched_expected.append(payout)
            exception = _classify_unmatched_expected(
                payout,
                unmatched_receipts,
                amount_tolerance=amount_tolerance,
                as_of_date=as_of_date,
            )
            exceptions.append(exception)
            continue

        matched.append((payout, match))
        unmatched_receipts.remove(match)

    exceptions.extend(_classify_unmatched_receipts(unmatched_receipts, matched))

    return ReconciliationResult(
        matched=matched,
        unmatched_expected=unmatched_expected,
        unmatched_receipts=unmatched_receipts,
        exceptions=exceptions,
    )


def _find_receipt_match(
    payout: ExpectedPayout,
    receipts: list[BankReceipt],
    *,
    date_tolerance_days: int,
    amount_tolerance: Decimal,
) -> BankReceipt | None:
    reference_matches = [
        receipt
        for receipt in receipts
        if payout.reference
        and receipt.reference
        and payout.reference == receipt.reference
        and payout.currency == receipt.currency
        and _within_amount_tolerance(payout.amount, receipt.amount, amount_tolerance)
        and _within_date_tolerance(
            payout.expected_date, receipt.receipt_date, date_tolerance_days
        )
    ]
    if reference_matches:
        return reference_matches[0]

    amount_matches = [
        receipt
        for receipt in receipts
        if payout.currency == receipt.currency
        and _within_amount_tolerance(payout.amount, receipt.amount, amount_tolerance)
        and _within_date_tolerance(
            payout.expected_date, receipt.receipt_date, date_tolerance_days
        )
        and not (
            payout.reference
            and receipt.reference
            and payout.reference != receipt.reference
        )
    ]
    return amount_matches[0] if amount_matches else None


def _classify_unmatched_expected(
    payout: ExpectedPayout,
    receipts: list[BankReceipt],
    *,
    amount_tolerance: Decimal,
    as_of_date: date | None,
) -> ReconciliationException:
    reference_candidates = [
        receipt
        for receipt in receipts
        if payout.reference
        and receipt.reference == payout.reference
        and payout.currency == receipt.currency
    ]
    if reference_candidates and any(
        receipt.amount < payout.amount - amount_tolerance
        for receipt in reference_candidates
    ):
        return ReconciliationException(
            exception_type="short_paid",
            payout=payout,
            receipt=reference_candidates[0],
            message="Receipt reference matches but amount is below expected payout.",
        )

    if any(
        receipt.currency == payout.currency
        and receipt.amount == payout.amount
        and payout.reference
        and receipt.reference
        and payout.reference != receipt.reference
        for receipt in receipts
    ):
        return ReconciliationException(
            exception_type="possible_reference_mismatch",
            payout=payout,
            message="Amount and currency match another receipt but reference differs.",
        )

    if as_of_date is not None and payout.expected_date < as_of_date:
        return ReconciliationException(
            exception_type="delayed",
            payout=payout,
            message="Expected payout date has passed without a matching receipt.",
        )

    return ReconciliationException(
        exception_type="pending_receipt",
        payout=payout,
        message="Expected payout is not yet matched to a receipt.",
    )


def _classify_unmatched_receipts(
    receipts: list[BankReceipt],
    matched: list[tuple[ExpectedPayout, BankReceipt]],
) -> list[ReconciliationException]:
    exceptions: list[ReconciliationException] = []
    matched_receipts = [receipt for _, receipt in matched]
    for receipt in receipts:
        exception_type = "unmatched_bank_receipt"
        message = "Bank/payment receipt has no matching expected payout."
        if any(
            receipt.reference
            and matched_receipt.reference == receipt.reference
            and matched_receipt.currency == receipt.currency
            and matched_receipt.amount == receipt.amount
            for matched_receipt in matched_receipts
        ):
            exception_type = "duplicate_receipt"
            message = "Receipt duplicates an already matched receipt."
        exceptions.append(
            ReconciliationException(
                exception_type=exception_type,
                receipt=receipt,
                message=message,
            )
        )
    return exceptions


def _within_amount_tolerance(
    expected: Decimal,
    actual: Decimal,
    amount_tolerance: Decimal,
) -> bool:
    return abs(expected - actual) <= amount_tolerance


def _within_date_tolerance(
    expected: date,
    actual: date,
    date_tolerance_days: int,
) -> bool:
    return abs((actual - expected).days) <= date_tolerance_days
