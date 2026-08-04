"""Marketplace accounts-receivable snapshots normalized to USD."""

import csv
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re


MONEY = Decimal("0.01")
MARKETPLACE_AR_FIELDS = (
    "snapshot_date",
    "rate_date",
    "rate_source",
    "source_id",
    "account_id",
    "venue",
    "group",
    "local_amount",
    "currency",
    "usd_rate",
)


@dataclass(frozen=True)
class MarketplaceArLine:
    snapshot_date: date
    rate_date: date
    rate_source: str
    source_id: str
    account_id: str
    venue: str
    group: str
    local_amount: Decimal
    currency: str
    usd_rate: Decimal

    @property
    def usd_amount(self) -> Decimal:
        return self.local_amount * self.usd_rate


def summarize_marketplace_ar(
    root: str | Path,
    *,
    registry_path: str | Path | None = None,
    fx_path: str | Path | None = None,
    as_of_date: date | None = None,
    ingest_mode: str = "hybrid",
) -> dict[str, object]:
    root = Path(root)
    if ingest_mode not in {"hybrid", "api_only"}:
        raise ValueError("Marketplace AR ingest_mode must be hybrid or api_only")
    registry = Path(registry_path) if registry_path else root / "source_registry.csv"
    if not registry.exists():
        registry = root / "expected_sources.csv"
    expected = [row for row in _load_source_registry(registry) if row["active"]]
    api_lines = _load_lines(root, api_only=True)
    lines = api_lines if ingest_mode == "api_only" else _load_lines(root)
    if not lines:
        return {
            "status": "unavailable",
            "currency": "USD",
            "message": (
                "No API-ingested marketplace AR snapshot has been loaded."
                if ingest_mode == "api_only"
                else "No marketplace AR snapshot has been loaded."
            ),
            "ingest_mode": ingest_mode,
            "api_only_active": ingest_mode == "api_only",
            "api_cutover_ready": False,
            "display_ready": False,
            "api_received_source_count": 0,
            "expected_source_count": len(expected),
            "channel_breakdown": [],
            "currency_breakdown": [],
            "top_balances": [],
            "missing_sources": [],
            "duplicate_rows": 0,
        }

    latest_date = max(line.snapshot_date for line in lines)
    today = as_of_date or date.today()
    if latest_date > today:
        raise ValueError("snapshot_date cannot be in the future")
    lines = [line for line in lines if line.snapshot_date == latest_date]
    if fx_path:
        approved = {
            (line.snapshot_date, line.currency): approved_marketplace_ar_fx(
                fx_path,
                line.snapshot_date,
                line.currency,
            )
            for line in lines
        }
        for line in lines:
            expected_rate_date, expected_rate, expected_source = approved[
                (line.snapshot_date, line.currency)
            ]
            if (
                line.rate_date,
                line.usd_rate,
                line.rate_source,
            ) != (expected_rate_date, expected_rate, expected_source):
                raise ValueError(
                    f"unapproved FX rate for {line.currency} on {line.snapshot_date}"
                )
    gross_total = sum((line.usd_amount for line in lines), Decimal("0"))
    grouped: dict[tuple[object, ...], list[MarketplaceArLine]] = {}
    for line in lines:
        key = (line.snapshot_date, line.source_id, line.account_id)
        grouped.setdefault(key, []).append(line)

    counted: list[MarketplaceArLine] = []
    quarantined: list[tuple[MarketplaceArLine, str]] = []
    conflicting_rows = 0
    for matches in grouped.values():
        if len(matches) == 1:
            counted.extend(matches)
        elif all(line == matches[0] for line in matches[1:]):
            counted.append(matches[0])
            quarantined.extend((line, "exact_duplicate") for line in matches[1:])
        else:
            conflicting_rows += len(matches)
            quarantined.extend((line, "conflicting_duplicate") for line in matches)

    controlled_total = sum((line.usd_amount for line in counted), Decimal("0"))
    fx_exposed = sum(
        (line.usd_amount for line in lines if line.currency != "USD"),
        Decimal("0"),
    )
    received_ids = {line.source_id for line in lines}
    expected_by_id = {str(row["source_id"]): row for row in expected}
    api_latest_date = max((line.snapshot_date for line in api_lines), default=None)
    api_received_ids = {
        line.source_id
        for line in api_lines
        if line.snapshot_date == api_latest_date
    }
    missing = [
        str(row["source_name"])
        for row in expected
        if row["source_id"] not in received_ids
    ]
    unknown_sources = sorted(received_ids - expected_by_id.keys())
    currency_mismatches = sorted({
        (
            f"{expected_by_id[line.source_id]['source_name']}: expected "
            f"{expected_by_id[line.source_id]['expected_currency']}, "
            f"received {line.currency}"
        )
        for line in lines
        if line.source_id in expected_by_id
        and expected_by_id[line.source_id]["expected_currency"]
        and line.currency != expected_by_id[line.source_id]["expected_currency"]
    })
    pending = [
        str(row["source_name"])
        for row in expected
        if row["verification_status"] != "approved"
    ]
    pending_connectors = [
        str(row["source_name"])
        for row in expected
        if row["connector_status"] != "ready"
    ]
    coverage_verified = (
        bool(expected)
        and not missing
        and not unknown_sources
        and not pending
        and not pending_connectors
        and not currency_mismatches
    )
    age_days = (today - latest_date).days
    expected_ids = set(expected_by_id)
    api_cutover_ready = (
        bool(expected_ids)
        and api_latest_date == latest_date
        and api_received_ids == expected_ids
        and not pending
        and not pending_connectors
        and not currency_mismatches
        and not quarantined
        and not conflicting_rows
        and age_days <= 1
    )
    channel_breakdown = _breakdown(lines, "group", gross_total)
    currency_breakdown = _breakdown(lines, "currency", gross_total)
    ranked = sorted(lines, key=lambda line: line.usd_amount, reverse=True)
    status = (
        "blocked"
        if conflicting_rows or currency_mismatches
        else "ready"
        if coverage_verified and not quarantined and age_days <= 1
        else "partial"
    )

    return {
        "status": status,
        "currency": "USD",
        "ingest_mode": ingest_mode,
        "api_only_active": ingest_mode == "api_only",
        "api_cutover_ready": api_cutover_ready,
        "display_ready": api_cutover_ready,
        "api_received_source_count": len(api_received_ids),
        "snapshot_date": latest_date.isoformat(),
        "rate_date": max(line.rate_date for line in lines).isoformat(),
        "rate_sources": sorted({line.rate_source for line in lines}),
        "snapshot_age_days": age_days,
        "freshness_status": "current" if age_days <= 1 else "stale",
        "row_count": len(lines),
        "counted_row_count": len(counted),
        "quarantined_row_count": len(quarantined),
        "marketplace_count": len(lines),
        "unique_marketplace_count": len({_normalize(line.venue) for line in lines}),
        "source_count": len(received_ids),
        "expected_source_count": len(expected),
        "received_source_count": len(received_ids),
        "missing_sources": missing,
        "unknown_sources": unknown_sources,
        "currency_mismatches": currency_mismatches,
        "coverage_verified": coverage_verified,
        "pending_source_approvals": pending,
        "pending_connector_count": len(pending_connectors),
        "automated_source_count": sum(
            row["connector_status"] == "ready"
            and row["connector_type"] != "manual_csv"
            for row in expected
        ),
        "manual_source_count": sum(
            row["connector_status"] == "ready"
            and row["connector_type"] == "manual_csv"
            for row in expected
        ),
        "duplicate_rows": len(quarantined),
        "conflicting_rows": conflicting_rows,
        "duplicate_value_usd": _money(
            sum((line.usd_amount for line, _ in quarantined), Decimal("0"))
        ),
        "gross_received_ar_usd": _money(gross_total),
        "total_ar_usd": _money(gross_total),
        "controlled_total_ar_usd": _money(controlled_total),
        "fx_exposed_ar_usd": _money(fx_exposed),
        "fx_exposed_share": float(fx_exposed / gross_total) if gross_total else 0,
        "channel_breakdown": channel_breakdown,
        "currency_breakdown": currency_breakdown,
        "top_balances": [_line_dict(line, gross_total) for line in ranked[:10]],
        "data_quality_exceptions": [
            {
                "source_id": line.source_id,
                "account_id": line.account_id,
                "venue": line.venue,
                "reason": reason,
                "amount_usd": _money(line.usd_amount),
            }
            for line, reason in quarantined[:50]
        ],
    }


