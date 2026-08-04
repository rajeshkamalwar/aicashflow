"""Cashflow reconciliation report generation."""

import csv
import tempfile
from dataclasses import dataclass, replace
from decimal import Decimal, ROUND_HALF_UP
from html import escape
from pathlib import Path

from ai_cashflow.data_quality import (
    DataQualityIssue,
    validate_bank_receipts_csv,
    validate_expected_payouts_csv,
    write_data_quality_report,
)
from ai_cashflow.ingestion import (
    load_amazon_transactions_csv,
    load_bank_receipts_csv,
    load_expected_payouts_csv,
)
from ai_cashflow.ingestion.csv_loader import (
    is_amazon_flat_file_v2,
    load_amazon_flat_file_v2,
)
from ai_cashflow.models import (
    AmazonTransaction,
    BankReceipt,
    ExpectedPayout,
    ReconciliationException,
    ReconciliationResult,
)
from ai_cashflow.reconciliation import reconcile_payouts


@dataclass(frozen=True)
class SourceFiles:
    payout_files: list[Path]
    receipt_files: list[Path]
    amazon_transaction_files: list[Path]
    flat_file_v2_files: list[Path] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.flat_file_v2_files is None:
            object.__setattr__(self, "flat_file_v2_files", [])


def discover_source_files(
    samples_dir: str | Path,
    *,
    active_amazon_source_ids: set[str] | None = None,
) -> SourceFiles:
    root = Path(samples_dir)
    # Include both .csv and .txt (Amazon Flat File V2 downloads as .txt)
    marketplace_files: list[Path] = []
    for pattern in ("marketplaces/**/*.csv", "marketplaces/**/*.txt",
                    "manual_uploads/**/*.csv", "manual_uploads/**/*.txt"):
        marketplace_files.extend(sorted(root.glob(pattern)))

    payout_files: list[Path] = []
    amazon_transaction_files: list[Path] = []
    flat_file_v2_files: list[Path] = []

    for path in marketplace_files:
        if (
            active_amazon_source_ids is not None
            and path.name.startswith("amazon_sp_api_")
            and path.parent.name not in active_amazon_source_ids
        ):
            continue
        if is_amazon_flat_file_v2(path):
            flat_file_v2_files.append(path)
        elif _is_amazon_transaction_report(path):
            amazon_transaction_files.append(path)
        else:
            payout_files.append(path)

    receipt_files = sorted(root.glob("banks/**/*.csv"))
    receipt_files.extend(sorted(root.glob("banks/**/*.txt")))

    return SourceFiles(
        payout_files=payout_files,
        receipt_files=receipt_files,
        amazon_transaction_files=amazon_transaction_files,
        flat_file_v2_files=flat_file_v2_files,
    )


def generate_phase0_reports(
    samples_dir: str | Path,
    output_dir: str | Path,
    entity: str = "Unknown",
    usd_exchange_rates: dict[str, Decimal] | None = None,
    active_amazon_source_ids: set[str] | None = None,
) -> Path:
    samples_root = Path(samples_dir)
    output_root = Path(output_dir)
    output_root.mkdir(parents=True, exist_ok=True)

    source_files = discover_source_files(
        samples_root,
        active_amazon_source_ids=active_amazon_source_ids,
    )
    quality_issues = _validate_source_files(source_files)
    payouts = _load_payouts(source_files.payout_files)
    receipts = _load_receipts(source_files.receipt_files)
    amazon_transactions = _load_amazon_transactions(
        source_files.amazon_transaction_files
    )
    # Merge Amazon Flat File V2 data
    ffv2_payouts, ffv2_transactions = _load_flat_file_v2(source_files.flat_file_v2_files, entity=entity)
    payouts = list(payouts) + ffv2_payouts
    amazon_transactions = list(amazon_transactions) + ffv2_transactions
    rates = usd_exchange_rates or {"USD": Decimal("1")}
    payouts = [_payout_to_usd(row, rates) for row in payouts]
    receipts = [_receipt_to_usd(row, rates) for row in receipts]
    amazon_transactions = [
        _transaction_to_usd(row, rates) for row in amazon_transactions
    ]
    result = reconcile_payouts(payouts, receipts)

    write_data_quality_report(output_root / "data_quality_report.csv", quality_issues)
    _write_matched(output_root / "matched_payouts.csv", result.matched)
    _write_unmatched_expected(
        output_root / "unmatched_expected_payouts.csv", result.unmatched_expected
    )
    _write_unmatched_receipts(
        output_root / "unmatched_bank_receipts.csv", result.unmatched_receipts
    )
    _write_exception_summary(output_root / "exception_summary.csv", result.exceptions)
    _write_marketplace_activity(
        output_root / "marketplace_activity.csv", amazon_transactions
    )
    _write_source_evidence(
        output_root / "source_evidence.csv",
        source_files=source_files,
        payouts=payouts,
        receipts=receipts,
        amazon_transactions=amazon_transactions,
    )
    _write_cfo_summary(
        output_root / "cfo_summary.md",
        source_files=source_files,
        payouts=payouts,
        receipts=receipts,
        amazon_transactions=amazon_transactions,
        result=result,
        quality_issues=quality_issues,
    )
    _write_cfo_dashboard(
        output_root / "cfo_dashboard.html",
        source_files=source_files,
        payouts=payouts,
        receipts=receipts,
        amazon_transactions=amazon_transactions,
        result=result,
        quality_issues=quality_issues,
    )
    return output_root


