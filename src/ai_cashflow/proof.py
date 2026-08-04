"""Cashflow proof calculations shared by API and scheduled reconciliation."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from ai_cashflow.models import BankReceipt, ExpectedPayout
from ai_cashflow.reconciliation import reconcile_payouts


MONEY_QUANTUM = Decimal("0.01")
AMAZON_EVIDENCE_LAG = timedelta(hours=48)


def evaluate_amazon_proof(
    group: dict[str, object],
    transactions: list[dict[str, object]],
    *,
    settlement_id: str | None,
    settlement_total: Decimal | None,
    tolerance: Decimal,
    now: datetime,
) -> dict[str, object]:
    """Compare one Amazon payment group with Finances and settlement evidence."""
    payment_group_id = str(group.get("FinancialEventGroupId", "")).strip()
    if not payment_group_id:
        raise ValueError("Amazon financial event group ID is missing.")
    payment_amount, currency = _money(
        group.get("OriginalTotal"), "financial event group total"
    )
    payment_time = _payment_time(group)
    result: dict[str, object] = {
        "payment_group_id": payment_group_id,
        "settlement_id": settlement_id,
        "currency": currency,
        "amazon_payment_amount": _format_money(payment_amount),
        "finances_total": None,
        "settlement_total": (
            _format_money(settlement_total) if settlement_total is not None else None
        ),
        "amazon_variance": None,
        "payment_date": payment_time.date().isoformat(),
        "bank_receipt_id": None,
        "bank_received_amount": None,
        "bank_variance": None,
    }
    if str(group.get("ProcessingStatus", "")).strip().lower() != "closed":
        result["status"] = "Pending"
        return result

    finances_total = Decimal("0")
    for transaction in transactions:
        amount, transaction_currency = _money(
            transaction.get("totalAmount"), "finance transaction total"
        )
        if transaction_currency != currency:
            raise ValueError(
                f"Finance transaction currency {transaction_currency} does not match {currency}."
            )
        finances_total += amount
    result["finances_total"] = _format_money(finances_total)

    if not transactions or settlement_total is None:
        checked_at = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
        result["status"] = (
            "Pending"
            if checked_at.astimezone(timezone.utc) - payment_time < AMAZON_EVIDENCE_LAG
            else "Failed"
        )
        return result

    variance = max(
        abs(payment_amount - finances_total),
        abs(payment_amount - settlement_total),
    )
    result["amazon_variance"] = _format_money(variance)
    result["status"] = (
        "Amazon verified" if variance <= tolerance else "Variance"
    )
    return result


def match_bank_receipts(
    proofs: list[dict[str, object]],
    receipts: list[BankReceipt],
    *,
    date_tolerance_days: int,
    amount_tolerance: Decimal,
) -> list[dict[str, object]]:
    """Apply the existing payout reconciliation rules to Amazon-verified proofs."""
    eligible = [
        proof for proof in proofs if proof.get("status") == "Amazon verified"
    ]
    payouts = [
        ExpectedPayout(
            payout_id=str(proof["payment_group_id"]),
            entity="Amazon",
            marketplace="Amazon",
            currency=str(proof["currency"]),
            expected_date=date.fromisoformat(str(proof["payment_date"])[:10]),
            amount=Decimal(str(proof["amazon_payment_amount"])),
            reference=str(proof["payment_group_id"]),
            source_id=str(proof.get("source_id", "")),
        )
        for proof in eligible
    ]
    reconciliation = reconcile_payouts(
        payouts,
        receipts,
        date_tolerance_days=date_tolerance_days,
        amount_tolerance=amount_tolerance,
    )
    matches = {
        payout.payout_id: receipt
        for payout, receipt in reconciliation.matched
    }
    updated = []
    for proof in proofs:
        row = dict(proof)
        receipt = matches.get(str(row.get("payment_group_id", "")))
        if receipt is not None:
            expected = Decimal(str(row["amazon_payment_amount"]))
            row.update({
                "status": "Verified",
                "bank_receipt_id": receipt.receipt_id,
                "bank_received_amount": _format_money(receipt.amount),
                "bank_variance": _format_money(abs(expected - receipt.amount)),
                "last_error": None,
            })
        elif row.get("status") == "Amazon verified":
            row["last_error"] = (
                "No bank receipt matched reference, currency, amount and date tolerances."
            )
        updated.append(row)
    return updated


def _money(value: object, label: str) -> tuple[Decimal, str]:
    if not isinstance(value, dict):
        raise ValueError(f"Amazon {label} is missing.")
    amount_value = value.get("CurrencyAmount", value.get("currencyAmount"))
    currency = str(
        value.get("CurrencyCode", value.get("currencyCode", ""))
    ).strip().upper()
    if amount_value is None or not currency:
        raise ValueError(f"Amazon {label} is incomplete.")
    try:
        return Decimal(str(amount_value)), currency
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"Amazon {label} amount is invalid.") from exc


def _payment_time(group: dict[str, object]) -> datetime:
    value = (
        group.get("FundTransferDate")
        or group.get("FinancialEventGroupEnd")
        or group.get("FinancialEventGroupStart")
    )
    if not value:
        raise ValueError("Amazon payment date is missing.")
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return (
        parsed.replace(tzinfo=timezone.utc)
        if parsed.tzinfo is None
        else parsed.astimezone(timezone.utc)
    )


def _format_money(value: Decimal) -> str:
    return str(value.quantize(MONEY_QUANTUM))