def approved_marketplace_ar_fx(
    path: str | Path,
    snapshot_date: date,
    currency: str,
) -> tuple[date, Decimal, str]:
    path = Path(path)
    if not path.exists():
        raise ValueError("approved Marketplace AR FX rate table is missing")
    currency = currency.strip().upper()
    candidates: dict[date, tuple[Decimal, str]] = {}
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for row_number, row in enumerate(csv.DictReader(handle), start=2):
            try:
                row_currency = row["currency"].strip().upper()
                rate_date = date.fromisoformat(row["rate_date"].strip())
                rate = Decimal(row["usd_rate"].strip())
                rate_source = row["rate_source"].strip()
                if not rate.is_finite() or rate <= 0:
                    raise ValueError("usd_rate must be positive")
                if row_currency == "USD" and (
                    rate != Decimal("1") or rate_source != "Fixed USD"
                ):
                    raise ValueError("USD rate must be 1 from Fixed USD")
                if row_currency != "USD" and rate_source != "ECB euro reference rates":
                    raise ValueError("non-USD rate source must be ECB euro reference rates")
                if row_currency == currency and rate_date <= snapshot_date:
                    value = (rate, rate_source)
                    if rate_date in candidates and candidates[rate_date] != value:
                        raise ValueError("conflicting approved FX rates")
                    candidates[rate_date] = value
            except (KeyError, InvalidOperation, ValueError) as exc:
                raise ValueError(f"{path.name} row {row_number}: {exc}") from exc
    if not candidates:
        raise ValueError(
            f"no approved FX rate for {currency} on or before {snapshot_date}"
        )
    rate_date = max(candidates)
    rate, rate_source = candidates[rate_date]
    return rate_date, rate, rate_source