def _validate_source_files(source_files: SourceFiles) -> list[DataQualityIssue]:
    issues: list[DataQualityIssue] = []
    supported_currencies = {"USD"}
    for path in source_files.payout_files:
        issues.extend(
            validate_expected_payouts_csv(
                path, supported_currencies=supported_currencies
            )
        )
    for path in source_files.receipt_files:
        issues.extend(
            validate_bank_receipts_csv(path, supported_currencies=supported_currencies)
        )
    return issues


def _load_payouts(paths: list[Path]) -> list[ExpectedPayout]:
    payouts: list[ExpectedPayout] = []
    for path in paths:
        payouts.extend(load_expected_payouts_csv(path))
    return payouts


def _load_receipts(paths: list[Path]) -> list[BankReceipt]:
    receipts: list[BankReceipt] = []
    for path in paths:
        receipts.extend(load_bank_receipts_csv(path))
    return receipts


def _load_amazon_transactions(paths: list[Path]) -> list[AmazonTransaction]:
    transactions: list[AmazonTransaction] = []
    for path in paths:
        transactions.extend(
            load_amazon_transactions_csv(path, source_id=_source_id_from_path(path))
        )
    return transactions


def _load_flat_file_v2(
    paths: list[Path],
    entity: str = "Unknown",
) -> tuple[list[ExpectedPayout], list[AmazonTransaction]]:
    all_payouts: list[ExpectedPayout] = []
    all_transactions: list[AmazonTransaction] = []
    for path in paths:
        payouts, transactions = load_amazon_flat_file_v2(
            path,
            entity=entity,
            source_id=_source_id_from_path(path),
        )
        all_payouts.extend(payouts)
        all_transactions.extend(transactions)
    return all_payouts, all_transactions


def _usd(amount: Decimal, currency: str, rates: dict[str, Decimal]) -> Decimal:
    code = currency.strip().upper()
    rate = rates.get(code)
    if rate is None or rate <= 0:
        raise ValueError(f"Missing positive USD exchange rate for {code or 'blank currency'}.")
    return (amount * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _payout_to_usd(
    row: ExpectedPayout, rates: dict[str, Decimal]
) -> ExpectedPayout:
    return replace(row, currency="USD", amount=_usd(row.amount, row.currency, rates))


def _receipt_to_usd(row: BankReceipt, rates: dict[str, Decimal]) -> BankReceipt:
    return replace(row, currency="USD", amount=_usd(row.amount, row.currency, rates))


def _transaction_to_usd(
    row: AmazonTransaction, rates: dict[str, Decimal]
) -> AmazonTransaction:
    fields = (
        "product_sales",
        "selling_fees",
        "fba_fees",
        "other_transaction_fees",
        "other",
        "total",
    )
    return replace(
        row,
        currency="USD",
        **{field: _usd(getattr(row, field), row.currency, rates) for field in fields},
    )


def _source_id_from_path(path: Path) -> str:
    parts = list(path.parts)
    lowered = [part.lower() for part in parts]
    for index in range(len(parts) - 1):
        if lowered[index] == "api":
            return parts[index + 1]
    return ""


def _write_matched(
    path: Path,
    matched: list[tuple[ExpectedPayout, BankReceipt]],
) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "payout_id",
                "source_id",
                "receipt_id",
                "entity",
                "marketplace",
                "bank_account",
                "currency",
                "amount",
                "expected_date",
                "receipt_date",
                "reference",
            ]
        )
        for payout, receipt in matched:
            writer.writerow(
                [
                    payout.payout_id,
                    payout.source_id,
                    receipt.receipt_id,
                    payout.entity,
                    payout.marketplace,
                    receipt.bank_account,
                    payout.currency,
                    _money(payout.amount),
                    payout.expected_date.isoformat(),
                    receipt.receipt_date.isoformat(),
                    payout.reference or receipt.reference or "",
                ]
            )


