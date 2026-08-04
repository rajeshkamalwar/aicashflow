"""Encrypted, backend-managed external integration settings."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
import hashlib
import json
import logging
import os
import re
import sqlite3
from typing import Any, Mapping
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken

from .amazon_sp_api import AmazonSpApiConfig


AMAZON_SP_API_ENDPOINTS = frozenset(
    {
        "https://sellingpartnerapi-na.amazon.com",
        "https://sellingpartnerapi-eu.amazon.com",
        "https://sellingpartnerapi-fe.amazon.com",
    }
)
DEFAULT_AMAZON_ENDPOINT = "https://sellingpartnerapi-na.amazon.com"
LOGGER = logging.getLogger(__name__)
TRANSACTION_STATUSES = ("DEFERRED", "DEFERRED_RELEASED", "RELEASED")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AmazonIntegrationManager:
    """Owns the singleton Amazon configuration without exposing its secrets."""

    def __init__(self, database_path: str | Path, encryption_key: str) -> None:
        if not encryption_key:
            raise ValueError("AI_CASHFLOW_MASTER_KEY is required.")
        try:
            self._cipher = Fernet(encryption_key.encode())
        except (TypeError, ValueError) as exc:
            raise ValueError("AI_CASHFLOW_MASTER_KEY is invalid.") from exc
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS amazon_integration (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    application_id TEXT NOT NULL DEFAULT '',
                    endpoint TEXT NOT NULL,
                    marketplace TEXT NOT NULL DEFAULT 'Canada',
                    sync_frequency_minutes INTEGER NOT NULL DEFAULT 60,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    client_id_encrypted TEXT,
                    client_secret_encrypted TEXT,
                    refresh_token_encrypted TEXT,
                    status TEXT NOT NULL DEFAULT 'Not configured',
                    last_tested_at TEXT,
                    last_sync_at TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            now = _utc_now()
            connection.execute(
                """
                INSERT OR IGNORE INTO amazon_integration
                    (id, endpoint, created_at, updated_at)
                VALUES (1, ?, ?, ?)
                """,
                (DEFAULT_AMAZON_ENDPOINT, now, now),
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS amazon_reports (
                    report_id TEXT PRIMARY KEY,
                    report_document_id TEXT NOT NULL,
                    created_time TEXT,
                    cache_path TEXT NOT NULL,
                    ingested_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS amazon_sync_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reports_found INTEGER NOT NULL,
                    reports_ingested INTEGER NOT NULL,
                    message TEXT NOT NULL
                )
                """
            )

    @staticmethod
    def _validate_endpoint(endpoint: str) -> str:
        normalized = endpoint.strip().rstrip("/")
        if normalized not in AMAZON_SP_API_ENDPOINTS:
            raise ValueError("Use an official Amazon SP-API endpoint.")
        return normalized

    def _encrypt(self, value: str) -> str:
        return self._cipher.encrypt(value.encode()).decode()

    def _decrypt(self, value: str | None, label: str) -> str:
        if not value:
            return ""
        try:
            return self._cipher.decrypt(value.encode()).decode()
        except InvalidToken as exc:
            raise ValueError(f"Stored Amazon {label} cannot be decrypted with the active master key.") from exc

    def _row(self) -> sqlite3.Row:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM amazon_integration WHERE id = 1").fetchone()
        if row is None:  # pragma: no cover - guarded by initialization
            raise RuntimeError("Amazon integration settings are unavailable.")
        return row

    def save(self, values: Mapping[str, Any]) -> dict[str, Any]:
        current = self._row()
        endpoint = self._validate_endpoint(str(values.get("endpoint", current["endpoint"])))
        frequency = int(values.get("sync_frequency_minutes", current["sync_frequency_minutes"]))
        if frequency < 5 or frequency > 10_080:
            raise ValueError("Sync frequency must be between 5 and 10080 minutes.")

        secret_columns: dict[str, str | None] = {}
        for field, column in (
            ("client_id", "client_id_encrypted"),
            ("client_secret", "client_secret_encrypted"),
            ("refresh_token", "refresh_token_encrypted"),
        ):
            supplied = values.get(field)
            secret_columns[column] = self._encrypt(str(supplied).strip()) if supplied and str(supplied).strip() else current[column]

        enabled = bool(values.get("enabled", current["enabled"]))
        has_all_credentials = all(secret_columns.values())
        status = "Ready" if has_all_credentials else "Authorization pending"
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE amazon_integration SET
                    application_id = ?, endpoint = ?, marketplace = ?,
                    sync_frequency_minutes = ?, enabled = ?,
                    client_id_encrypted = ?, client_secret_encrypted = ?,
                    refresh_token_encrypted = ?, status = ?, last_error = NULL, updated_at = ?
                WHERE id = 1
                """,
                (
                    str(values.get("application_id", current["application_id"])).strip(),
                    endpoint,
                    str(values.get("marketplace", current["marketplace"])).strip() or "Canada",
                    frequency,
                    int(enabled),
                    secret_columns["client_id_encrypted"],
                    secret_columns["client_secret_encrypted"],
                    secret_columns["refresh_token_encrypted"],
                    status,
                    _utc_now(),
                ),
            )
        return self.public()

    def public(self) -> dict[str, Any]:
        row = self._row()
        return {
            "application_id": row["application_id"],
            "endpoint": row["endpoint"],
            "marketplace": row["marketplace"],
            "sync_frequency_minutes": row["sync_frequency_minutes"],
            "enabled": bool(row["enabled"]),
            "status": row["status"],
            "last_tested_at": row["last_tested_at"],
            "last_sync_at": row["last_sync_at"],
            "last_error": row["last_error"],
            "updated_at": row["updated_at"],
            "credentials": {
                "client_id_configured": bool(row["client_id_encrypted"]),
                "client_secret_configured": bool(row["client_secret_encrypted"]),
                "refresh_token_configured": bool(row["refresh_token_encrypted"]),
            },
        }

    def credentials(self) -> AmazonSpApiConfig:
        row = self._row()
        return AmazonSpApiConfig(
            client_id=self._decrypt(row["client_id_encrypted"], "client ID"),
            client_secret=self._decrypt(row["client_secret_encrypted"], "client secret"),
            refresh_token=self._decrypt(row["refresh_token_encrypted"], "refresh token"),
            endpoint=row["endpoint"],
        )

    def delete_credentials(self) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE amazon_integration SET
                    client_id_encrypted = NULL,
                    client_secret_encrypted = NULL,
                    refresh_token_encrypted = NULL,
                    enabled = 0,
                    status = 'Authorization pending',
                    last_error = NULL,
                    updated_at = ?
                WHERE id = 1
                """,
                (_utc_now(),),
            )
        return self.public()

    def sync(self, client: Any, samples_dir: str | Path) -> dict[str, Any]:
        started_at = _utc_now()
        reports = client.list_settlement_reports()
        target_dir = Path(samples_dir) / "marketplaces" / "api"
        target_dir.mkdir(parents=True, exist_ok=True)
        ingested = 0
        for report in reports:
            report_id = str(report.get("reportId", "")).strip()
            document_id = str(report.get("reportDocumentId", "")).strip()
            if not report_id or not document_id or self._report_exists(report_id):
                continue
            safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", report_id)[:120]
            target = target_dir / f"amazon_sp_api_{safe_id}.txt"
            body = client.download_report_document(document_id)
            if "settlement-id" not in body.lower() or "settlement-start-date" not in body.lower():
                raise ValueError(f"Amazon report {report_id} is not a Flat File V2 settlement report.")
            temporary = target.with_suffix(".tmp")
            temporary.write_text(body, encoding="utf-8")
            temporary.replace(target)
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO amazon_reports
                        (report_id, report_document_id, created_time, cache_path, ingested_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (report_id, document_id, str(report.get("createdTime", "")), str(target), _utc_now()),
                )
            ingested += 1
        completed_at = _utc_now()
        result = {
            "status": "completed",
            "reports_found": len(reports),
            "reports_ingested": ingested,
            "started_at": started_at,
            "completed_at": completed_at,
            "message": f"Ingested {ingested} new settlement report(s).",
        }
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO amazon_sync_runs
                    (started_at, completed_at, status, reports_found, reports_ingested, message)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (started_at, completed_at, result["status"], len(reports), ingested, result["message"]),
            )
            connection.execute(
                """
                UPDATE amazon_integration
                SET last_sync_at = ?, last_error = NULL, status = 'Connected', updated_at = ?
                WHERE id = 1
                """,
                (completed_at, completed_at),
            )
        return result

    def _report_exists(self, report_id: str) -> bool:
        with self._connect() as connection:
            return connection.execute(
                "SELECT 1 FROM amazon_reports WHERE report_id = ?", (report_id,)
            ).fetchone() is not None

    def list_syncs(self, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM amazon_sync_runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def is_sync_due(self, now: datetime | None = None) -> bool:
        settings = self.public()
        if not settings["enabled"] or not all(settings["credentials"].values()):
            return False
        last_sync = settings.get("last_sync_at")
        if not last_sync:
            return True
        current = now or datetime.now(timezone.utc)
        previous = datetime.fromisoformat(str(last_sync))
        return current >= previous + timedelta(minutes=int(settings["sync_frequency_minutes"]))

    def record_connection_test(self, *, success: bool, error: str | None = None) -> dict[str, Any]:
        now = _utc_now()
        safe_error = (error or "")[:500] or None
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE amazon_integration
                SET status = ?, last_tested_at = ?, last_error = ?, updated_at = ?
                WHERE id = 1
                """,
                ("Connected" if success else "Connection failed", now, safe_error, now),
            )
        return self.public()

    def record_sync_failure(self, error: str) -> None:
        now = _utc_now()
        safe_error = error[:500]
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO amazon_sync_runs
                    (started_at, completed_at, status, reports_found, reports_ingested, message)
                VALUES (?, ?, 'failed', 0, 0, ?)
                """,
                (now, now, safe_error),
            )
            connection.execute(
                """
                UPDATE amazon_integration
                SET status = 'Sync failed', last_error = ?, updated_at = ?
                WHERE id = 1
                """,
                (safe_error, now),
            )