def _load_lines(root: Path, *, api_only: bool = False) -> list[MarketplaceArLine]:
    lines: list[MarketplaceArLine] = []
    for path in sorted(root.glob("*.csv")):
        if path.name in {"expected_sources.csv", "source_registry.csv"}:
            continue
        if api_only and not path.name.startswith("api_"):
            continue
        with path.open(newline="", encoding="utf-8-sig") as handle:
            for row_number, row in enumerate(csv.DictReader(handle), start=2):
                try:
                    currency = row["currency"].strip().upper()
                    rate = Decimal(row["usd_rate"].strip())
                    local_amount = Decimal(row["local_amount"].strip())
                    snapshot_date = date.fromisoformat(row["snapshot_date"].strip())
                    rate_date = date.fromisoformat(row["rate_date"].strip())
                    rate_source = row["rate_source"].strip()
                    if not re.fullmatch(r"[A-Z]{3}", currency):
                        raise ValueError("currency must be a three-letter code")
                    if not rate.is_finite() or rate <= 0:
                        raise ValueError("usd_rate must be positive")
                    if not local_amount.is_finite():
                        raise ValueError("local_amount must be finite")
                    if currency == "USD" and rate != Decimal("1"):
                        raise ValueError("USD rows must use usd_rate 1")
                    if rate_date > snapshot_date:
                        raise ValueError("rate_date cannot be after snapshot_date")
                    if not rate_source:
                        raise ValueError("rate_source is required")
                    if currency == "USD" and rate_source != "Fixed USD":
                        raise ValueError("USD rows must use rate_source Fixed USD")
                    if (
                        currency != "USD"
                        and not rate_source.startswith("ECB euro reference rates")
                    ):
                        raise ValueError(
                            "non-USD rows must use ECB euro reference rates"
                        )
                    source_id = row["source_id"].strip()
                    lines.append(
                        MarketplaceArLine(
                            snapshot_date=snapshot_date,
                            rate_date=rate_date,
                            rate_source=rate_source,
                            source_id=source_id,
                            account_id=(row.get("account_id") or source_id).strip(),
                            venue=row["venue"].strip(),
                            group=row["group"].strip(),
                            local_amount=local_amount,
                            currency=currency,
                            usd_rate=rate,
                        )
                    )
                    if not all((
                        lines[-1].source_id,
                        lines[-1].account_id,
                        lines[-1].venue,
                        lines[-1].group,
                    )):
                        raise ValueError(
                            "source_id, account_id, venue, and group are required"
                        )
                except (KeyError, InvalidOperation, ValueError) as exc:
                    raise ValueError(f"{path.name} row {row_number}: {exc}") from exc
    fx_rates: dict[tuple[date, str], tuple[date, Decimal, str]] = {}
    for line in lines:
        key = (line.snapshot_date, line.currency)
        value = (line.rate_date, line.usd_rate, line.rate_source)
        if key in fx_rates and fx_rates[key] != value:
            raise ValueError(
                f"inconsistent FX rate for {line.currency} on {line.snapshot_date}"
            )
        fx_rates[key] = value
    return lines


