"""Service layer for Phase 0 API routes."""

import csv
from copy import deepcopy
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from threading import Lock
from time import monotonic

from ai_cashflow.amazon_sp_api import AmazonSpApiClient, AmazonSpApiError
from ai_cashflow.config import AppConfig, TenantConfig
from ai_cashflow.ingestion.csv_loader import (
    ColumnMapping,
    load_amazon_transactions_csv,
    load_bank_receipts_csv,
    load_mapped_bank_receipts_csv,
)
from ai_cashflow.models import BankReceipt
from ai_cashflow.proof import evaluate_amazon_proof, match_bank_receipts
from ai_cashflow.reporting import generate_phase0_reports
from ai_cashflow.storage import OperationalStore
from ai_cashflow.integrations import AmazonIntegrationManager, AmazonSourceRegistry
from ai_cashflow.marketplace_ar import (
    MARKETPLACE_AR_FIELDS,
    approved_marketplace_ar_fx,
    summarize_marketplace_ar,
)

MARKETPLACE_CURRENCY_BY_COUNTRY = {
    "AU": "AUD",
    "CA": "CAD",
    "JP": "JPY",
    "MX": "MXN",
    "SG": "SGD",
    "US": "USD",
}
AMAZON_FINANCIAL_POSITION_CACHE_SECONDS = 300
# ponytail: per-process cache; use shared storage only if production adds multiple workers.
_AMAZON_FINANCIAL_POSITION_CACHE: dict[
    str, tuple[float, str | None, dict[str, object]]
] = {}
_AMAZON_FINANCIAL_POSITION_CACHE_LOCK = Lock()


@dataclass(frozen=True)
class Phase0Summary:
    marketplace_sample_files: int
    bank_payment_sample_files: int
    expected_payout_records: int
    marketplace_transaction_records: int
    bank_payment_receipt_records: int
    data_quality_issues: int
    matched_payouts: int
    unmatched_expected_payouts: int
    unmatched_bank_receipts: int
    total_expected_payouts: str
    marketplace_net_activity: str
    marketplace_order_amount: str
    marketplace_refund_amount: str
    marketplace_tax_amount: str
    marketplace_selling_fees: str
    marketplace_fba_fees: str
    marketplace_other_fees: str
    marketplace_reimbursements: str
    marketplace_withheld_tax: str
    marketplace_failed_transfers: str
    marketplace_reserve_adjustments: str
    marketplace_uncategorised: str
    marketplace_deferred_amount: str
    marketplace_released_amount: str
    total_bank_payment_receipts: str
    matched_amount: str
    unmatched_expected_amount: str
    unmatched_bank_payment_receipt_amount: str


@dataclass(frozen=True)
class ReportRunResult:
    status: str
    reports_dir: Path


