"""Validated, atomic refresh of approved currency-to-USD reference rates."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree


ECB_DAILY_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
CBUAE_DAILY_URL = "https://www.centralbank.ae/umbraco/Surface/Exchange/GetExchangeRateAllCurrencyDate"
ECB_SOURCE = "ECB euro reference rates"
CBUAE_SOURCE = "CBUAE USD/AED reference rate"
USD_SOURCE = "Fixed USD"
_CURRENCY = re.compile(r"^[A-Z]{3}$")


@dataclass(frozen=True)
class FxDataset:
    effective_date: date
    retrieved_at: datetime
    base_currency: str
    rates: dict[str, Decimal]
    sources: dict[str, str]
    status: str


class _TableRows(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "tr":
            self._row = []
        elif tag.lower() in {"td", "th"} and self._row is not None:
            self._cell = []

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"td", "th"} and self._row is not None and self._cell is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag.lower() == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None


def _decimal(value: object) -> Decimal:
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("rate must be a valid Decimal value") from exc
    if not result.is_finite():
        raise ValueError("rate must be a valid Decimal value")
    if result <= 0:
        raise ValueError("rate must be positive")
    return result


def _currency(value: object) -> str:
    result = str(value).strip().upper()
    if not _CURRENCY.fullmatch(result):
        raise ValueError(f"invalid ISO currency code: {result or 'missing'}")
    return result


def _ecb_rates(payload: bytes) -> tuple[date, dict[str, Decimal]]:
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as exc:
        raise ValueError("ECB response is not valid XML") from exc
    dated = [node for node in root.iter() if node.tag.rsplit("}", 1)[-1] == "Cube" and node.get("time")]
    if len(dated) != 1:
        raise ValueError("ECB response must contain exactly one effective date")
    try:
        effective_date = date.fromisoformat(str(dated[0].get("time")))
    except ValueError as exc:
        raise ValueError("ECB effective date is invalid") from exc
    rates: dict[str, Decimal] = {}
    for node in dated[0]:
        if node.tag.rsplit("}", 1)[-1] != "Cube":
            continue
        code = _currency(node.get("currency"))
        if code in rates:
            raise ValueError(f"ECB response contains duplicate currency {code}")
        rates[code] = _decimal(node.get("rate"))
    usd_per_eur = rates.get("USD")
    if usd_per_eur is None:
        raise ValueError("ECB response is missing required currencies: USD")
    converted = {"USD": Decimal("1"), "EUR": usd_per_eur}
    converted.update({code: usd_per_eur / rate for code, rate in rates.items() if code != "USD"})
    return effective_date, converted


def _cbuae_aed_per_usd(payload: str) -> Decimal:
    parser = _TableRows()
    parser.feed(payload)
    names = {"us dollar", "دولار امريكي", "دولار أمريكي"}
    matches = []
    for row in parser.rows:
        normalized = {cell.strip().lower() for cell in row}
        if normalized & names:
            matches.append(_decimal(row[-1]))
    if len(matches) != 1:
        raise ValueError("CBUAE response must contain exactly one USD/AED rate")
    return matches[0]


def validate_fx_dataset(
    dataset: FxDataset,
    required_currencies: set[str],
    now: datetime,
    max_age_days: int,
) -> None:
    required = {_currency(code) for code in required_currencies}
    missing = sorted(required - dataset.rates.keys())
    if missing:
        raise ValueError(f"FX dataset is missing required currencies: {', '.join(missing)}")
    if dataset.base_currency != "USD" or dataset.rates.get("USD") != Decimal("1"):
        raise ValueError("FX dataset base currency must be USD with rate 1")
    for code, rate in dataset.rates.items():
        _currency(code)
        _decimal(rate)
        if not dataset.sources.get(code):
            raise ValueError(f"FX dataset has no source identifier for {code}")
    current_date = now.astimezone(timezone.utc).date()
    age = (current_date - dataset.effective_date).days
    if age < 0:
        raise ValueError("FX effective date is in the future")
    if age > max_age_days:
        raise ValueError(f"FX rates are older than {max_age_days} days")


def build_fx_dataset(
    ecb_payload: bytes,
    cbuae_payload: str,
    required_currencies: set[str],
    retrieved_at: datetime,
    max_age_days: int,
) -> FxDataset:
    if retrieved_at.tzinfo is None:
        retrieved_at = retrieved_at.replace(tzinfo=timezone.utc)
    effective_date, rates = _ecb_rates(ecb_payload)
    rates["AED"] = Decimal("1") / _cbuae_aed_per_usd(cbuae_payload)
    sources = {code: ECB_SOURCE for code in rates}
    sources.update({"USD": USD_SOURCE, "AED": CBUAE_SOURCE})
    dataset = FxDataset(
        effective_date=effective_date,
        retrieved_at=retrieved_at.astimezone(timezone.utc),
        base_currency="USD",
        rates=rates,
        sources=sources,
        status="current",
    )
    validate_fx_dataset(dataset, required_currencies, retrieved_at, max_age_days)
    return dataset


def required_amazon_currencies(database_path: str | Path) -> set[str]:
    currencies = {"USD"}
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            "SELECT marketplaces_json FROM amazon_sources WHERE enabled=1 AND status='Connected'"
        ).fetchall()
    for (raw_marketplaces,) in rows:
        for marketplace in json.loads(raw_marketplaces or "[]"):
            if marketplace.get("is_participating") and str(marketplace.get("name", "")).lower().startswith("amazon"):
                value = str(marketplace.get("currency") or "").strip()
                if value:
                    currencies.add(_currency(value))
    return currencies


def _tenant_bytes(path: Path, dataset: FxDataset, max_age_days: int) -> bytes:
    payload = json.loads(path.read_text(encoding="utf-8"))
    reconciliation = payload.setdefault("reconciliation", {})
    reconciliation.update({
        "usd_exchange_rates": {code: format(rate, "f") for code, rate in sorted(dataset.rates.items())},
        "usd_exchange_rates_as_of": dataset.effective_date.isoformat(),
        "usd_exchange_rates_source": f"{ECB_SOURCE}; {CBUAE_SOURCE}; {USD_SOURCE}",
        "usd_exchange_rates_max_age_days": max_age_days,
        "usd_exchange_rate_metadata": {
            "base_currency": dataset.base_currency,
            "retrieved_at": dataset.retrieved_at.isoformat().replace("+00:00", "Z"),
            "source_identifiers": sorted(set(dataset.sources.values())),
        },
    })
    return (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _marketplace_bytes(path: Path, dataset: FxDataset) -> bytes:
    fieldnames = ["rate_date", "currency", "usd_rate", "rate_source"]
    rows: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    if path.exists():
        with path.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                key = (str(row.get("rate_date", "")), str(row.get("currency", "")).upper())
                if key in seen:
                    raise ValueError(f"Marketplace FX file contains duplicate currency row {key[1]} on {key[0]}")
                seen.add(key)
                rows.append({name: str(row.get(name, "")) for name in fieldnames})
    rate_date = dataset.effective_date.isoformat()
    for code, rate in sorted(dataset.rates.items()):
        if code == "AED":
            continue
        key = (rate_date, code)
        if key in seen:
            continue
        rows.append({
            "rate_date": rate_date,
            "currency": code,
            "usd_rate": format(rate, "f"),
            "rate_source": dataset.sources[code],
        })
        seen.add(key)
    import io
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def _stage(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
        staged = Path(handle.name)
    if path.exists():
        os.chmod(staged, path.stat().st_mode & 0o777)
    return staged


def promote_fx_files(
    tenant_path: str | Path,
    marketplace_path: str | Path,
    dataset: FxDataset,
    backup_dir: str | Path,
    *,
    required_currencies: set[str] | None = None,
    max_age_days: int = 7,
) -> dict[str, Path]:
    tenant = Path(tenant_path)
    marketplace = Path(marketplace_path)
    validate_fx_dataset(
        dataset,
        required_currencies or set(dataset.rates),
        dataset.retrieved_at,
        max_age_days,
    )
    tenant_content = _tenant_bytes(tenant, dataset, max_age_days)
    marketplace_content = _marketplace_bytes(marketplace, dataset)
    backup_root = Path(backup_dir)
    backup_root.mkdir(parents=True, exist_ok=True)
    stamp = dataset.retrieved_at.strftime("%Y%m%dT%H%M%SZ")
    tenant_backup = backup_root / f"{tenant.name}.{stamp}.bak"
    marketplace_backup = backup_root / f"{marketplace.name}.{stamp}.bak"
    shutil.copy2(tenant, tenant_backup)
    shutil.copy2(marketplace, marketplace_backup)
    staged_tenant = _stage(tenant, tenant_content)
    staged_marketplace = _stage(marketplace, marketplace_content)
    try:
        os.replace(staged_tenant, tenant)
        os.replace(staged_marketplace, marketplace)
    except Exception:
        shutil.copy2(tenant_backup, tenant)
        shutil.copy2(marketplace_backup, marketplace)
        raise
    return {"tenant_backup": tenant_backup, "marketplace_backup": marketplace_backup}


def _fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "AI-Cashflow-FX-Refresh/1.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def _write_status(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = _stage(path, (json.dumps(payload, indent=2) + "\n").encode("utf-8"))
    os.replace(staged, path)


def refresh_fx_rates(
    tenant_path: Path,
    marketplace_path: Path,
    database_path: Path,
    backup_dir: Path,
    status_path: Path,
    max_age_days: int = 7,
) -> dict[str, object]:
    retrieved_at = datetime.now(timezone.utc)
    try:
        ecb_payload = _fetch(ECB_DAILY_URL)
        effective_date, _ = _ecb_rates(ecb_payload)
        cbuae_url = CBUAE_DAILY_URL + "?" + urllib.parse.urlencode({"dateTime": effective_date.isoformat()})
        required = required_amazon_currencies(database_path)
        dataset = build_fx_dataset(
            ecb_payload,
            _fetch(cbuae_url).decode("utf-8"),
            required,
            retrieved_at,
            max_age_days,
        )
        backups = promote_fx_files(
            tenant_path,
            marketplace_path,
            dataset,
            backup_dir,
            required_currencies=required,
            max_age_days=max_age_days,
        )
        result = {
            "status": "success",
            "effective_date": dataset.effective_date.isoformat(),
            "retrieved_at": dataset.retrieved_at.isoformat().replace("+00:00", "Z"),
            "required_currencies": sorted(required),
            "sources": sorted(set(dataset.sources.values())),
            "tenant_sha256": hashlib.sha256(tenant_path.read_bytes()).hexdigest(),
            "marketplace_sha256": hashlib.sha256(marketplace_path.read_bytes()).hexdigest(),
            "backups": {name: str(path) for name, path in backups.items()},
        }
    except Exception as exc:
        result = {
            "status": "failed",
            "retrieved_at": retrieved_at.isoformat().replace("+00:00", "Z"),
            "error": str(exc)[:300],
        }
        _write_status(status_path, result)
        raise
    _write_status(status_path, result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh approved FX reference rates atomically.")
    parser.add_argument("--tenant-config", type=Path, required=True)
    parser.add_argument("--marketplace-fx", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--backup-dir", type=Path, required=True)
    parser.add_argument("--status-file", type=Path, required=True)
    parser.add_argument("--max-age-days", type=int, default=7)
    args = parser.parse_args()
    result = refresh_fx_rates(
        args.tenant_config,
        args.marketplace_fx,
        args.database,
        args.backup_dir,
        args.status_file,
        args.max_age_days,
    )
    print(json.dumps({
        "status": result["status"],
        "effective_date": result["effective_date"],
        "required_currencies": result["required_currencies"],
    }))


if __name__ == "__main__":
    main()