class AmazonSourceRegistry(AmazonIntegrationManager):
    """Encrypted registry for independently authorized Amazon accounts."""

    def _initialize(self) -> None:
        super()._initialize()
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS amazon_sources (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    seller_id TEXT NOT NULL DEFAULT '',
                    application_id TEXT NOT NULL DEFAULT '',
                    endpoint TEXT NOT NULL,
                    marketplace TEXT NOT NULL DEFAULT '',
                    sync_frequency_minutes INTEGER NOT NULL DEFAULT 60,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    client_id_encrypted TEXT,
                    client_secret_encrypted TEXT,
                    refresh_token_encrypted TEXT,
                    status TEXT NOT NULL DEFAULT 'Not configured',
                    marketplaces_json TEXT NOT NULL DEFAULT '[]',
                    last_tested_at TEXT,
                    last_sync_at TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(amazon_sources)")
            }
            if "seller_id" not in columns:
                connection.execute(
                    "ALTER TABLE amazon_sources ADD COLUMN seller_id TEXT NOT NULL DEFAULT ''"
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS amazon_source_reports (
                    source_id TEXT NOT NULL,
                    report_id TEXT NOT NULL,
                    report_document_id TEXT NOT NULL,
                    created_time TEXT,
                    cache_path TEXT NOT NULL,
                    ingested_at TEXT NOT NULL,
                    PRIMARY KEY (source_id, report_id),
                    FOREIGN KEY (source_id) REFERENCES amazon_sources(id) ON DELETE CASCADE
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS amazon_source_sync_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_id TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reports_found INTEGER NOT NULL,
                    reports_ingested INTEGER NOT NULL,
                    message TEXT NOT NULL,
                    FOREIGN KEY (source_id) REFERENCES amazon_sources(id) ON DELETE CASCADE
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS amazon_transaction_observations (
                    source_id TEXT NOT NULL,
                    marketplace_id TEXT NOT NULL,
                    transaction_id TEXT NOT NULL,
                    retrieved_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    conflict INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (source_id, marketplace_id, transaction_id, retrieved_at, fingerprint),
                    FOREIGN KEY (source_id) REFERENCES amazon_sources(id) ON DELETE CASCADE
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS amazon_transaction_state (
                    source_id TEXT NOT NULL,
                    marketplace_id TEXT NOT NULL,
                    transaction_id TEXT NOT NULL,
                    marketplace_name TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    status TEXT NOT NULL,
                    retrieved_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    conflict INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (source_id, marketplace_id, transaction_id),
                    FOREIGN KEY (source_id) REFERENCES amazon_sources(id) ON DELETE CASCADE
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS amazon_transaction_checkpoints (
                    source_id TEXT NOT NULL,
                    marketplace_id TEXT NOT NULL,
                    marketplace_name TEXT NOT NULL DEFAULT '',
                    currency TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    coverage_start TEXT NOT NULL,
                    coverage_end TEXT NOT NULL,
                    retrieved_at TEXT NOT NULL,
                    pagination_complete INTEGER NOT NULL,
                    last_error TEXT,
                    PRIMARY KEY (source_id, marketplace_id, status),
                    FOREIGN KEY (source_id) REFERENCES amazon_sources(id) ON DELETE CASCADE
                )
                """
            )

    def _source_row(self, source_id: str) -> sqlite3.Row:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM amazon_sources WHERE id = ?", (source_id,)
            ).fetchone()
        if row is None:
            raise ValueError("Amazon source not found.")
        return row

    def upsert(
        self, values: Mapping[str, Any], source_id: str | None = None
    ) -> dict[str, Any]:
        name = str(values.get("name", "")).strip()
        if not name or len(name) > 120:
            raise ValueError("Amazon source name is required and must be 120 characters or fewer.")
        current = None
        with self._connect() as connection:
            if source_id:
                current = connection.execute(
                    "SELECT * FROM amazon_sources WHERE id = ?", (source_id,)
                ).fetchone()
            else:
                current = connection.execute(
                    "SELECT * FROM amazon_sources WHERE name = ?", (name,)
                ).fetchone()
        if source_id and current is None:
            raise ValueError("Amazon source not found.")
        source_id = str(current["id"]) if current else uuid4().hex
        endpoint = self._validate_endpoint(
            str(values.get("endpoint", current["endpoint"] if current else DEFAULT_AMAZON_ENDPOINT))
        )
        frequency = int(values.get(
            "sync_frequency_minutes", current["sync_frequency_minutes"] if current else 60
        ))
        if frequency < 5 or frequency > 10_080:
            raise ValueError("Sync frequency must be between 5 and 10080 minutes.")
        secrets = {}
        for field, column in (
            ("client_id", "client_id_encrypted"),
            ("client_secret", "client_secret_encrypted"),
            ("refresh_token", "refresh_token_encrypted"),
        ):
            value = str(values.get(field) or "").strip()
            secrets[column] = self._encrypt(value) if value else (current[column] if current else None)
        status = "Ready" if all(secrets.values()) else "Authorization pending"
        now = _utc_now()
        record = (
            source_id,
            name,
            str(values.get("application_id", current["application_id"] if current else "")).strip(),
            endpoint,
            str(values.get("marketplace", current["marketplace"] if current else "")).strip(),
            frequency,
            int(bool(values.get("enabled", current["enabled"] if current else False))),
            secrets["client_id_encrypted"],
            secrets["client_secret_encrypted"],
            secrets["refresh_token_encrypted"],
            status,
            current["marketplaces_json"] if current else "[]",
            current["last_tested_at"] if current else None,
            current["last_sync_at"] if current else None,
            None,
            current["created_at"] if current else now,
            now,
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO amazon_sources (
                    id, name, application_id, endpoint, marketplace,
                    sync_frequency_minutes, enabled, client_id_encrypted,
                    client_secret_encrypted, refresh_token_encrypted, status,
                    marketplaces_json, last_tested_at, last_sync_at, last_error,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name, application_id=excluded.application_id,
                    endpoint=excluded.endpoint, marketplace=excluded.marketplace,
                    sync_frequency_minutes=excluded.sync_frequency_minutes,
                    enabled=excluded.enabled,
                    client_id_encrypted=excluded.client_id_encrypted,
                    client_secret_encrypted=excluded.client_secret_encrypted,
                    refresh_token_encrypted=excluded.refresh_token_encrypted,
                    status=excluded.status, last_error=NULL, updated_at=excluded.updated_at
                """,
                record,
            )
        return self.get_source(source_id)

    def _public_source(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "name": row["name"],
            "seller_id": row["seller_id"],
            "application_id": row["application_id"],
            "endpoint": row["endpoint"],
            "marketplace": row["marketplace"],
            "sync_frequency_minutes": row["sync_frequency_minutes"],
            "enabled": bool(row["enabled"]),
            "status": row["status"],
            "marketplaces": json.loads(row["marketplaces_json"]),
            "last_tested_at": row["last_tested_at"],
            "last_sync_at": row["last_sync_at"],
            "last_error": row["last_error"],
            "updated_at": row["updated_at"],
            "credentials": {
                "client_id_configured": bool(row["client_id_encrypted"]),
                "client_secret_configured": bool(row["client_secret_encrypted"]),
                "refresh_token_configured": bool(row["refresh_token_encrypted"]),
            },
        }

    def get_source(self, source_id: str) -> dict[str, Any]:
        return self._public_source(self._source_row(source_id))

    def list_sources(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM amazon_sources ORDER BY name COLLATE NOCASE"
            ).fetchall()
        return [self._public_source(row) for row in rows]

    def credentials(self, source_id: str) -> AmazonSpApiConfig:
        row = self._source_row(source_id)
        return AmazonSpApiConfig(
            client_id=self._decrypt(row["client_id_encrypted"], "client ID"),
            client_secret=self._decrypt(row["client_secret_encrypted"], "client secret"),
            refresh_token=self._decrypt(row["refresh_token_encrypted"], "refresh token"),
            endpoint=row["endpoint"],
        )

    def record_connection_test(
        self,
        source_id: str,
        transactions: list[dict[str, object]] | None = None,
        *,
        error: str | None = None,
    ) -> dict[str, Any]:
        now = _utc_now()
        current = self._source_row(source_id)
        safe_marketplaces = json.loads(current["marketplaces_json"])
        discovered = {}
        seller_ids = set()
        for row in transactions or []:
            marketplace = row.get("marketplaceDetails") if isinstance(row, dict) else None
            metadata = row.get("sellingPartnerMetadata") if isinstance(row, dict) else None
            amount = row.get("totalAmount") if isinstance(row, dict) else None
            if not isinstance(marketplace, dict):
                marketplace = {}
            if not isinstance(metadata, dict):
                metadata = {}
            seller_id = str(metadata.get("sellingPartnerId") or "").strip()[:64]
            if seller_id:
                seller_ids.add(seller_id)
            marketplace_id = str(
                marketplace.get("marketplaceId") or metadata.get("marketplaceId") or ""
            )[:40]
            if not marketplace_id:
                continue
            discovered[marketplace_id] = {
                "id": marketplace_id,
                "country_code": "",
                "name": str(marketplace.get("marketplaceName") or marketplace_id)[:120],
                "currency": str(amount.get("currencyCode", ""))[:3]
                if isinstance(amount, dict) else "",
                "is_participating": True,
                "has_suspended_listings": False,
            }
        if discovered:
            safe_marketplaces = list(discovered.values())
        if len(seller_ids) > 1:
            raise ValueError("Amazon returned multiple seller accounts for one source.")
        seller_id = next(iter(seller_ids), str(current["seller_id"] or ""))
        if current["seller_id"] and seller_id != current["seller_id"]:
            raise ValueError("The credentials belong to a different Amazon seller account.")
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE amazon_sources SET status = ?, seller_id = ?, marketplaces_json = ?,
                    last_tested_at = ?, last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    "Connection failed" if error else "Connected",
                    seller_id,
                    json.dumps(safe_marketplaces),
                    now,
                    (error or "")[:500] or None,
                    now,
                    source_id,
                ),
            )
        return self.get_source(source_id)

    @staticmethod
    def _transaction_fingerprint(payload: Mapping[str, object]) -> tuple[str, str]:
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return serialized, hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    @staticmethod
    def _money(payload: Mapping[str, object]) -> Decimal:
        amount = payload.get("currencyAmount")
        try:
            return Decimal(str(amount if amount is not None else "0"))
        except InvalidOperation:
            return Decimal("0")

    @classmethod
    def _composition(cls, payload: Mapping[str, object]) -> dict[str, Decimal]:
        totals: dict[str, Decimal] = {}

        def visit(nodes: object, transaction_type: str) -> None:
            if not isinstance(nodes, list):
                return
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                children = node.get("breakdowns")
                if isinstance(children, list) and children:
                    visit(children, transaction_type)
                    continue
                kind = f"{transaction_type} {node.get('breakdownType', '')}".lower()
                if "refund" in kind:
                    bucket = "Refunds"
                elif "chargeback" in kind:
                    bucket = "Chargebacks"
                elif "fba" in kind or "fulfillment" in kind:
                    bucket = "FBA fees"
                elif "tax" in kind:
                    bucket = "Taxes"
                elif "fee" in kind or "commission" in kind:
                    bucket = "Amazon fees"
                elif "adjust" in kind:
                    bucket = "Adjustments"
                elif any(word in kind for word in ("principal", "product", "sale", "shipment")):
                    bucket = "Product sales"
                else:
                    bucket = "Other"
                amount = node.get("breakdownAmount")
                if isinstance(amount, dict):
                    totals[bucket] = totals.get(bucket, Decimal("0")) + cls._money(amount)

        transaction_type = str(payload.get("transactionType") or "")
        visit(payload.get("breakdowns"), transaction_type)
        for item in payload.get("items", []) if isinstance(payload.get("items"), list) else []:
            if isinstance(item, dict):
                visit(item.get("breakdowns"), transaction_type)
        return totals

    def record_transaction_snapshot(
        self,
        source_id: str,
        marketplace_id: str,
        marketplace_name: str,
        currency: str,
        requested_status: str,
        transactions: list[dict[str, object]],
        retrieved_at: datetime,
        *,
        coverage_start: datetime | None = None,
        coverage_end: datetime | None = None,
        pagination_complete: bool = True,
    ) -> None:
        """Atomically append observations and advance deterministic current state."""
        if requested_status not in TRANSACTION_STATUSES:
            raise ValueError(f"Unsupported Amazon transaction status: {requested_status}.")
        retrieved_at = retrieved_at.astimezone(timezone.utc)
        retrieved_text = retrieved_at.isoformat()
        start = coverage_start or retrieved_at - timedelta(days=179)
        end = coverage_end or retrieved_at
        ranks = {"DEFERRED": 0, "RELEASED": 1, "DEFERRED_RELEASED": 2}
        observed_ids: set[str] = set()
        with self._connect() as connection:
            for payload in transactions:
                transaction_id = str(payload.get("transactionId") or "").strip()
                if not transaction_id:
                    continue
                observed_ids.add(transaction_id)
                status = str(payload.get("transactionStatus") or requested_status).upper()
                total = payload.get("totalAmount")
                row_currency = str(
                    (total.get("currencyCode") or currency) if isinstance(total, dict) else currency
                ).upper()
                serialized, fingerprint = self._transaction_fingerprint(payload)
                current = connection.execute(
                    """SELECT * FROM amazon_transaction_state
                       WHERE source_id=? AND marketplace_id=? AND transaction_id=?""",
                    (source_id, marketplace_id, transaction_id),
                ).fetchone()
                regressed = bool(current and current["status"] in {"RELEASED", "DEFERRED_RELEASED"} and status == "DEFERRED")
                connection.execute(
                    """INSERT OR IGNORE INTO amazon_transaction_observations
                       (source_id, marketplace_id, transaction_id, retrieved_at, status,
                        currency, payload_json, fingerprint, conflict)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (source_id, marketplace_id, transaction_id, retrieved_text, status,
                     row_currency, serialized, fingerprint, int(regressed)),
                )
                candidate_key = (retrieved_text, ranks.get(status, -1), fingerprint)
                current_key = (
                    (current["retrieved_at"], ranks.get(current["status"], -1), current["fingerprint"])
                    if current else ("", -1, "")
                )
                if regressed:
                    connection.execute(
                        """UPDATE amazon_transaction_state SET conflict=1
                           WHERE source_id=? AND marketplace_id=? AND transaction_id=?""",
                        (source_id, marketplace_id, transaction_id),
                    )
                    LOGGER.warning(
                        "amazon_transaction_state_conflict source=%s marketplace=%s transaction=%s current=%s observed=%s",
                        source_id, marketplace_id, transaction_id[-8:], current["status"], status,
                    )
                elif candidate_key > current_key:
                    connection.execute(
                        """INSERT INTO amazon_transaction_state
                           (source_id, marketplace_id, transaction_id, marketplace_name, currency,
                            status, retrieved_at, payload_json, fingerprint, conflict)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                           ON CONFLICT(source_id, marketplace_id, transaction_id) DO UPDATE SET
                             marketplace_name=excluded.marketplace_name, currency=excluded.currency,
                             status=excluded.status, retrieved_at=excluded.retrieved_at,
                             payload_json=excluded.payload_json, fingerprint=excluded.fingerprint""",
                        (source_id, marketplace_id, transaction_id, marketplace_name, row_currency,
                         status, retrieved_text, serialized, fingerprint),
                    )
            stale_params: list[object] = [source_id, marketplace_id, requested_status, retrieved_text]
            stale_sql = (
                "DELETE FROM amazon_transaction_state WHERE source_id=? AND marketplace_id=? "
                "AND status=? AND retrieved_at<?"
            )
            if observed_ids:
                stale_sql += f" AND transaction_id NOT IN ({','.join('?' for _ in observed_ids)})"
                stale_params.extend(sorted(observed_ids))
            connection.execute(stale_sql, stale_params)
            connection.execute(
                """INSERT INTO amazon_transaction_checkpoints
                   (source_id, marketplace_id, marketplace_name, currency, status, coverage_start, coverage_end,
                    retrieved_at, pagination_complete, last_error)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                   ON CONFLICT(source_id, marketplace_id, status) DO UPDATE SET
                     marketplace_name=excluded.marketplace_name, currency=excluded.currency,
                     coverage_start=excluded.coverage_start, coverage_end=excluded.coverage_end,
                     retrieved_at=excluded.retrieved_at,
                     pagination_complete=excluded.pagination_complete, last_error=NULL""",
                (source_id, marketplace_id, marketplace_name, currency, requested_status, start.isoformat(), end.isoformat(),
                 retrieved_text, int(pagination_complete)),
            )

    def record_transaction_sync_failure(
        self, source_id: str, marketplace_id: str, status: str, message: str,
        *, marketplace_name: str = "", currency: str = "",
    ) -> None:
        now = _utc_now()
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO amazon_transaction_checkpoints
                   (source_id, marketplace_id, marketplace_name, currency, status, coverage_start, coverage_end,
                    retrieved_at, pagination_complete, last_error)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                   ON CONFLICT(source_id, marketplace_id, status) DO UPDATE SET
                     last_error=excluded.last_error""",
                (source_id, marketplace_id, marketplace_name, currency, status, now, now, now,
                 (message or "Amazon transaction retrieval failed.")[:300]),
            )

    def transaction_visibility(
        self, source_id: str, currency: str | None = None
    ) -> dict[str, Any]:
        requested = str(currency or "").upper()
        where = "source_id=?" + (" AND currency=?" if requested else "")
        params: tuple[object, ...] = (source_id, requested) if requested else (source_id,)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM amazon_transaction_state WHERE {where}", params
            ).fetchall()
            observation_count = connection.execute(
                f"SELECT COUNT(*) FROM amazon_transaction_observations WHERE {where}", params
            ).fetchone()[0]
            checkpoints = connection.execute(
                "SELECT * FROM amazon_transaction_checkpoints WHERE source_id=?",
                (source_id,),
            ).fetchall()

        def summarize(selected: list[sqlite3.Row], selected_currency: str) -> dict[str, Any]:
            amounts = {status: Decimal("0") for status in TRANSACTION_STATUSES}
            counts = {status: 0 for status in TRANSACTION_STATUSES}
            composition: dict[str, Decimal] = {}
            for row in selected:
                payload = json.loads(row["payload_json"])
                status = row["status"]
                total = payload.get("totalAmount")
                if status in amounts:
                    counts[status] += 1
                    if isinstance(total, dict):
                        amounts[status] += self._money(total)
                for bucket, amount in self._composition(payload).items():
                    composition[bucket] = composition.get(bucket, Decimal("0")) + amount
            relevant_checkpoints = [row for row in checkpoints if row["currency"] == selected_currency]
            return {
                "classification": "AMAZON_TRANSACTION_DERIVED",
                "currency": selected_currency,
                "marketplaces": sorted({
                    row["marketplace_name"] for row in relevant_checkpoints if row["marketplace_name"]
                }),
                "count": len(selected),
                "deferred_count": counts["DEFERRED"],
                "deferred_amount": str(amounts["DEFERRED"]),
                "released_count": counts["RELEASED"],
                "released_amount": str(amounts["RELEASED"]),
                "deferred_released_count": counts["DEFERRED_RELEASED"],
                "deferred_released_amount": str(amounts["DEFERRED_RELEASED"]),
                "conflict_count": sum(int(row["conflict"]) for row in selected),
                "composition": {key: str(value) for key, value in composition.items() if value},
                "coverage_start": min((row["coverage_start"] for row in relevant_checkpoints), default=None),
                "coverage_end": max((row["coverage_end"] for row in relevant_checkpoints), default=None),
                "retrieved_at": max((row["retrieved_at"] for row in relevant_checkpoints), default=None),
                "pagination_complete": bool(relevant_checkpoints) and all(row["pagination_complete"] for row in relevant_checkpoints),
                "partial_failure": any(bool(row["last_error"]) for row in relevant_checkpoints),
            }

        if requested:
            result = summarize(rows, requested)
            result.update({"currency": requested, "observation_count": observation_count})
            return result
        currencies = sorted(
            {row["currency"] for row in rows} |
            {row["currency"] for row in checkpoints if row["currency"]}
        )
        return {
            "classification": "AMAZON_TRANSACTION_DERIVED",
            "observation_count": observation_count,
            "by_currency": {
                code: summarize([row for row in rows if row["currency"] == code], code)
                for code in currencies
            },
        }

    def transaction_reconciliation_report(self, source_id: str) -> dict[str, Any]:
        """Internal-only trend evidence; three retrieval snapshots are required."""
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT retrieved_at, currency, status, payload_json
                   FROM amazon_transaction_observations
                   WHERE source_id=? ORDER BY retrieved_at""",
                (source_id,),
            ).fetchall()
        snapshots: dict[str, dict[str, Decimal]] = {}
        for row in rows:
            payload = json.loads(row["payload_json"])
            total = payload.get("totalAmount")
            if not isinstance(total, dict):
                continue
            key = f"{row['currency']}:{row['status']}"
            snapshot = snapshots.setdefault(row["retrieved_at"], {})
            snapshot[key] = snapshot.get(key, Decimal("0")) + self._money(total)
        if len(snapshots) < 3:
            return {
                "status": "insufficient_history",
                "snapshot_count": len(snapshots),
                "required_snapshot_count": 3,
                "snapshots": [],
            }
        return {
            "status": "ready",
            "snapshot_count": len(snapshots),
            "required_snapshot_count": 3,
            "snapshots": [
                {
                    "retrieved_at": retrieved_at,
                    "totals": {key: str(amount) for key, amount in sorted(totals.items())},
                }
                for retrieved_at, totals in snapshots.items()
            ],
        }

    def sync_source(
        self, source_id: str, client: Any, samples_dir: str | Path
    ) -> dict[str, Any]:
        source = self.get_source(source_id)
        started_at = _utc_now()
        reports = client.list_settlement_reports()
        target_dir = Path(samples_dir) / "marketplaces" / "api" / source_id
        target_dir.mkdir(parents=True, exist_ok=True)
        ingested = 0
        for report in reports:
            report_id = str(report.get("reportId", "")).strip()
            document_id = str(report.get("reportDocumentId", "")).strip()
            if not report_id or not document_id or self._source_report_exists(source_id, report_id):
                continue
            safe_report_id = re.sub(r"[^A-Za-z0-9_-]", "_", report_id)[:120]
            target = target_dir / f"amazon_sp_api_{safe_report_id}.txt"
            body = client.download_report_document(document_id)
            if "settlement-id" not in body.lower() or "settlement-start-date" not in body.lower():
                raise ValueError(f"Amazon report {report_id} is not a Flat File V2 settlement report.")
            temporary = target.with_suffix(".tmp")
            temporary.write_text(body, encoding="utf-8")
            temporary.replace(target)
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO amazon_source_reports
                        (source_id, report_id, report_document_id, created_time, cache_path, ingested_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source_id, report_id, document_id,
                        str(report.get("createdTime", "")), str(target), _utc_now(),
                    ),
                )
            ingested += 1
        transaction_errors = 0
        transaction_rows = 0
        list_by_status = getattr(client, "list_transactions_by_status", None)
        visibility_enabled = os.getenv(
            "AI_CASHFLOW_TRANSACTION_VISIBILITY_ENABLED", "false"
        ).strip().lower() in {"1", "true", "yes", "on"}
        if visibility_enabled and callable(list_by_status):
            retrieved = datetime.now(timezone.utc)
            coverage_end = retrieved - timedelta(minutes=2)
            coverage_start = coverage_end - timedelta(days=179)
            marketplaces = [
                row for row in source.get("marketplaces", [])
                if row.get("is_participating") and str(row.get("name", "")).lower().startswith("amazon")
            ]
            for marketplace in marketplaces:
                marketplace_id = str(marketplace.get("id") or "")
                if not marketplace_id:
                    continue
                for status in TRANSACTION_STATUSES:
                    try:
                        rows = list_by_status(marketplace_id, status, coverage_start)
                        self.record_transaction_snapshot(
                            source_id, marketplace_id, str(marketplace.get("name") or marketplace_id),
                            str(marketplace.get("currency") or "").upper(), status, rows, retrieved,
                            coverage_start=coverage_start, coverage_end=coverage_end,
                        )
                        transaction_rows += len(rows)
                    except Exception as exc:  # each marketplace/status keeps its last good snapshot
                        transaction_errors += 1
                        self.record_transaction_sync_failure(
                            source_id, marketplace_id, status, str(exc),
                            marketplace_name=str(marketplace.get("name") or marketplace_id),
                            currency=str(marketplace.get("currency") or "").upper(),
                        )
                        LOGGER.warning(
                            "amazon_transaction_sync_partial source=%s marketplace=%s status=%s error=%s",
                            source_id, marketplace_id, status, type(exc).__name__,
                        )
        completed_at = _utc_now()
        sync_status = "partial" if transaction_errors else "completed"
        message = (
            f"Ingested {ingested} new settlement report(s); stored {transaction_rows} "
            f"transaction observation(s)."
        )
        if transaction_errors:
            message += f" {transaction_errors} transaction request(s) retained their last successful snapshot."
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO amazon_source_sync_runs
                    (source_id, started_at, completed_at, status, reports_found, reports_ingested, message)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (source_id, started_at, completed_at, sync_status, len(reports), ingested, message),
            )
            connection.execute(
                """
                UPDATE amazon_sources SET status='Connected', last_sync_at=?,
                    last_error=NULL, updated_at=? WHERE id=?
                """,
                (completed_at, completed_at, source_id),
            )
        return {
            "source_id": source_id,
            "status": sync_status,
            "reports_found": len(reports),
            "reports_ingested": ingested,
            "transaction_observations": transaction_rows,
            "transaction_errors": transaction_errors,
            "started_at": started_at,
            "completed_at": completed_at,
            "message": message,
        }

    def _source_report_exists(self, source_id: str, report_id: str) -> bool:
        with self._connect() as connection:
            return connection.execute(
                "SELECT 1 FROM amazon_source_reports WHERE source_id=? AND report_id=?",
                (source_id, report_id),
            ).fetchone() is not None

    def list_reports(self, source_id: str) -> list[dict[str, Any]]:
        self._source_row(source_id)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT report_id, report_document_id, created_time, cache_path, ingested_at
                FROM amazon_source_reports
                WHERE source_id=?
                ORDER BY created_time, report_id
                """,
                (source_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_syncs(self, source_id: str, limit: int = 20) -> list[dict[str, Any]]:
        self._source_row(source_id)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM amazon_source_sync_runs
                WHERE source_id=? ORDER BY id DESC LIMIT ?
                """,
                (source_id, max(1, min(int(limit), 100))),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_sync_failure(self, source_id: str, error: str) -> None:
        self._source_row(source_id)
        now = _utc_now()
        safe_error = error[:500]
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO amazon_source_sync_runs
                    (source_id, started_at, completed_at, status, reports_found, reports_ingested, message)
                VALUES (?, ?, ?, 'failed', 0, 0, ?)
                """,
                (source_id, now, now, safe_error),
            )
            connection.execute(
                """
                UPDATE amazon_sources SET status='Sync failed', last_error=?, updated_at=?
                WHERE id=?
                """,
                (safe_error, now, source_id),
            )

    def due_source_ids(self, now: datetime | None = None) -> list[str]:
        current = now or datetime.now(timezone.utc)
        due = []
        for source in self.list_sources():
            if not source["enabled"] or not all(source["credentials"].values()):
                continue
            last_sync = source["last_sync_at"]
            if not last_sync or current >= datetime.fromisoformat(last_sync) + timedelta(
                minutes=int(source["sync_frequency_minutes"])
            ):
                due.append(source["id"])
        return due

    def delete_source_credentials(self, source_id: str) -> dict[str, Any]:
        self._source_row(source_id)
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE amazon_sources SET client_id_encrypted=NULL,
                    client_secret_encrypted=NULL, refresh_token_encrypted=NULL,
                    enabled=0, status='Authorization pending', marketplaces_json='[]',
                    last_error=NULL, updated_at=? WHERE id=?
                """,
                (_utc_now(), source_id),
            )
        return self.get_source(source_id)