def _write_unmatched_expected(path: Path, payouts: list[ExpectedPayout]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "payout_id",
                "source_id",
                "entity",
                "marketplace",
                "currency",
                "expected_date",
                "amount",
                "reference",
                "exception_type",
            ]
        )
        for payout in payouts:
            exception_type = _exception_type_for_payout(payout)
            writer.writerow(
                [
                    payout.payout_id,
                    payout.source_id,
                    payout.entity,
                    payout.marketplace,
                    payout.currency,
                    payout.expected_date.isoformat(),
                    _money(payout.amount),
                    payout.reference or "",
                    exception_type,
                ]
            )


def _write_unmatched_receipts(path: Path, receipts: list[BankReceipt]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "receipt_id",
                "bank_account",
                "currency",
                "receipt_date",
                "amount",
                "reference",
                "exception_type",
            ]
        )
        for receipt in receipts:
            exception_type = _exception_type_for_receipt(receipt)
            writer.writerow(
                [
                    receipt.receipt_id,
                    receipt.bank_account,
                    receipt.currency,
                    receipt.receipt_date.isoformat(),
                    _money(receipt.amount),
                    receipt.reference or "",
                    exception_type,
                ]
            )


def _write_marketplace_activity(
    path: Path, transactions: list[AmazonTransaction]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", newline="", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        temporary = Path(handle.name)
        writer = csv.writer(handle)
        writer.writerow(
            [
                "posted_at",
                "source_id",
                "settlement_id",
                "type",
                "marketplace",
                "currency",
                "total",
                "status",
                "release_date",
                "order_id",
                "source_file",
            ]
        )
        for transaction in transactions:
            writer.writerow(
                [
                    transaction.posted_at,
                    transaction.source_id,
                    transaction.settlement_id,
                    transaction.transaction_type,
                    transaction.marketplace,
                    transaction.currency,
                    _money(transaction.total),
                    transaction.status,
                    transaction.release_date or "",
                    transaction.order_id,
                    transaction.source_file,
                ]
            )
    temporary.replace(path)


def _write_source_evidence(
    path: Path,
    *,
    source_files: SourceFiles,
    payouts: list[ExpectedPayout],
    receipts: list[BankReceipt],
    amazon_transactions: list[AmazonTransaction],
) -> None:
    rows = []
    amazon_files = sorted({t.source_file for t in amazon_transactions if t.source_file})
    rows.append(
        {
            "source": "Amazon Payments transaction report",
            "file": ", ".join(amazon_files) or "Missing",
            "status": "Loaded" if amazon_transactions else "Missing",
            "records": str(len(amazon_transactions)),
            "control_requirement": "Marketplace payout tracking, fees, returns, deferred/released activity",
            "limitation": "Transaction activity only; not confirmed bank cash and not a settlement payout deposit.",
        }
    )
    settlement_files = list(source_files.payout_files) + list(source_files.flat_file_v2_files)
    rows.append(
        {
            "source": "Marketplace settlement payout report",
            "file": ", ".join(p.name for p in settlement_files) or "Missing",
            "status": "Loaded" if payouts else "Missing",
            "records": str(len(payouts)),
            "control_requirement": "Expected payout amount, settlement date, settlement reference",
            "limitation": "Required to convert transaction activity into expected payout records.",
        }
    )
    rows.append(
        {
            "source": "Bank/payment receipt feed",
            "file": ", ".join(path.name for path in source_files.receipt_files) or "Missing",
            "status": "Loaded" if receipts else "Missing",
            "records": str(len(receipts)),
            "control_requirement": "Bank receipt reconciliation and confirmed cash position",
            "limitation": "Without this, cash balance and reconciliation remain pending.",
        }
    )
    rows.extend(
        [
            {
                "source": "Reserve / hold report",
                "file": "Missing",
                "status": "Missing",
                "records": "0",
                "control_requirement": "Reserve / hold tracking",
                "limitation": "Current deferred status is from the transaction report only, not a full reserve ledger.",
            },
            {
                "source": "Planned outflow amounts",
                "file": "Missing",
                "status": "Missing",
                "records": "0",
                "control_requirement": "Vendor payments, payroll, taxes, debt/card obligations",
                "limitation": "Funding gap forecast cannot be final until committed outflow amounts are supplied.",
            },
            {
                "source": "Additional entity and marketplace feeds",
                "file": "Missing",
                "status": "Limited coverage",
                "records": "0",
                "control_requirement": "Additional marketplace and entity feeds",
                "limitation": "Current evidence covers one Amazon seller account only.",
            },
        ]
    )
    with path.open("w", newline="", encoding="utf-8") as handle:
        fieldnames = [
            "source",
            "file",
            "status",
            "records",
            "control_requirement",
            "limitation",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _write_exception_summary(
    path: Path,
    exceptions: list[ReconciliationException],
) -> None:
    counts: dict[str, int] = {}
    for exception in exceptions:
        counts[exception.exception_type] = counts.get(exception.exception_type, 0) + 1

    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["exception_type", "count"])
        for exception_type, count in sorted(counts.items()):
            writer.writerow([exception_type, count])


def _write_cfo_summary(
    path: Path,
    *,
    source_files: SourceFiles,
    payouts: list[ExpectedPayout],
    receipts: list[BankReceipt],
    amazon_transactions: list[AmazonTransaction],
    result: ReconciliationResult,
    quality_issues: list[DataQualityIssue],
) -> None:
    total_expected = sum((p.amount for p in payouts), Decimal("0"))
    total_receipts = sum((r.amount for r in receipts), Decimal("0"))
    matched_total = sum((p.amount for p, _ in result.matched), Decimal("0"))
    unmatched_expected_total = sum(
        (p.amount for p in result.unmatched_expected), Decimal("0")
    )
    unmatched_receipt_total = sum(
        (r.amount for r in result.unmatched_receipts), Decimal("0")
    )
    marketplace_metrics = _marketplace_metrics(amazon_transactions)
    exception_counts = _exception_counts(result.exceptions)

    lines = [
        "# CFO Cashflow Reconciliation Summary",
        "",
        "## Scope",
        "",
        f"- Marketplace sample files: {len(source_files.payout_files) + len(source_files.amazon_transaction_files) + len(source_files.flat_file_v2_files)}",
        f"- Marketplace transaction report files: {len(source_files.amazon_transaction_files) + len(source_files.flat_file_v2_files)}",
        f"- Bank/payment sample files: {len(source_files.receipt_files)}",
        f"- Expected payout records: {len(payouts)}",
        f"- Marketplace transaction records: {len(amazon_transactions)}",
        f"- Bank/payment receipt records: {len(receipts)}",
        f"- Data quality issues: {len(quality_issues)}",
        "",
        "## Reconciliation Results",
        "",
        f"- Matched payouts: {len(result.matched)}",
        f"- Unmatched expected payouts: {len(result.unmatched_expected)}",
        f"- Unmatched bank/payment receipts: {len(result.unmatched_receipts)}",
        f"- Exception types: {_format_exception_counts(exception_counts)}",
        "",
        "## Amount Summary",
        "",
        f"- Total expected payouts: {_money(total_expected)}",
        f"- Marketplace net activity: {_money(marketplace_metrics['net'])}",
        f"- Marketplace order amount: {_money(marketplace_metrics['orders'])}",
        f"- Marketplace refund amount: {_money(marketplace_metrics['refunds'])}",
        f"- Marketplace tax amount: {_money(marketplace_metrics['tax'])}",
        f"- Marketplace selling fees: {_money(marketplace_metrics['selling_fees'])}",
        f"- Marketplace FBA fees: {_money(marketplace_metrics['fba_fees'])}",
        f"- Marketplace other fees: {_money(marketplace_metrics['other_fees'])}",
        f"- Marketplace reimbursements: {_money(marketplace_metrics['reimbursements'])}",
        f"- Marketplace withheld tax: {_money(marketplace_metrics['withheld_tax'])}",
        f"- Marketplace failed transfers: {_money(marketplace_metrics['failed_transfers'])}",
        f"- Marketplace reserve adjustments: {_money(marketplace_metrics['reserve_adjustments'])}",
        f"- Marketplace uncategorised: {_money(marketplace_metrics['uncategorised'])}",
        f"- Marketplace deferred amount: {_money(marketplace_metrics['deferred'])}",
        f"- Marketplace released amount: {_money(marketplace_metrics['released'])}",
        f"- Total bank/payment receipts: {_money(total_receipts)}",
        f"- Matched amount: {_money(matched_total)}",
        f"- Unmatched expected amount: {_money(unmatched_expected_total)}",
        f"- Unmatched bank/payment receipt amount: {_money(unmatched_receipt_total)}",
        "",
        "## CFO Notes",
        "",
        "- This report uses only files currently present in the configured data folder.",
        "- Marketplace transaction reports show net sales, refunds, fees, deferred balances, and released activity.",
        "- Bank/payment feeds are required before confirmed cash balance and receipt reconciliation can be trusted.",
        "- Unmatched expected payouts represent missing, delayed, short-paid, or not-yet-received settlement candidates.",
        "- Unmatched receipts represent bank/payment transactions without a matching expected payout record.",
        "- AI analysis is not used in this report; reconciliation is deterministic and source-data based.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_cfo_dashboard(
    path: Path,
    *,
    source_files: SourceFiles,
    payouts: list[ExpectedPayout],
    receipts: list[BankReceipt],
    amazon_transactions: list[AmazonTransaction],
    result: ReconciliationResult,
    quality_issues: list[DataQualityIssue],
) -> None:
    total_expected = sum((p.amount for p in payouts), Decimal("0"))
    total_receipts = sum((r.amount for r in receipts), Decimal("0"))
    matched_total = sum((p.amount for p, _ in result.matched), Decimal("0"))
    unmatched_expected_total = sum(
        (p.amount for p in result.unmatched_expected), Decimal("0")
    )
    unmatched_receipt_total = sum(
        (r.amount for r in result.unmatched_receipts), Decimal("0")
    )
    marketplace_metrics = _marketplace_metrics(amazon_transactions)
    exception_counts = _exception_counts(result.exceptions)

    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Cash Position Report</title>
  <style>
    :root {{
      --ink: #18212f;
      --muted: #667085;
      --line: #d9e0e8;
      --panel: #ffffff;
      --page: #f5f7fa;
      --teal: #087f7a;
      --blue: #2457a6;
      --amber: #b7791f;
      --red: #b42318;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Arial, Helvetica, sans-serif;
      color: var(--ink);
      background: var(--page);
      line-height: 1.45;
    }}
    main {{
      width: min(1180px, calc(100% - 32px));
      margin: 0 auto;
      padding: 28px 0 40px;
    }}
    header {{
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: flex-end;
      border-bottom: 1px solid var(--line);
      padding-bottom: 18px;
      margin-bottom: 18px;
    }}
    h1, h2, p {{ margin: 0; }}
    h1 {{ font-size: 28px; line-height: 1.15; }}
    h2 {{ font-size: 16px; margin-bottom: 12px; }}
    .subtle {{ color: var(--muted); font-size: 13px; }}
    .status {{
      border: 1px solid var(--line);
      background: var(--panel);
      padding: 8px 10px;
      font-size: 13px;
      white-space: nowrap;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
      margin: 18px 0;
    }}
    .card, .section {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
    }}
    .label {{
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0;
    }}
    .value {{
      font-size: 26px;
      font-weight: 700;
      margin-top: 6px;
    }}
    .teal {{ color: var(--teal); }}
    .blue {{ color: var(--blue); }}
    .amber {{ color: var(--amber); }}
    .red {{ color: var(--red); }}
    .two-col {{
      display: grid;
      grid-template-columns: 1.2fr .8fr;
      gap: 12px;
      margin-top: 12px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }}
    th, td {{
      text-align: left;
      padding: 10px 8px;
      border-bottom: 1px solid var(--line);
      vertical-align: top;
    }}
    th {{ color: var(--muted); font-weight: 700; }}
    .amount-row {{
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 12px;
      padding: 8px 0;
      border-bottom: 1px solid var(--line);
      font-size: 14px;
    }}
    .amount-row:last-child {{ border-bottom: 0; }}
    @media (max-width: 820px) {{
      header, .two-col {{ display: block; }}
      .status {{ margin-top: 12px; white-space: normal; }}
      .grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    }}
    @media (max-width: 520px) {{
      main {{ width: min(100% - 20px, 1180px); padding-top: 18px; }}
      .grid {{ grid-template-columns: 1fr; }}
      h1 {{ font-size: 23px; }}
    }}
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <h1>Cash Position Report</h1>
        <p class="subtle">Marketplace cash, input status, and reconciliation status</p>
      </div>
      <div class="status">Data Quality Issues: {len(quality_issues)}</div>
    </header>

    <section class="grid" aria-label="Core reconciliation metrics">
      {_metric_card("Matched Payouts", len(result.matched), "teal")}
      {_metric_card("Unmatched Expected", len(result.unmatched_expected), "amber")}
      {_metric_card("Unmatched Receipts", len(result.unmatched_receipts), "red")}
      {_metric_card("Data Quality Issues", len(quality_issues), "blue")}
    </section>

    <section class="two-col">
      <div class="section">
        <h2>Amount Summary</h2>
        {_amount_row("Total expected payouts", total_expected)}
        {_amount_row("Marketplace net activity", marketplace_metrics["net"])}
        {_amount_row("Marketplace deferred amount", marketplace_metrics["deferred"])}
        {_amount_row("Marketplace released amount", marketplace_metrics["released"])}
        {_amount_row("Total bank/payment receipts", total_receipts)}
        {_amount_row("Matched amount", matched_total)}
        {_amount_row("Unmatched expected amount", unmatched_expected_total)}
        {_amount_row("Unmatched receipt amount", unmatched_receipt_total)}
      </div>
      <div class="section">
        <h2>Source Coverage</h2>
        {_amount_text_row("Marketplace sample files", len(source_files.payout_files))}
        {_amount_text_row("Marketplace transaction files", len(source_files.amazon_transaction_files))}
        {_amount_text_row("Marketplace transaction records", len(amazon_transactions))}
        {_amount_text_row("Bank/payment sample files", len(source_files.receipt_files))}
        {_amount_text_row("Expected payout records", len(payouts))}
        {_amount_text_row("Bank/payment receipt records", len(receipts))}
      </div>
    </section>

    <section class="section" style="margin-top: 12px;">
      <h2>Exception Summary</h2>
      {_exception_table(exception_counts)}
    </section>

    <section class="section" style="margin-top: 12px;">
      <h2>CFO Notes</h2>
      <table>
        <tbody>
          <tr><td>This dashboard uses only files currently present in the configured data folder.</td></tr>
          <tr><td>Marketplace transaction reports show activity; bank feeds are still required for confirmed cash balance.</td></tr>
          <tr><td>Reconciliation is deterministic and source-data based; AI analysis stays disabled until the data foundation is stable.</td></tr>
        </tbody>
      </table>
    </section>
  </main>