class Phase0Service:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.store = OperationalStore(config.database_path)

    def run_report(self) -> ReportRunResult:
        entity = self.config.tenant.poc_entity or "Unknown"
        active_amazon_source_ids = None
        master_key = os.getenv("AI_CASHFLOW_MASTER_KEY", "")
        if master_key:
            sources = AmazonSourceRegistry(
                self.config.database_path,
                master_key,
            ).list_sources()
            active_amazon_source_ids = {
                source["id"]
                for source in sources
                if source["enabled"] and source["status"] == "Connected"
            }
        reports_dir = generate_phase0_reports(
            self.config.samples_dir,
            self.config.reports_dir,
            database_path=self.config.database_path,
            source_mode=self.config.report_source_mode,
            entity=entity,
            usd_exchange_rates=self.config.tenant.reconciliation.usd_exchange_rates,
            active_amazon_source_ids=active_amazon_source_ids,
        )
        self._record_run("generated")
        return ReportRunResult(status="generated", reports_dir=reports_dir)

    def get_canonical_payout_state(
        self, source_id: str | None = None, currency: str | None = None,
    ) -> dict[str, object]:
        """Read persisted canonical payouts without refreshing Amazon."""
        master_key = os.getenv("AI_CASHFLOW_MASTER_KEY", "")
        if not master_key:
            raise ValueError("The integration encryption key has not been provisioned.")
        return AmazonSourceRegistry(
            self.config.database_path, master_key,
        ).get_canonical_payout_state(source_id, currency)

    def get_summary(self) -> Phase0Summary:
        summary_path = self.config.reports_dir / "cfo_summary.md"
        if not summary_path.exists():
            self.run_report()
        text = summary_path.read_text(encoding="utf-8")
        return Phase0Summary(
            marketplace_sample_files=_metric_int(text, "Marketplace sample files"),
            bank_payment_sample_files=_metric_int(text, "Bank/payment sample files"),
            expected_payout_records=_metric_int(text, "Expected payout records"),
            marketplace_transaction_records=_metric_int_or_default(
                text, "Marketplace transaction records"
            ),
            bank_payment_receipt_records=_metric_int(
                text, "Bank/payment receipt records"
            ),
            data_quality_issues=_metric_int(text, "Data quality issues"),
            matched_payouts=_metric_int(text, "Matched payouts"),
            unmatched_expected_payouts=_metric_int(text, "Unmatched expected payouts"),
            unmatched_bank_receipts=_metric_int(
                text, "Unmatched bank/payment receipts"
            ),
            total_expected_payouts=_metric_text(text, "Total expected payouts"),
            marketplace_net_activity=_metric_text_or_default(
                text, "Marketplace net activity"
            ),
            marketplace_order_amount=_metric_text_or_default(
                text, "Marketplace order amount"
            ),
            marketplace_refund_amount=_metric_text_or_default(
                text, "Marketplace refund amount"
            ),
            marketplace_tax_amount=_metric_text_or_default(
                text, "Marketplace tax amount"
            ),
            marketplace_selling_fees=_metric_text_or_default(
                text, "Marketplace selling fees"
            ),
            marketplace_fba_fees=_metric_text_or_default(
                text, "Marketplace FBA fees"
            ),
            marketplace_other_fees=_metric_text_or_default(
                text, "Marketplace other fees"
            ),
            marketplace_reimbursements=_metric_text_or_default(
                text, "Marketplace reimbursements"
            ),
            marketplace_withheld_tax=_metric_text_or_default(
                text, "Marketplace withheld tax"
            ),
            marketplace_failed_transfers=_metric_text_or_default(
                text, "Marketplace failed transfers"
            ),
            marketplace_reserve_adjustments=_metric_text_or_default(
                text, "Marketplace reserve adjustments"
            ),
            marketplace_uncategorised=_metric_text_or_default(
                text, "Marketplace uncategorised"
            ),
            marketplace_deferred_amount=_metric_text_or_default(
                text, "Marketplace deferred amount"
            ),
            marketplace_released_amount=_metric_text_or_default(
                text, "Marketplace released amount"
            ),
            total_bank_payment_receipts=_metric_text(
                text, "Total bank/payment receipts"
            ),
            matched_amount=_metric_text(text, "Matched amount"),
            unmatched_expected_amount=_metric_text(text, "Unmatched expected amount"),
            unmatched_bank_payment_receipt_amount=_metric_text(
                text, "Unmatched bank/payment receipt amount"
            ),
        )

    def get_exceptions(self) -> dict[str, int]:
        return _read_count_csv(self.config.reports_dir / "exception_summary.csv")

    def get_tenant_settings(self) -> dict[str, object]:
        return self.config.tenant.to_dict()

    def update_tenant_settings(self, payload: dict[str, object]) -> dict[str, object]:
        tenant = TenantConfig.from_dict(payload)
        _validate_workspace_identity(tenant)
        tenant.save(self.config.tenant_config_path)
        self.config.tenant = tenant
        return tenant.to_dict()

    def get_data_quality(self) -> list[dict[str, str]]:
        path = self.config.reports_dir / "data_quality_report.csv"
        if not path.exists():
            self.run_report()
        with path.open(newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))

    def get_matched_payouts(self) -> list[dict[str, str]]:
        return self._read_report_csv("matched_payouts.csv")

    def get_unmatched_expected(self) -> list[dict[str, str]]:
        return self._read_report_csv("unmatched_expected_payouts.csv")

    def get_unmatched_receipts(self) -> list[dict[str, str]]:
        return self._read_report_csv("unmatched_bank_receipts.csv")

    def get_marketplace_activity(self) -> list[dict[str, str]]:
        return self.get_marketplace_activity_response()["rows"]

    def get_marketplace_activity_response(self) -> dict[str, object]:
        try:
            rows = self._read_report_csv("marketplace_activity.csv")
        except (OSError, UnicodeError, csv.Error):
            return {
                "status": "unavailable",
                "rows": [],
                "diagnostics": {
                    "invalid_marketplace_activity_rows": True,
                    "skipped_row_count": 0,
                    "validation_reason": "marketplace_activity_unavailable",
                    "validation_reasons": ["marketplace_activity_unavailable"],
                },
            }

        required = ("posted_at", "type", "marketplace", "currency", "total")
        valid = []
        reasons = []
        for row in rows:
            missing = [field for field in required if not str(row.get(field) or "").strip()]
            if missing:
                reasons.append("empty_row" if not any(row.values()) else "missing_required_fields")
                continue
            valid.append({key: str(value) for key, value in row.items() if key is not None and value is not None})
        return {
            "status": "partial" if reasons else "ready",
            "rows": valid,
            "diagnostics": {
                "invalid_marketplace_activity_rows": bool(reasons),
                "skipped_row_count": len(reasons),
                "validation_reason": reasons[0] if reasons else None,
                "validation_reasons": sorted(set(reasons)),
            },
        }

    def get_marketplace_ar(self) -> dict[str, object]:
        return summarize_marketplace_ar(
            self.config.samples_dir / "marketplace_ar",
            registry_path=self.config.marketplace_ar_registry_path,
            fx_path=self.config.marketplace_ar_fx_path,
            ingest_mode=self.config.marketplace_ar_ingest_mode,
        )

    def get_source_evidence(self) -> list[dict[str, str]]:
        return self._read_report_csv("source_evidence.csv")

    def get_input_readiness(self) -> dict[str, object]:
        summary = self.get_summary()
        tenant = self.config.tenant
        samples_dir = self.config.samples_dir
        calculation_audit = self.get_calculation_audit()

        # Classify sources by their intake method
        api_sources = [s for s in tenant.sources.marketplaces if "api" in s.type.lower()]
        file_sources = [s for s in tenant.sources.marketplaces if "api" not in s.type.lower()]
        bank_sources = list(tenant.sources.banks) if tenant.sources.banks else []
        is_api_first = len(api_sources) > 0

        # Per-source API credential checks
        api_credential_checks = [
            _readiness_check(
                f"API credential — {s.name}",
                bool(s.api_endpoint.strip() and s.credential_reference.strip()),
                f"Endpoint and credential reference configured for {s.name}.",
                f"Set API endpoint and credential reference for '{s.name}' in Marketplaces → Edit.",
            )
            for s in api_sources
        ]

        # Per-source file checks (only for non-API sources)
        api_dir = samples_dir / "marketplaces" / "api"
        manual_dir = samples_dir / "manual_uploads"
        api_files = _count_csv_files(api_dir)
        manual_files = _count_csv_files(manual_dir)
        portal_files = _count_csv_files(samples_dir / "marketplaces" / "portal_reports")
        total_file_sources = _count_csv_files(samples_dir / "marketplaces") + manual_files

        file_source_checks = (
            []
            if is_api_first
            else [
                _readiness_check(
                    "Marketplace report files",
                    total_file_sources > 0,
                    f"{total_file_sources} CSV file(s) available.",
                    "Upload at least one marketplace payout, transaction, or settlement file.",
                ),
                _readiness_check(
                    "Portal report files",
                    portal_files > 0,
                    f"{portal_files} portal/report file(s) available.",
                    "Download and upload the settlement report from the marketplace portal.",
                ),
            ]
        )

        # Bank / payment source checks
        bank_checks = []
        if bank_sources:
            bank_api_sources = [b for b in bank_sources if b.connection_type and "api" in b.connection_type.lower()]
            bank_file_sources = [b for b in bank_sources if b not in bank_api_sources]
            for b in bank_api_sources:
                bank_checks.append(_readiness_check(
                    f"Bank API credential — {b.name}",
                    bool(getattr(b, "api_endpoint", "").strip() and getattr(b, "credential_reference", "").strip()),
                    f"Bank API connection configured for {b.name}.",
                    f"Set API endpoint and credential for bank source '{b.name}'.",
                ))
            if bank_file_sources:
                bank_file_count = _count_csv_files(samples_dir / "banks")
                bank_checks.append(_readiness_check(
                    "Bank / payment receipt files",
                    bank_file_count > 0,
                    f"{bank_file_count} bank/payment file(s) loaded.",
                    "Upload bank statement or payment processor export to confirm cash receipts.",
                ))
        else:
            # No bank source configured at all
            bank_checks.append(_readiness_check(
                "Cash confirmation source",
                False,
                "Bank or payment source configured.",
                "Add a bank account or payment processor in Settings → Bank & Payment Sources to confirm received cash.",
            ))

        checks = [
            _readiness_check(
                "Entity and currency setup",
                bool(tenant.poc_entity and tenant.currencies),
                f"{tenant.poc_entity} configured with {', '.join(tenant.currencies)}.",
                "Configure entity and currency in Settings.",
            ),
            _readiness_check(
                "Marketplace source setup",
                bool(tenant.sources.marketplaces),
                f"{len(tenant.sources.marketplaces)} marketplace source(s) configured.",
                "Add at least one marketplace source in Settings → Marketplaces.",
            ),
            *api_credential_checks,
            *file_source_checks,
            *bank_checks,
            _readiness_check(
                "Transaction activity records",
                summary.marketplace_transaction_records > 0,
                f"{summary.marketplace_transaction_records} transaction record(s) loaded.",
                "No marketplace transaction data received yet — trigger a sync or upload a file.",
            ),
            _readiness_check(
                "Expected payout records",
                summary.expected_payout_records > 0,
                f"{summary.expected_payout_records} payout record(s) loaded.",
                "No expected payout records — run reconciliation after data is available.",
            ),
            _readiness_check(
                "Calculation audit",
                calculation_audit["status"] == "passed",
                "Displayed totals have passed calculation audit.",
                "Displayed totals need calculation review.",
            ),
            _readiness_check(
                "Planned outflow amounts",
                not _has_placeholder_outflows(tenant),
                "Planned outflow amounts are configured.",
                "Confirm planned outflow amounts in Settings → Planned Outflows.",
            ),
            _deferred_check("Automated alerts", "Alert delivery is next-phase scope."),
            _deferred_check("Forecast automation", "Live forecast automation is next-phase scope."),
        ]
        blocking = [check for check in checks if check["status"] == "blocked"]
        warning = [check for check in checks if check["status"] == "warning"]
        score = round(
            100 * (len(checks) - len(blocking) - (len(warning) * 0.5)) / len(checks)
        )
        return {
            "status": _readiness_status(blocking, warning),
            "score": max(0, score),
            "mode": "source_data_mode",
            "blocking_count": len(blocking),
            "warning_count": len(warning),
            "summary": (
                "Marketplace CSV intake is active. Configure API connector, "
                "settlement records, outflows, alert delivery, and forecast "
                "automation for live operation."
            ),
            "checks": checks,
        }

    def get_calculation_audit(self) -> dict[str, object]:
        summary = self.get_summary()
        matched = self.get_matched_payouts()
        unmatched_expected = self.get_unmatched_expected()
        unmatched_receipts = self.get_unmatched_receipts()
        marketplace_activity = self.get_marketplace_activity()

        checks = [
            _audit_check(
                "Matched payout count",
                Decimal(len(matched)),
                Decimal(summary.matched_payouts),
            ),
            _audit_check(
                "Unmatched expected count",
                Decimal(len(unmatched_expected)),
                Decimal(summary.unmatched_expected_payouts),
            ),
            _audit_check(
                "Unmatched receipt count",
                Decimal(len(unmatched_receipts)),
                Decimal(summary.unmatched_bank_receipts),
            ),
            _audit_check(
                "Matched amount",
                _sum_decimal(matched, "amount"),
                _money_decimal(summary.matched_amount),
            ),
            _audit_check(
                "Unmatched expected amount",
                _sum_decimal(unmatched_expected, "amount"),
                _money_decimal(summary.unmatched_expected_amount),
            ),
            _audit_check(
                "Unmatched receipt amount",
                _sum_decimal(unmatched_receipts, "amount"),
                _money_decimal(summary.unmatched_bank_payment_receipt_amount),
            ),
            _audit_check(
                "Marketplace transaction count",
                Decimal(len(marketplace_activity)),
                Decimal(summary.marketplace_transaction_records),
            ),
            _audit_check(
                "Marketplace net activity",
                _sum_decimal(marketplace_activity, "total"),
                _money_decimal(summary.marketplace_net_activity),
            ),
        ]
        failed = [check for check in checks if check["status"] != "passed"]
        return {
            "status": "passed" if not failed else "failed",
            "failed_count": len(failed),
            "checks": checks,
        }

    def _read_report_csv(self, file_name: str) -> list[dict[str, str]]:
        path = self.config.reports_dir / file_name
        if not path.exists():
            self.run_report()
        with path.open(newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))

    def save_upload(self, file_name: str, category: str, source) -> dict[str, str]:
        self._validate_upload_request(file_name, category)
        safe_name = Path(file_name).name
        target_dir = self.config.samples_dir / category
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / safe_name
        try:
            with target_path.open("wb") as handle:
                _copy_limited(source, handle, self.config.max_upload_bytes)
        except OverflowError:
            target_path.unlink(missing_ok=True)
            raise
        record = {
            "file_name": safe_name,
            "category": category,
            "path": str(target_path),
            "uploaded_at": _utc_now(),
        }
        self.store.add_upload(record)
        self.audit("upload.saved", f"{category}/{safe_name}")
        return record

    def ingest_api_data(
        self,
        source_name: str,
        rows: list[dict],
        report_type: str,
    ) -> dict[str, object]:
        """Accept JSON rows from a live API, save as CSV, and auto-run reconciliation.

        report_type must be one of:
          "transactions"     – Amazon-style transaction rows  → marketplaces/api/
          "settlements"      – settlement/payout rows          → marketplaces/portal_reports/
          "payouts"          – expected-payout rows            → marketplaces/
          "bank_receipts"    – bank receipt rows               → banks/
          "marketplace_ar"    – USD-normalized AR snapshot rows → marketplace_ar/

        Incoming field names are normalised using a loose alias table so the caller
        does not need to match the exact internal column names.
        """
        if not rows:
            return {"status": "empty", "rows_ingested": 0, "report_triggered": False}

        rt = report_type.lower().replace(" ", "_").replace("-", "_")
        if rt == "marketplace_ar":
            return self._ingest_marketplace_ar(source_name, rows)
        category, aliases = _API_TYPE_CONFIG.get(rt, ("marketplaces/api", _AMAZON_ALIASES))

        normalized = [_normalize_api_row(row, aliases) for row in rows]

        ts = _utc_now().replace(":", "").replace("-", "")[:15]
        safe_src = re.sub(r"[^A-Za-z0-9]", "_", source_name)[:24]
        file_name = f"api_{safe_src}_{rt}_{ts}.csv"

        target_dir = self.config.samples_dir / category
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / file_name

        fieldnames = list(normalized[0].keys())
        with target_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(normalized)

        record = {
            "file_name": file_name,
            "category": category,
            "path": str(target_path),
            "uploaded_at": _utc_now(),
        }
        self.store.add_upload(record)
        self.audit("api.ingest", f"{len(rows)} rows from '{source_name}' ({report_type})")

        result = self.run_report()
        return {
            "status": "ingested",
            "rows_ingested": len(rows),
            "file": file_name,
            "category": category,
            "report_status": result.status,
        }

    def _ingest_marketplace_ar(
        self,
        source_name: str,
        rows: list[dict],
    ) -> dict[str, object]:
        if len(rows) > 10_000:
            raise ValueError("Marketplace AR batch cannot exceed 10000 rows.")
        normalized = []
        for row_number, row in enumerate(rows, start=1):
            if not isinstance(row, dict):
                raise ValueError(f"Marketplace AR row {row_number} must be an object.")
            record = {
                field: str(row.get(field, "") or "").strip()
                for field in MARKETPLACE_AR_FIELDS
            }
            record["account_id"] = record["account_id"] or record["source_id"]
            normalized.append(record)
        approved_rates = {}
        for record in normalized:
            try:
                key = (
                    datetime.strptime(record["snapshot_date"], "%Y-%m-%d").date(),
                    record["currency"].upper(),
                )
            except ValueError as exc:
                raise ValueError("Marketplace AR snapshot_date must use YYYY-MM-DD.") from exc
            if key not in approved_rates:
                approved_rates[key] = approved_marketplace_ar_fx(
                    self.config.marketplace_ar_fx_path,
                    *key,
                )
            rate_date, rate, rate_source = approved_rates[key]
            record["rate_date"] = rate_date.isoformat()
            record["usd_rate"] = str(rate)
            record["rate_source"] = rate_source
        normalized.sort(
            key=lambda row: (
                row["snapshot_date"],
                row["source_id"],
                row["account_id"],
                row["venue"],
            )
        )
        snapshot_dates = {row["snapshot_date"] for row in normalized}
        if len(snapshot_dates) != 1:
            raise ValueError("Marketplace AR batch must contain one snapshot_date.")

        digest = hashlib.sha256(
            json.dumps(
                normalized,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:12]
        safe_source = re.sub(r"[^A-Za-z0-9]+", "_", source_name).strip("_")[:24]
        file_name = (
            f"api_{safe_source or 'source'}_marketplace_ar_"
            f"{next(iter(snapshot_dates))}_{digest}.csv"
        )
        target_dir = self.config.samples_dir / "marketplace_ar"
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / file_name
        if target_path.exists():
            return {
                "status": "already_ingested",
                "rows_ingested": 0,
                "file": file_name,
                "category": "marketplace_ar",
                "marketplace_ar": self.get_marketplace_ar(),
            }

        with tempfile.TemporaryDirectory(dir=target_dir) as temp_dir:
            candidate = Path(temp_dir) / file_name
            with candidate.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=MARKETPLACE_AR_FIELDS,
                    quoting=csv.QUOTE_ALL,
                )
                writer.writeheader()
                writer.writerows(normalized)
            validation = summarize_marketplace_ar(
                temp_dir,
                registry_path=self.config.marketplace_ar_registry_path,
                fx_path=self.config.marketplace_ar_fx_path,
                ingest_mode="api_only",
            )
            if validation["conflicting_rows"]:
                raise ValueError(
                    "Marketplace AR batch contains conflicting account rows."
                )
            if validation["unknown_sources"]:
                raise ValueError(
                    "Marketplace AR batch contains source_id values not present "
                    "in the approved registry."
                )
            if validation["currency_mismatches"]:
                raise ValueError(
                    "Marketplace AR batch currency does not match the source registry."
                )
            candidate.replace(target_path)

        record = {
            "file_name": file_name,
            "category": "marketplace_ar",
            "path": str(target_path),
            "uploaded_at": _utc_now(),
        }
        self.store.add_upload(record)
        self.audit(
            "api.ingest",
            f"{len(rows)} Marketplace AR rows from '{source_name}'",
        )
        return {
            "status": "ingested",
            "rows_ingested": len(rows),
            "file": file_name,
            "category": "marketplace_ar",
            "marketplace_ar": self.get_marketplace_ar(),
        }

    def get_api_status(self) -> dict[str, object]:
        """Return current API source configuration and data availability."""
        tenant = self.config.tenant
        master_key = os.getenv("AI_CASHFLOW_MASTER_KEY", "")
        amazon_settings = None
        amazon_sources = []
        if master_key:
            amazon_sources = AmazonSourceRegistry(
                self.config.database_path, master_key
            ).list_sources()
            amazon_settings = AmazonIntegrationManager(
                self.config.database_path, master_key
            ).public()
        api_sources = [
            s for s in (tenant.sources.marketplaces or [])
            if s.api_endpoint or "api" in (s.type or "").lower()
        ]
        api_files = _count_csv_files(self.config.samples_dir / "marketplaces" / "api")
        marketplace_ar = self.get_marketplace_ar()
        return {
            "api_sources_configured": len(api_sources),
            "api_files_loaded": api_files,
            "polling_suggested_interval_seconds": 30,
            "ingest_endpoint": "/phase0/api-ingest",
            "supported_report_types": list(_API_TYPE_CONFIG.keys()),
            "marketplace_ar": {
                "ingest_mode": marketplace_ar["ingest_mode"],
                "api_only_active": marketplace_ar["api_only_active"],
                "api_cutover_ready": marketplace_ar["api_cutover_ready"],
                "api_files_loaded": len(list(
                    (self.config.samples_dir / "marketplace_ar").glob("api_*.csv")
                )),
                "api_received_source_count": marketplace_ar[
                    "api_received_source_count"
                ],
                "expected_source_count": marketplace_ar.get(
                    "expected_source_count", 0
                ),
            },
            "sources": [
                {
                    "name": s.name,
                    "endpoint": s.api_endpoint or "",
                    "has_credential": bool(s.credential_reference),
                    "frequency": s.frequency or "Daily",
                    "status": s.status or "Configured",
                }
                for s in api_sources
            ],
            "field_schema": {
                "transactions": list(_AMAZON_ALIASES.keys()),
                "payouts": list(_PAYOUT_ALIASES.keys()),
                "bank_receipts": list(_RECEIPT_ALIASES.keys()),
                "marketplace_ar": list(MARKETPLACE_AR_FIELDS),
            },
            "amazon_sp_api": {
                "configured": any(
                    all(source["credentials"].values()) for source in amazon_sources
                ) if amazon_sources else bool(
                    amazon_settings and all(amazon_settings["credentials"].values())
                ),
                "sources_total": len(amazon_sources),
                "sources_connected": sum(
                    source["status"] == "Connected" for source in amazon_sources
                ),
                "status": (
                    f"{len(amazon_sources)} source(s) configured"
                    if amazon_sources
                    else amazon_settings["status"] if amazon_settings else "Encryption key required"
                ),
                "credential_storage": "encrypted database" if amazon_settings else "unavailable",
                "settlement_report_type": "GET_V2_SETTLEMENT_REPORT_DATA_FLAT_FILE_V2",
            },
        }

    def list_amazon_settlement_reports(self) -> list[dict[str, object]]:
        """List completed settlement reports without downloading their contents."""
        master_key = os.getenv("AI_CASHFLOW_MASTER_KEY", "")
        if not master_key:
            raise ValueError("The integration encryption key has not been provisioned.")
        config = AmazonIntegrationManager(
            self.config.database_path, master_key
        ).credentials()
        reports = AmazonSpApiClient(config).list_settlement_reports()
        self.audit("amazon.settlement_reports.listed", f"{len(reports)} report metadata rows")
        return reports

    def get_amazon_financial_position(
        self, source_id: str, currency: str | None = None, snapshot_id: str | None = None
    ) -> dict[str, object]:
        """Return one source's current Seller Central statement calculation in USD."""
        master_key = os.getenv("AI_CASHFLOW_MASTER_KEY", "")
        if not master_key:
            raise ValueError("The integration encryption key has not been provisioned.")
        registry = AmazonSourceRegistry(self.config.database_path, master_key)
        source = registry.get_source(source_id)
        snapshot_metadata = registry.financial_snapshot_metadata(source_id)
        snapshot_version = f"{source.get('last_sync_at')}:{snapshot_metadata['transaction_snapshot_id']}"
        if not source["enabled"] or source["status"] != "Connected":
            raise ValueError("The selected Amazon source is not connected and enabled.")
        requested_currency = str(currency or "").strip().upper()
        cache_key = f"{source_id}:{int(self.config.transaction_visibility_enabled)}"
        with _AMAZON_FINANCIAL_POSITION_CACHE_LOCK:
            cached = _AMAZON_FINANCIAL_POSITION_CACHE.get(cache_key)
        if (
            cached
            and cached[1] == snapshot_version
            and monotonic() - cached[0] < AMAZON_FINANCIAL_POSITION_CACHE_SECONDS
        ):
            cached_result = cached[2]
            cached_scope = str(cached_result.get("currency_scope") or "ALL")
            cached_totals = cached_result.get("totals_by_currency") or {}
            if (not requested_currency and cached_scope == "ALL") or (
                requested_currency and requested_currency in cached_totals
            ):
                result = deepcopy(cached_result)
                if requested_currency:
                    native_amount = str(cached_totals[requested_currency])
                    result["currency_scope"] = requested_currency
                    result["source_currency"] = requested_currency
                    result["totals_by_currency"] = {requested_currency: native_amount}
                    result.setdefault("source_amounts", {})["standard_balance"] = native_amount
                    for value in result.get("financial_values", []):
                        if value.get("type") == "OPEN_BALANCE":
                            value["amount"] = native_amount
                            value["currency"] = requested_currency
                if self.config.transaction_visibility_enabled:
                    result["transaction_visibility"] = registry.transaction_visibility(
                        source_id, requested_currency or None
                    )
                result["snapshot_refreshed"] = bool(snapshot_id and snapshot_id != result.get("snapshot_id"))
                return result
        marketplaces = [
            row for row in source.get("marketplaces", [])
            if row.get("is_participating")
            and str(row.get("name", "")).lower().startswith("amazon")
        ]
        rates = self.config.tenant.reconciliation.usd_exchange_rates
        marketplace_currencies = []
        for row in marketplaces:
            country_code = str(row.get("country_code", "")).upper()
            marketplace_currency = str(row.get("currency", "")).upper() or (
                MARKETPLACE_CURRENCY_BY_COUNTRY.get(country_code)
            )
            marketplace_currencies.append((row, marketplace_currency))
        if requested_currency:
            matches = [
                row for row, row_currency in marketplace_currencies
                if row_currency == requested_currency
            ]
            if not matches:
                raise ValueError(
                    f"The selected Amazon source has no {requested_currency} marketplace."
                )
            if len(matches) > 1:
                raise ValueError(
                    f"The selected Amazon source has multiple {requested_currency} marketplaces."
                )
            marketplace = matches[0]
        else:
            marketplace = marketplaces[0] if len(marketplaces) == 1 else None
        client = AmazonSpApiClient(registry.credentials(source_id))
        if marketplace:
            country_code = str(marketplace.get("country_code", "")).upper()
            source_currency = str(marketplace.get("currency", "")).upper() or (
                MARKETPLACE_CURRENCY_BY_COUNTRY.get(country_code)
            )
            if not source_currency:
                raise ValueError(
                    f"No approved currency mapping exists for Amazon country {country_code or 'unknown'}."
                )
            usd_rate = rates.get(source_currency)
            result = client.get_financial_position(
                str(marketplace["id"]),
                usd_rate,
                expected_currency=source_currency,
            )
        else:
            result = client.get_current_balances(rates)
        result.update({
            "source_id": source["id"],
            "source_name": source["name"],
            "marketplace_id": marketplace["id"] if marketplace else None,
            "marketplace_name": marketplace["name"] if marketplace else "Multiple marketplaces",
            "available_currencies": sorted({
                row_currency for _, row_currency in marketplace_currencies if row_currency
            }),
            "usd_rate_as_of": (
                self.config.tenant.reconciliation.usd_exchange_rates_as_of
            ),
            "transaction_visibility_enabled": self.config.transaction_visibility_enabled,
            "currency_scope": requested_currency or "ALL",
        })
        if self.config.transaction_visibility_enabled:
            result["transaction_visibility"] = registry.transaction_visibility(
                source_id, requested_currency or None
            )
        retrieved_at = str(
            result.get("as_of")
            or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        )
        snapshot_seed = json.dumps(
            {
                "source_id": source_id,
                "source_sync_run_id": snapshot_metadata["source_sync_run_id"],
                "financial_group_retrieved_at": retrieved_at,
                "totals": result.get("totals_by_currency") or {
                    str(result.get("source_currency") or requested_currency):
                    str((result.get("source_amounts") or {}).get("standard_balance"))
                },
            }, sort_keys=True, separators=(",", ":"),
        )
        result.update({
            "snapshot_id": hashlib.sha256(snapshot_seed.encode()).hexdigest()[:24],
            "snapshot_created_at": retrieved_at,
            "source_sync_run_id": snapshot_metadata["source_sync_run_id"],
            "financial_group_retrieved_at": retrieved_at,
            "transaction_snapshot_id": snapshot_metadata["transaction_snapshot_id"],
            "transaction_snapshot_retrieved_at": snapshot_metadata["transaction_snapshot_retrieved_at"],
            "data_version": source.get("last_sync_at"),
            "snapshot_refreshed": bool(snapshot_id),
        })
        if result.get("source_currency") and not result.get("totals_by_currency"):
            result["totals_by_currency"] = {
                str(result["source_currency"]): str(
                    (result.get("source_amounts") or {}).get("standard_balance")
                )
            }
        fx_as_of = self.config.tenant.reconciliation.usd_exchange_rates_as_of
        fx_max_age_days = (
            self.config.tenant.reconciliation.usd_exchange_rates_max_age_days
        )
        try:
            fx_age_days = (
                date.fromisoformat(retrieved_at[:10]) - date.fromisoformat(fx_as_of[:10])
            ).days
            fx_status = "stale" if fx_age_days > fx_max_age_days else "current"
        except ValueError:
            fx_age_days = None
            fx_status = "unknown"
        currencies_included = sorted(
            result.get("totals_by_currency", {}).keys()
            if result.get("mode") == "amazon_open_balances"
            else [str(result.get("source_currency", ""))]
        )
        estimated_usd = (
            result.get("amounts", {}).get("current_balance")
            if result.get("mode") == "amazon_open_balances"
            else result.get("amounts", {}).get("standard_balance")
        )
        missing_fx_currencies = sorted(result.get("missing_fx_currencies", []))
        if estimated_usd is None:
            fx_status = "unavailable"
            missing = missing_fx_currencies or currencies_included
            fx_warning = (
                f"No approved USD exchange rate is available for {', '.join(missing)}."
            )
        else:
            fx_warning = (
                f"FX rates are {fx_age_days} days old; configured limit is {fx_max_age_days} days."
                if fx_status == "stale"
                else None
            )
        result["fx_conversion"] = {
            "amount": estimated_usd,
            "base_currency": "USD",
            "fx_source": self.config.tenant.reconciliation.usd_exchange_rates_source,
            "fx_rate_as_of": fx_as_of,
            "fx_retrieved_at": (
                self.config.tenant.reconciliation.usd_exchange_rates_retrieved_at
            ),
            "fx_rate_max_age_days": fx_max_age_days,
            "fx_age_days": fx_age_days,
            "currencies_included": currencies_included,
            "status": fx_status,
            "availability_reason": fx_warning,
            "disclaimer": (
                "Converted values are estimates for reporting purposes. "
                "Native-currency Amazon amounts remain authoritative."
            ),
        }
        result.setdefault("financial_values", []).append({
            "type": "CURRENCY_CONVERSION_ESTIMATE",
            "amount": estimated_usd,
            "currency": "USD",
            "source": "TENANT_FX_CONFIGURATION",
            "sourceField": "usd_exchange_rates",
            "processingStatus": fx_status.upper(),
            "retrievedAt": retrieved_at,
            "effectiveAt": fx_as_of,
            "isAuthoritative": False,
            "authoritativeFor": "REPORTING_ESTIMATE",
            "calculationMethod": "NATIVE_BALANCE_MULTIPLIED_BY_CONFIGURED_USD_RATE",
            "availabilityReason": fx_warning,
        })
        result["data_freshness"] = {
            "status": "current",
            "retrieved_at": retrieved_at,
            "last_successful_sync_at": source.get("last_sync_at"),
            "sync_frequency_minutes": source.get("sync_frequency_minutes"),
        }
        result["source_status"] = {
            "api_processing": {
                "status": "passed",
                "label": "API Processing",
            },
            "api_synchronization": {
                "status": "healthy",
                "label": "API Synchronization",
            },
            "seller_central_reconciliation": {
                "status": "not_verified",
                "label": "Seller Central Reconciliation",
            },
            "reserve_coverage": {
                "status": "unavailable",
                "label": "Reserve Coverage",
            },
            "payout_amount": {
                "status": "unavailable",
                "label": "Payout Amount",
            },
        }
        if marketplace and source_currency:
            statement = registry.latest_seller_central_statement_snapshot(
                source_id, str(marketplace["id"]), source_currency
            )
            now = datetime.now(timezone.utc)
            observed = None
            if statement:
                try:
                    observed = datetime.fromisoformat(str(statement["observed_at"]).replace("Z", "+00:00"))
                except ValueError:
                    pass
            freshness = "NOT_AVAILABLE" if not statement else (
                "FRESH" if observed and now - observed <= timedelta(minutes=30) else
                "STALE" if observed and now - observed <= timedelta(hours=24) else "EXPIRED"
            )
            automatic_standard = (result.get("source_amounts") or {}).get("standard_balance")
            automatic_payout = (result.get("recent_completed_payout") or {}).get("amount")
            def comparison(automatic: object, manual: object) -> dict[str, object]:
                if manual is None or automatic is None: status = "NOT_AVAILABLE"
                elif freshness != "FRESH": status = "TIMING_DIFFERENCE"
                else: status = "MATCH" if Decimal(str(automatic)) == Decimal(str(manual)) else "DIFFERENCE"
                difference = (str(Decimal(str(automatic)) - Decimal(str(manual)))
                              if automatic is not None and manual is not None else None)
                return {"automatic_amount": automatic, "seller_central_reported_amount": manual,
                        "difference": difference, "timing_delta": None if not observed else str(now - observed),
                        "reconciliation_status": status}
            manual = statement or {}
            result["statement_reconciliation"] = {
                "statement_snapshot_id": manual.get("id"), "source_id": source_id,
                "marketplace_id": marketplace["id"], "currency": source_currency,
                "observed_at": manual.get("observed_at"), "freshness": freshness,
                "standard_orders": comparison(automatic_standard, manual.get("standard_orders")),
                "recent_payout": comparison(automatic_payout, manual.get("recent_payout")),
                **{name: {"seller_central_reported_amount": manual.get(field), "authority": "SELLER_CENTRAL_REPORTED",
                           "automatic_equivalent_status": "NOT_PROVEN"}
                   for name, field in (("deferred", "deferred_transactions"), ("funds_available", "funds_available"),
                                       ("account_level_reserve", "account_level_reserve"), ("all_accounts", "all_accounts"))},
            }
        self.audit(
            "amazon.financial_position.read",
            f"{source['id']} {marketplace['id'] if marketplace else 'multi'} USD",
        )
        with _AMAZON_FINANCIAL_POSITION_CACHE_LOCK:
            _AMAZON_FINANCIAL_POSITION_CACHE[cache_key] = (
                monotonic(),
                snapshot_version,
                result,
            )
        return result

    def import_bank_statement(
        self,
        *,
        file_name: str,
        content: bytes,
        bank_source: str,
        column_mapping: dict[str, str] | None = None,
    ) -> dict[str, object]:
        safe_name = Path(file_name).name
        if safe_name != file_name or Path(safe_name).suffix.lower() != ".csv":
            raise ValueError("Bank statements must be CSV files without path segments.")
        if not content or len(content) > self.config.max_upload_bytes or b"\x00" in content:
            raise ValueError("Bank statement is empty, too large, or not valid text.")
        try:
            content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("Bank statement must be UTF-8 encoded.") from exc
        if not bank_source.strip():
            raise ValueError("Bank source is required.")
        required = {
            "receipt_id", "bank_account", "currency",
            "receipt_date", "amount", "reference",
        }
        if column_mapping is not None and set(column_mapping) != required:
            raise ValueError(
                "Column mapping must define receipt_id, bank_account, currency, "
                "receipt_date, amount and reference."
            )
        if column_mapping is not None and (
            not all(isinstance(value, str) and value.strip() for value in column_mapping.values())
            or len(set(column_mapping.values())) != len(column_mapping)
        ):
            raise ValueError("Mapped bank columns must be non-empty and unique.")
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                path = Path(temp_dir) / safe_name
                path.write_bytes(content)
                receipts = (
                    load_mapped_bank_receipts_csv(
                        path, ColumnMapping(columns=column_mapping)
                    )
                    if column_mapping is not None
                    else load_bank_receipts_csv(path)
                )
        except (csv.Error, KeyError, InvalidOperation, ValueError) as exc:
            raise ValueError(f"Invalid bank statement: {exc}") from exc
        if not receipts:
            raise ValueError("Bank statement contains no receipt rows.")
        receipts = [
            BankReceipt(
                receipt_id=receipt.receipt_id.strip(),
                bank_account=receipt.bank_account.strip(),
                currency=receipt.currency.strip().upper(),
                receipt_date=receipt.receipt_date,
                amount=receipt.amount,
                reference=receipt.reference.strip() if receipt.reference else None,
            )
            for receipt in receipts
        ]
        allowed_currencies = {
            str(currency).upper() for currency in self.config.tenant.currencies
        }
        statement_currencies = {receipt.currency for receipt in receipts}
        if len(statement_currencies) != 1:
            raise ValueError("A bank statement must contain exactly one currency.")
        for receipt in receipts:
            if not all((
                receipt.receipt_id.strip(),
                receipt.bank_account.strip(),
                receipt.currency.strip(),
                receipt.reference and receipt.reference.strip(),
            )):
                raise ValueError("Every bank statement field must be populated.")
            if receipt.currency.upper() not in allowed_currencies:
                raise ValueError(
                    f"Bank receipt currency {receipt.currency} is not configured."
                )
            if not receipt.amount.is_finite() or receipt.amount <= 0:
                raise ValueError("Bank receipt amounts must be positive.")
        imported = self.store.import_bank_statement(
            file_name=safe_name,
            file_sha256=hashlib.sha256(content).hexdigest(),
            bank_source=bank_source.strip(),
            receipts=receipts,
            imported_at=_utc_now(),
            column_mapping_json=(
                json.dumps(column_mapping, sort_keys=True)
                if column_mapping is not None else None
            ),
        )
        matched = self._match_stored_bank_receipts()
        self.audit(
            "reconciliation.bank_statement.imported",
            f"{safe_name}: {len(receipts)} rows; {matched} proof matches",
        )
        return {**imported, "proofs_matched": matched}

    def run_cashflow_proof(
        self,
        *,
        window_days: int = 90,
        source_id: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, object]:
        if window_days != 90:
            raise ValueError("Cashflow proof runs use the fixed 90-day window.")
        master_key = os.getenv("AI_CASHFLOW_MASTER_KEY", "")
        if not master_key:
            raise ValueError("The integration encryption key has not been provisioned.")
        checked = now or datetime.now(timezone.utc)
        checked = checked if checked.tzinfo else checked.replace(tzinfo=timezone.utc)
        started_after = checked - timedelta(days=window_days)
        previous_run = self.store.latest_completed_proof_run_at()
        if previous_run:
            previous_time = datetime.fromisoformat(previous_run)
            if previous_time.tzinfo is None:
                previous_time = previous_time.replace(tzinfo=timezone.utc)
            started_after = max(
                started_after,
                previous_time.astimezone(timezone.utc) - timedelta(hours=48),
            )
        registry = AmazonSourceRegistry(self.config.database_path, master_key)
        enabled = [
            source for source in registry.list_sources()
            if source["enabled"]
            and (source_id is None or source["id"] == source_id)
        ]
        run_id = self.store.start_proof_run(
            window_days=window_days,
            source_count=len(enabled),
            started_at=checked.isoformat(),
        )
        item_count = 0
        succeeded = 0
        errors = []
        for source in enabled:
            try:
                if source["status"] != "Connected":
                    raise ValueError("Amazon source is enabled but not connected.")
                item_count += self._prove_amazon_source(
                    registry,
                    source,
                    checked,
                    (
                        checked - timedelta(days=window_days)
                        if self.store.has_due_pending_proofs(
                            str(source["id"]),
                            (checked - timedelta(hours=1)).isoformat(),
                        )
                        else started_after
                    ),
                )
                succeeded += 1
            except Exception as exc:
                errors.append(f"{source['id']}: {str(exc)[:300]}")
        self._match_stored_bank_receipts()
        status = "completed" if not errors else "partial"
        self.store.finish_proof_run(
            run_id,
            status=status,
            item_count=item_count,
            source_success_count=succeeded,
            completed_at=_utc_now(),
            message="; ".join(errors) or f"Checked {item_count} payment group(s).",
        )
        self.audit(
            "reconciliation.proof.run",
            f"run {run_id}: {succeeded}/{len(enabled)} sources, {item_count} groups",
        )
        return {
            "run_id": run_id,
            "status": status,
            "window_days": window_days,
            **self.get_cashflow_proof_summary(),
        }

    def get_cashflow_proof_summary(self) -> dict[str, object]:
        master_key = os.getenv("AI_CASHFLOW_MASTER_KEY", "")
        enabled_count = 0
        if master_key:
            enabled_count = sum(
                source["enabled"]
                for source in AmazonSourceRegistry(
                    self.config.database_path, master_key
                ).list_sources()
            )
        return self.store.get_cashflow_proof_summary(
            enabled_source_count=enabled_count
        )

    def list_cashflow_proof_items(
        self, *, limit: int = 100, offset: int = 0
    ) -> dict[str, object]:
        safe_limit = max(1, min(int(limit), 200))
        safe_offset = max(0, int(offset))
        return {
            "items": self.store.list_cashflow_proofs(
                limit=safe_limit, offset=safe_offset
            ),
            "pagination": {
                "limit": safe_limit,
                "offset": safe_offset,
                "total": self.store.count_cashflow_proofs(),
            },
        }

    def cashflow_proof_due(self, source_id: str) -> bool:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
        return self.store.has_due_pending_proofs(source_id, cutoff.isoformat())

    def _prove_amazon_source(
        self,
        registry: AmazonSourceRegistry,
        source: dict[str, object],
        checked: datetime,
        started_after: datetime,
    ) -> int:
        marketplaces = [
            row for row in source.get("marketplaces", [])
            if row.get("is_participating")
            and str(row.get("name", "")).lower().startswith("amazon")
        ]
        if len(marketplaces) != 1:
            raise ValueError(
                "Amazon source must resolve to exactly one active marketplace."
            )
        marketplace = marketplaces[0]
        country = str(marketplace.get("country_code", "")).upper()
        currency = str(marketplace.get("currency", "")).upper() or (
            MARKETPLACE_CURRENCY_BY_COUNTRY.get(country)
        )
        if not currency:
            raise ValueError(f"No currency mapping exists for Amazon {country}.")
        settlements: dict[str, tuple[Decimal, str, str]] = {}
        for report in registry.list_reports(str(source["id"])):
            path = Path(str(report["cache_path"]))
            report_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            totals: dict[str, Decimal] = {}
            for transaction in load_amazon_transactions_csv(
                path, currency=currency, source_id=str(source["id"])
            ):
                totals[transaction.settlement_id] = (
                    totals.get(transaction.settlement_id, Decimal("0"))
                    + transaction.total
                )
            for settlement_id, total in totals.items():
                settlements[settlement_id] = (
                    total, str(report["report_id"]), report_hash
                )
        client = AmazonSpApiClient(registry.credentials(str(source["id"])))
        groups = client.list_financial_event_groups(started_after)
        record_groups = getattr(registry, "record_financial_event_groups", None)
        if record_groups is not None:
            record_groups(str(source["id"]), groups, checked)
        for group in groups:
            group_id = str(group.get("FinancialEventGroupId", "")).strip()
            settlement = settlements.get(group_id)
            transactions = (
                client.list_payment_transactions(
                    str(marketplace["id"]),
                    group_id,
                    posted_after=started_after,
                )
                if str(group.get("ProcessingStatus", "")).lower() == "closed"
                else []
            )
            proof = evaluate_amazon_proof(
                group,
                transactions,
                settlement_id=group_id if settlement else None,
                settlement_total=settlement[0] if settlement else None,
                tolerance=self.config.amount_tolerance,
                now=checked,
            )
            proof.update({
                "source_id": source["id"],
                "source_name": source["name"],
                "marketplace_id": marketplace["id"],
                "report_id": settlement[1] if settlement else None,
                "evidence_hash": hashlib.sha256(json.dumps(
                    {
                        "group": group,
                        "transactions": transactions,
                        "report_hash": settlement[2] if settlement else None,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode()).hexdigest(),
                "checked_at": checked.isoformat(),
                "last_error": None,
            })
            self.store.upsert_cashflow_proof(proof)
        return len(groups)

    def _match_stored_bank_receipts(self) -> int:
        proofs = []
        for offset in range(0, self.store.count_cashflow_proofs(), 200):
            proofs.extend(
                self.store.list_cashflow_proofs(limit=200, offset=offset)
            )
        receipts = [
            BankReceipt(
                receipt_id=str(row["receipt_id"]),
                bank_account=str(row["bank_account"]),
                currency=str(row["currency"]),
                receipt_date=date.fromisoformat(str(row["receipt_date"])),
                amount=Decimal(str(row["amount"])),
                reference=str(row["reference"]),
            )
            for row in self.store.list_bank_receipts()
        ]
        updated = match_bank_receipts(
            proofs,
            receipts,
            date_tolerance_days=self.config.date_tolerance_days,
            amount_tolerance=self.config.amount_tolerance,
        )
        matched = 0
        for before, after in zip(proofs, updated):
            if before["status"] != "Verified" and after["status"] == "Verified":
                matched += 1
            if after != before:
                self.store.upsert_cashflow_proof(after)
        return matched

    def list_uploads(self) -> list[dict[str, str]]:
        return self.store.list_uploads()

    def delete_upload(self, file_name: str) -> dict[str, object]:
        """Delete the upload record and its file from disk, then re-run the report."""
        stored_path = self.store.delete_upload(file_name)
        if stored_path is None:
            raise ValueError(f"No upload record found for: {file_name}")
        path = Path(stored_path)
        if path.exists():
            path.unlink()
        self.audit("upload.deleted", file_name)
        result = self.run_report()
        return {"status": "deleted", "file": file_name, "report_status": result.status}

    def list_runs(self) -> list[dict[str, object]]:
        return self.store.list_runs()

    def clear_runs(self) -> int:
        return self.store.clear_runs()

    def list_audit_events(self) -> list[dict[str, str]]:
        return self.store.list_audit_events()

    def audit(self, event_type: str, details: str) -> None:
        self.store.add_audit_event(event_type, details)

    def _record_run(self, status: str) -> None:
        summary = self.get_summary_without_regeneration()
        runs = self.list_runs()
        record = {
            "run_id": f"run-{len(runs) + 1}",
            "status": status,
            "created_at": _utc_now(),
            "matched_payouts": summary.matched_payouts,
            "unmatched_expected_payouts": summary.unmatched_expected_payouts,
            "unmatched_bank_receipts": summary.unmatched_bank_receipts,
            "data_quality_issues": summary.data_quality_issues,
        }
        self.store.add_run(record)
        self.audit("report.generated", str(record["run_id"]))

    def _validate_upload_request(self, file_name: str, category: str) -> None:
        safe_name = Path(file_name).name
        if category not in self.config.allowed_upload_categories:
            raise ValueError(f"Unsupported upload category: {category}")
        if safe_name != file_name or not safe_name:
            raise ValueError("Upload file name must not contain path segments.")
        if Path(safe_name).suffix.lower() not in (".csv", ".txt"):
            raise ValueError("Only CSV and TXT uploads are supported.")

    def get_summary_without_regeneration(self) -> Phase0Summary:
        summary_path = self.config.reports_dir / "cfo_summary.md"
        text = summary_path.read_text(encoding="utf-8")
        return Phase0Summary(
            marketplace_sample_files=_metric_int(text, "Marketplace sample files"),
            bank_payment_sample_files=_metric_int(text, "Bank/payment sample files"),
            expected_payout_records=_metric_int(text, "Expected payout records"),
            marketplace_transaction_records=_metric_int_or_default(
                text, "Marketplace transaction records"
            ),
            bank_payment_receipt_records=_metric_int(
                text, "Bank/payment receipt records"
            ),
            data_quality_issues=_metric_int(text, "Data quality issues"),
            matched_payouts=_metric_int(text, "Matched payouts"),
            unmatched_expected_payouts=_metric_int(text, "Unmatched expected payouts"),
            unmatched_bank_receipts=_metric_int(
                text, "Unmatched bank/payment receipts"
            ),
            total_expected_payouts=_metric_text(text, "Total expected payouts"),
            marketplace_net_activity=_metric_text_or_default(
                text, "Marketplace net activity"
            ),
            marketplace_order_amount=_metric_text_or_default(
                text, "Marketplace order amount"
            ),
            marketplace_refund_amount=_metric_text_or_default(
                text, "Marketplace refund amount"
            ),
            marketplace_tax_amount=_metric_text_or_default(
                text, "Marketplace tax amount"
            ),
            marketplace_selling_fees=_metric_text_or_default(
                text, "Marketplace selling fees"
            ),
            marketplace_fba_fees=_metric_text_or_default(
                text, "Marketplace FBA fees"
            ),
            marketplace_other_fees=_metric_text_or_default(
                text, "Marketplace other fees"
            ),
            marketplace_reimbursements=_metric_text_or_default(
                text, "Marketplace reimbursements"
            ),
            marketplace_withheld_tax=_metric_text_or_default(
                text, "Marketplace withheld tax"
            ),
            marketplace_failed_transfers=_metric_text_or_default(
                text, "Marketplace failed transfers"
            ),
            marketplace_reserve_adjustments=_metric_text_or_default(
                text, "Marketplace reserve adjustments"
            ),
            marketplace_uncategorised=_metric_text_or_default(
                text, "Marketplace uncategorised"
            ),
            marketplace_deferred_amount=_metric_text_or_default(
                text, "Marketplace deferred amount"
            ),
            marketplace_released_amount=_metric_text_or_default(
                text, "Marketplace released amount"
            ),
            total_bank_payment_receipts=_metric_text(
                text, "Total bank/payment receipts"
            ),
            matched_amount=_metric_text(text, "Matched amount"),
            unmatched_expected_amount=_metric_text(text, "Unmatched expected amount"),
            unmatched_bank_payment_receipt_amount=_metric_text(
                text, "Unmatched bank/payment receipt amount"
            ),
        )

def _read_count_csv(path: Path) -> dict[str, int]:
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return {row["exception_type"]: int(row["count"]) for row in reader}


def _metric_int(text: str, label: str) -> int:
    return int(_metric_text(text, label))


def _metric_int_or_default(text: str, label: str, default: int = 0) -> int:
    try:
        return int(_metric_text(text, label))
    except ValueError:
        return default


def _metric_text_or_default(
    text: str, label: str, default: str = "0.00"
) -> str:
    try:
        return _metric_text(text, label)
    except ValueError:
        return default


def _metric_text(text: str, label: str) -> str:
    match = re.search(rf"^- {re.escape(label)}: (.+)$", text, re.MULTILINE)
    if not match:
        raise ValueError(f"Metric not found in summary: {label}")
    return match.group(1).strip()


def _validate_workspace_identity(tenant: TenantConfig) -> None:
    marketplace_labels = _marketplace_account_labels(tenant)
    identity_values = [
        ("tenant name", tenant.tenant_name),
        ("primary entity", tenant.poc_entity),
    ]
    identity_values.extend(
        ("entity", entity.name) for entity in tenant.client_setup.entities
    )
    for field_name, value in identity_values:
        normalized_value = _normalize_identity(value)
        for label in marketplace_labels:
            if label and label in normalized_value:
                raise ValueError(
                    f"{field_name} cannot be a marketplace account. "
                    "Marketplace account names belong under Marketplace Report Sources."
                )


def _marketplace_account_labels(tenant: TenantConfig) -> set[str]:
    labels: set[str] = set()
    for source in tenant.sources.marketplaces:
        source_name = source.name or ""
        if " - " in source_name:
            labels.add(_normalize_identity(source_name.rsplit(" - ", 1)[1]))
        labels.add(_normalize_identity(source_name))
    return {label for label in labels if label}


def _normalize_identity(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _copy_limited(source, target, max_bytes: int) -> None:
    total = 0
    while True:
        chunk = source.read(1024 * 1024)
        if not chunk:
            return
        total += len(chunk)
        if total > max_bytes:
            raise OverflowError("Uploaded file exceeds the configured size limit.")
        target.write(chunk)


def _count_csv_files(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(
        1 for item in path.rglob("*")
        if item.is_file() and item.suffix.lower() in (".csv", ".txt")
    )


def _count_manifest_rows(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open(newline="", encoding="utf-8") as handle:
        return sum(1 for _ in csv.DictReader(handle))


def _has_placeholder_outflows(tenant: TenantConfig) -> bool:
    placeholders = {"", "to be confirmed", "tbc", "pending", "unknown"}
    return any(
        outflow.amount.strip().lower() in placeholders
        for outflow in tenant.client_setup.planned_outflows
    )


def _has_configured_api_connector(tenant: TenantConfig) -> bool:
    for source in tenant.sources.marketplaces:
        source_type = source.type.lower()
        if "api" not in source_type:
            continue
        if source.api_endpoint.strip() and source.credential_reference.strip():
            return True
    return False


def _readiness_check(
    name: str,
    passed: bool,
    passed_detail: str,
    failed_detail: str,
) -> dict[str, str]:
    return {
        "name": name,
        "status": "passed" if passed else "blocked",
        "detail": passed_detail if passed else failed_detail,
    }


def _readiness_status(
    blocking: list[dict[str, str]],
    warning: list[dict[str, str]],
) -> str:
    if not blocking and not warning:
        return "live_ready"
    if not blocking:
        return "partially_configured"
    return "configuration_required"


def _deferred_check(name: str, detail: str) -> dict[str, str]:
    return {
        "name": name,
        "status": "deferred",
        "detail": detail,
    }


def _audit_check(name: str, computed: Decimal, reported: Decimal) -> dict[str, str]:
    computed = computed.quantize(Decimal("0.01"))
    reported = reported.quantize(Decimal("0.01"))
    passed = computed == reported
    return {
        "name": name,
        "status": "passed" if passed else "failed",
        "computed": str(computed),
        "reported": str(reported),
    }


def _sum_decimal(rows: list[dict[str, str]], key: str) -> Decimal:
    total = Decimal("0")
    for row in rows:
        total += _money_decimal(row.get(key, "0"))
    return total


def _money_decimal(value: str) -> Decimal:
    cleaned = str(value or "0").replace(",", "").strip()
    if cleaned.lower() in {"disabled", "pending", "bank feed pending"}:
        return Decimal("0")
    try:
        return Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError(f"Cannot parse numeric value: {value}") from exc


# ---------------------------------------------------------------------------
# API ingest helpers
# ---------------------------------------------------------------------------

# Loose field aliases: what an external API might send → canonical column name
_AMAZON_ALIASES: dict[str, str] = {
    "date_time": "date/time",
    "date/time": "date/time",
    "posted_at": "date/time",
    "timestamp": "date/time",
    "transaction_time": "date/time",
    "settlement_id": "settlement id",
    "settlement id": "settlement id",
    "type": "type",
    "transaction_type": "type",
    "order_id": "order id",
    "order id": "order id",
    "sku": "sku",
    "asin": "sku",
    "description": "description",
    "marketplace": "marketplace",
    "fulfillment": "fulfillment",
    "fulfillment_channel": "fulfillment",
    "product_sales": "product sales",
    "product sales": "product sales",
    "revenue": "product sales",
    "selling_fees": "selling fees",
    "selling fees": "selling fees",
    "fba_fees": "fba fees",
    "fba fees": "fba fees",
    "other_transaction_fees": "other transaction fees",
    "other transaction fees": "other transaction fees",
    "other": "other",
    "total": "total",
    "net_amount": "total",
    "amount": "total",
    "transaction_status": "Transaction status",
    "Transaction status": "Transaction status",
    "status": "Transaction status",
    "release_date": "Transaction Release Date",
    "Transaction Release Date": "Transaction Release Date",
}

_PAYOUT_ALIASES: dict[str, str] = {
    "id": "payout_id",
    "payout_id": "payout_id",
    "settlement_id": "payout_id",
    "entity_name": "entity",
    "entity": "entity",
    "company": "entity",
    "source": "marketplace",
    "marketplace": "marketplace",
    "account": "marketplace",
    "ccy": "currency",
    "currency": "currency",
    "date": "expected_date",
    "expected_date": "expected_date",
    "payout_date": "expected_date",
    "settlement_date": "expected_date",
    "value": "amount",
    "amount": "amount",
    "net_amount": "amount",
    "total": "amount",
    "ref": "reference",
    "reference": "reference",
    "order_id": "reference",
}

_RECEIPT_ALIASES: dict[str, str] = {
    "id": "receipt_id",
    "receipt_id": "receipt_id",
    "transaction_id": "receipt_id",
    "bank_reference": "receipt_id",
    "account": "bank_account",
    "bank_account": "bank_account",
    "bank": "bank_account",
    "account_name": "bank_account",
    "ccy": "currency",
    "currency": "currency",
    "date": "receipt_date",
    "receipt_date": "receipt_date",
    "transaction_date": "receipt_date",
    "value_date": "receipt_date",
    "value": "amount",
    "amount": "amount",
    "net_amount": "amount",
    "credit_amount": "amount",
    "ref": "reference",
    "reference": "reference",
    "description": "reference",
    "narration": "reference",
}

_API_TYPE_CONFIG: dict[str, tuple[str, dict[str, str]]] = {
    "marketplace_ar": (
        "marketplace_ar",
        {field: field for field in MARKETPLACE_AR_FIELDS},
    ),
    "transactions": ("marketplaces/api", _AMAZON_ALIASES),
    "amazon_transactions": ("marketplaces/api", _AMAZON_ALIASES),
    "settlements": ("marketplaces/portal_reports", _PAYOUT_ALIASES),
    "payouts": ("marketplaces", _PAYOUT_ALIASES),
    "expected_payouts": ("marketplaces", _PAYOUT_ALIASES),
    "bank_receipts": ("banks", _RECEIPT_ALIASES),
    "receipts": ("banks", _RECEIPT_ALIASES),
    "bank": ("banks", _RECEIPT_ALIASES),
}


def _normalize_api_row(row: dict, aliases: dict[str, str]) -> dict[str, str]:
    """Remap row keys using alias table; pass through unrecognised keys as-is."""
    result: dict[str, str] = {}
    for key, value in row.items():
        canonical = aliases.get(key) or aliases.get(key.lower()) or key
        result[canonical] = str(value) if value is not None else ""
    return result