def _load_source_registry(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows: list[dict[str, object]] = []
        seen: set[str] = set()
        for row_number, row in enumerate(csv.DictReader(handle), start=2):
            source_id = row.get("source_id", "").strip()
            source_name = row.get("source_name", "").strip()
            if not source_id:
                continue
            if not source_name:
                raise ValueError(f"{path.name} row {row_number}: source_name is required")
            if source_id in seen:
                raise ValueError(f"{path.name} row {row_number}: duplicate source_id")
            seen.add(source_id)
            active = row.get("active", "true").strip().lower()
            if active not in {"true", "false"}:
                raise ValueError(f"{path.name} row {row_number}: active must be true or false")
            verification = row.get("verification_status", "pending").strip().lower()
            if verification not in {"approved", "pending"}:
                raise ValueError(
                    f"{path.name} row {row_number}: invalid verification_status"
                )
            connector_status = row.get("connector_status", "pending").strip().lower()
            if connector_status not in {"ready", "pending"}:
                raise ValueError(
                    f"{path.name} row {row_number}: invalid connector_status"
                )
            rows.append({
                "source_id": source_id,
                "source_name": source_name,
                "active": active == "true",
                "connector_type": (
                    row.get("connector_type", "manual_csv").strip() or "manual_csv"
                ),
                "connector_status": connector_status,
                "expected_currency": row.get("expected_currency", "").strip().upper(),
                "verification_status": verification,
            })
        return rows


def _breakdown(
    lines: list[MarketplaceArLine],
    field: str,
    total: Decimal,
) -> list[dict[str, object]]:
    grouped: dict[str, tuple[int, Decimal]] = {}
    for line in lines:
        key = str(getattr(line, field))
        count, amount = grouped.get(key, (0, Decimal("0")))
        grouped[key] = (count + 1, amount + line.usd_amount)
    return [
        {
            "name": name,
            "row_count": count,
            "amount_usd": _money(amount),
            "share": float(amount / total) if total else 0,
        }
        for name, (count, amount) in sorted(
            grouped.items(), key=lambda item: item[1][1], reverse=True
        )
    ]


def _line_dict(line: MarketplaceArLine, total: Decimal) -> dict[str, object]:
    return {
        "source_id": line.source_id,
        "account_id": line.account_id,
        "venue": line.venue,
        "group": line.group,
        "local_amount": _money(line.local_amount),
        "currency": line.currency,
        "usd_rate": str(line.usd_rate),
        "rate_source": line.rate_source,
        "amount_usd": _money(line.usd_amount),
        "share": float(line.usd_amount / total) if total else 0,
    }


def _money(value: Decimal) -> str:
    return str(value.quantize(MONEY))


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())