</body>
</html>
"""
    path.write_text(html, encoding="utf-8")


def _money(value: Decimal) -> str:
    return f"{value:.2f}"


def _marketplace_metrics(
    transactions: list[AmazonTransaction],
) -> dict[str, Decimal]:
    metrics = {
        "net": Decimal("0"),
        "orders": Decimal("0"),
        "refunds": Decimal("0"),
        "deferred": Decimal("0"),
        "released": Decimal("0"),
        "selling_fees": Decimal("0"),
        "fba_fees": Decimal("0"),
        "other_fees": Decimal("0"),
        "tax": Decimal("0"),
        "reimbursements": Decimal("0"),
        "withheld_tax": Decimal("0"),
        "failed_transfers": Decimal("0"),
        "reserve_adjustments": Decimal("0"),
    }
    for transaction in transactions:
        metrics["net"] += transaction.total
        metrics["selling_fees"] += transaction.selling_fees
        metrics["fba_fees"] += transaction.fba_fees
        metrics["other_fees"] += transaction.other_transaction_fees
        desc = (transaction.description or "").lower()
        txn_type = transaction.transaction_type.lower()

        if "itemprice: tax" in desc or ": tax" in desc:
            metrics["tax"] += transaction.total
        elif txn_type == "order":
            metrics["orders"] += transaction.product_sales
        elif txn_type == "refund":
            metrics["refunds"] += transaction.total
        elif "reimbursement" in txn_type or "reimbursement" in desc:
            metrics["reimbursements"] += transaction.total
        elif "itemwithheldtax" in desc or "marketplacefacilitator" in desc:
            metrics["withheld_tax"] += transaction.total
        elif "transfer" in desc and ("fail" in desc or "unsuccessful" in desc or "cancel" in desc):
            metrics["failed_transfers"] += transaction.total
        elif "reserve" in desc or "other-transaction" in txn_type:
            metrics["reserve_adjustments"] += transaction.total

        if transaction.status.lower() == "deferred":
            metrics["deferred"] += transaction.total
        if transaction.status.lower() == "released":
            metrics["released"] += transaction.total

    # Compute catch-all so breakdown always sums to net
    categorised = (
        metrics["orders"] + metrics["refunds"] + metrics["tax"]
        + metrics["selling_fees"] + metrics["fba_fees"] + metrics["other_fees"]
        + metrics["reimbursements"] + metrics["withheld_tax"]
        + metrics["failed_transfers"] + metrics["reserve_adjustments"]
    )
    metrics["uncategorised"] = metrics["net"] - categorised
    return metrics


def _is_amazon_transaction_report(path: Path) -> bool:
    try:
        with path.open(encoding="utf-8-sig") as handle:
            head = "".join(handle.readline() for _ in range(15)).lower()
    except UnicodeDecodeError:
        with path.open(encoding="utf-8", errors="ignore") as handle:
            head = "".join(handle.readline() for _ in range(15)).lower()
    return (
        "settlement id" in head
        and "transaction status" in head
        and "product sales" in head
    )


def _metric_card(label: str, value: int, color_class: str) -> str:
    return (
        '<div class="card">'
        f'<div class="label">{escape(label)}</div>'
        f'<div class="value {escape(color_class)}">{value}</div>'
        "</div>"
    )


def _amount_row(label: str, value: Decimal) -> str:
    return (
        '<div class="amount-row">'
        f"<span>{escape(label)}</span>"
        f"<strong>{_money(value)}</strong>"
        "</div>"
    )


def _amount_text_row(label: str, value: int) -> str:
    return (
        '<div class="amount-row">'
        f"<span>{escape(label)}</span>"
        f"<strong>{value}</strong>"
        "</div>"
    )


def _exception_table(counts: dict[str, int]) -> str:
    if not counts:
        return "<p class=\"subtle\">No exceptions found.</p>"
    rows = "\n".join(
        "<tr>"
        f"<td>{escape(exception_type)}</td>"
        f"<td>{count}</td>"
        "</tr>"
        for exception_type, count in sorted(counts.items())
    )
    return (
        "<table>"
        "<thead><tr><th>Exception Type</th><th>Count</th></tr></thead>"
        f"<tbody>{rows}</tbody>"
        "</table>"
    )


def _exception_counts(
    exceptions: list[ReconciliationException],
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for exception in exceptions:
        counts[exception.exception_type] = counts.get(exception.exception_type, 0) + 1
    return counts


def _format_exception_counts(counts: dict[str, int]) -> str:
    if not counts:
        return "none"
    return ", ".join(
        f"{exception_type}={count}" for exception_type, count in sorted(counts.items())
    )


def _exception_type_for_payout(payout: ExpectedPayout) -> str:
    return "pending_receipt"


def _exception_type_for_receipt(receipt: BankReceipt) -> str:
    return "unmatched_bank_receipt"
