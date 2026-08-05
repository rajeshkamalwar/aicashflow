"""Encrypted, backend-managed external integration settings."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
import hashlib
import json
import logging
import math
import os
import re
import shutil
import sqlite3
import time
import zlib
from typing import Any, Mapping
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken

from .amazon_sp_api import AmazonSpApiConfig
from .amazon_transaction_state import transaction_timestamp, transition_decision


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
TRANSACTION_JOB_TYPES = (
    "CANARY", "INCREMENTAL_SYNC", "HISTORICAL_BACKFILL", "LEGACY_UNCLASSIFIED",
)


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
            for table, column, definition in (
                ("amazon_transaction_observations", "run_id", "TEXT NOT NULL DEFAULT 'legacy'"),
                ("amazon_transaction_observations", "observation_at", "TEXT"),
                ("amazon_transaction_observations", "previous_status", "TEXT"),
                ("amazon_transaction_observations", "conflict_category", "TEXT"),
                ("amazon_transaction_observations", "included_in_totals", "INTEGER NOT NULL DEFAULT 1"),
                ("amazon_transaction_observations", "resolution_reason", "TEXT"),
                ("amazon_transaction_state", "active_run_id", "TEXT NOT NULL DEFAULT 'legacy'"),
                ("amazon_transaction_state", "observation_at", "TEXT"),
                ("amazon_transaction_state", "conflict_category", "TEXT"),
                ("amazon_transaction_state", "included_in_totals", "INTEGER NOT NULL DEFAULT 1"),
                ("amazon_transaction_state", "resolution_reason", "TEXT"),
                ("amazon_transaction_checkpoints", "successful_run_id", "TEXT NOT NULL DEFAULT 'legacy'"),
            ):
                columns = {
                    row["name"] for row in connection.execute(f"PRAGMA table_info({table})")
                }
                if column not in columns:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            connection.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS amazon_transaction_observation_fingerprint_uq
                   ON amazon_transaction_observations
                   (source_id, marketplace_id, transaction_id, fingerprint)"""
            )
            connection.execute(
                """CREATE INDEX IF NOT EXISTS amazon_transaction_state_scope_idx
                   ON amazon_transaction_state (source_id, marketplace_id, status, currency)"""
            )
            connection.execute(
                """CREATE INDEX IF NOT EXISTS amazon_transaction_observation_retrieved_idx
                   ON amazon_transaction_observations (retrieved_at)"""
            )
            connection.execute(
                """CREATE INDEX IF NOT EXISTS amazon_transaction_observation_status_event_idx
                   ON amazon_transaction_observations
                   (source_id, conflict_category, retrieved_at)"""
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS amazon_transaction_payloads (
                    fingerprint TEXT PRIMARY KEY,
                    payload_zlib BLOB NOT NULL,
                    uncompressed_bytes INTEGER NOT NULL,
                    first_run_id TEXT NOT NULL DEFAULT 'legacy',
                    created_at TEXT NOT NULL
                )
                """
            )
            payload_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(amazon_transaction_payloads)")
            }
            if "first_run_id" not in payload_columns:
                connection.execute(
                    "ALTER TABLE amazon_transaction_payloads ADD COLUMN first_run_id TEXT NOT NULL DEFAULT 'legacy'"
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS amazon_transaction_runs (
                    run_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL CHECK (kind IN ('incremental', 'backfill')),
                    job_type TEXT NOT NULL DEFAULT 'LEGACY_UNCLASSIFIED',
                    parent_job_id TEXT,
                    source_id TEXT NOT NULL,
                    marketplace_id TEXT NOT NULL,
                    marketplace_name TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    transaction_status TEXT NOT NULL,
                    coverage_start TEXT NOT NULL,
                    coverage_end TEXT NOT NULL,
                    next_token TEXT,
                    seen_tokens_json TEXT NOT NULL DEFAULT '[]',
                    pages_completed INTEGER NOT NULL DEFAULT 0,
                    transactions_received INTEGER NOT NULL DEFAULT 0,
                    observations_inserted INTEGER NOT NULL DEFAULT 0,
                    current_state_updated INTEGER NOT NULL DEFAULT 0,
                    duplicates_ignored INTEGER NOT NULL DEFAULT 0,
                    status_conflicts INTEGER NOT NULL DEFAULT 0,
                    pagination_complete INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    last_error TEXT,
                    rate_limit TEXT,
                    throttled_attempts INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    FOREIGN KEY (source_id) REFERENCES amazon_sources(id) ON DELETE CASCADE
                )
                """
            )
            connection.execute(
                """CREATE INDEX IF NOT EXISTS amazon_transaction_runs_scope_idx
                   ON amazon_transaction_runs
                   (source_id, marketplace_id, transaction_status, kind, status, updated_at)"""
            )
            run_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(amazon_transaction_runs)")
            }
            if "throttled_attempts" not in run_columns:
                connection.execute(
                    "ALTER TABLE amazon_transaction_runs ADD COLUMN throttled_attempts INTEGER NOT NULL DEFAULT 0"
                )
            if "job_type" not in run_columns:
                connection.execute(
                    "ALTER TABLE amazon_transaction_runs ADD COLUMN job_type TEXT NOT NULL DEFAULT 'LEGACY_UNCLASSIFIED'"
                )
            if "parent_job_id" not in run_columns:
                connection.execute(
                    "ALTER TABLE amazon_transaction_runs ADD COLUMN parent_job_id TEXT"
                )
            connection.execute(
                """UPDATE amazon_transaction_runs SET job_type='INCREMENTAL_SYNC'
                   WHERE kind='incremental' AND job_type='LEGACY_UNCLASSIFIED'"""
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS amazon_transaction_staging (
                    run_id TEXT NOT NULL,
                    transaction_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    normalized_json TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    raw_payload_zlib BLOB NOT NULL,
                    raw_payload_bytes INTEGER NOT NULL,
                    PRIMARY KEY (run_id, transaction_id, fingerprint),
                    FOREIGN KEY (run_id) REFERENCES amazon_transaction_runs(run_id) ON DELETE CASCADE
                )
                """
            )
            connection.execute(
                """CREATE INDEX IF NOT EXISTS amazon_transaction_staging_run_idx
                   ON amazon_transaction_staging (run_id, status, currency)"""
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS amazon_transaction_backfill_jobs (
                    job_id TEXT PRIMARY KEY,
                    job_type TEXT NOT NULL DEFAULT 'LEGACY_UNCLASSIFIED',
                    source_id TEXT NOT NULL,
                    marketplace_id TEXT NOT NULL,
                    marketplace_name TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    transaction_status TEXT NOT NULL,
                    overall_start TEXT NOT NULL,
                    overall_end TEXT NOT NULL,
                    current_slice_start TEXT NOT NULL,
                    current_slice_end TEXT NOT NULL,
                    slice_hours INTEGER NOT NULL,
                    active_run_id TEXT,
                    next_token TEXT,
                    pages_completed INTEGER NOT NULL DEFAULT 0,
                    transactions_received INTEGER NOT NULL DEFAULT 0,
                    observations_inserted INTEGER NOT NULL DEFAULT 0,
                    current_state_updated INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    last_error TEXT,
                    starting_database_bytes INTEGER NOT NULL DEFAULT 0,
                    status_conflicts INTEGER NOT NULL DEFAULT 0,
                    consecutive_throttles INTEGER NOT NULL DEFAULT 0,
                    pause_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    FOREIGN KEY (source_id) REFERENCES amazon_sources(id) ON DELETE CASCADE
                )
                """
            )
            connection.execute(
                """CREATE INDEX IF NOT EXISTS amazon_transaction_backfill_scope_idx
                   ON amazon_transaction_backfill_jobs
                   (source_id, marketplace_id, transaction_status, status, updated_at)"""
            )
            backfill_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(amazon_transaction_backfill_jobs)")
            }
            for column, definition in (
                ("starting_database_bytes", "INTEGER NOT NULL DEFAULT 0"),
                ("status_conflicts", "INTEGER NOT NULL DEFAULT 0"),
                ("consecutive_throttles", "INTEGER NOT NULL DEFAULT 0"),
                ("pause_reason", "TEXT"),
                ("job_type", "TEXT NOT NULL DEFAULT 'LEGACY_UNCLASSIFIED'"),
            ):
                if column not in backfill_columns:
                    connection.execute(
                        f"ALTER TABLE amazon_transaction_backfill_jobs ADD COLUMN {column} {definition}"
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
    def _normalized_money(value: object) -> dict[str, str]:
        if not isinstance(value, Mapping):
            return {"currencyCode": "", "currencyAmount": "0"}
        currency = str(value.get("currencyCode") or value.get("CurrencyCode") or "").upper()
        raw = value.get("currencyAmount", value.get("CurrencyAmount", "0"))
        try:
            amount = format(Decimal(str(raw)).normalize(), "f")
        except (InvalidOperation, TypeError, ValueError):
            amount = str(raw)
        return {"currencyCode": currency, "currencyAmount": amount}

    @classmethod
    def _normalized_transaction(cls, payload: Mapping[str, object]) -> dict[str, object]:
        def breakdowns(value: object) -> list[dict[str, object]]:
            rows = []
            for row in value if isinstance(value, list) else []:
                if not isinstance(row, Mapping):
                    continue
                normalized = {
                    "breakdownType": str(row.get("breakdownType") or ""),
                    "breakdownAmount": cls._normalized_money(row.get("breakdownAmount")),
                }
                children = breakdowns(row.get("breakdowns"))
                if children:
                    normalized["breakdowns"] = children
                rows.append(normalized)
            return sorted(rows, key=lambda row: json.dumps(row, sort_keys=True))

        identifiers = []
        for row in payload.get("relatedIdentifiers", []) if isinstance(payload.get("relatedIdentifiers"), list) else []:
            if isinstance(row, Mapping):
                identifiers.append({
                    "relatedIdentifierName": str(row.get("relatedIdentifierName") or ""),
                    "relatedIdentifierValue": str(row.get("relatedIdentifierValue") or ""),
                })
        items = []
        for row in payload.get("items", []) if isinstance(payload.get("items"), list) else []:
            if isinstance(row, Mapping):
                items.append({"breakdowns": breakdowns(row.get("breakdowns"))})
        return {
            "transactionId": str(payload.get("transactionId") or ""),
            "transactionStatus": str(payload.get("transactionStatus") or "").upper(),
            "transactionType": str(payload.get("transactionType") or ""),
            "postedDate": str(payload.get("postedDate") or ""),
            "totalAmount": cls._normalized_money(payload.get("totalAmount")),
            "relatedIdentifiers": sorted(
                identifiers,
                key=lambda row: (row["relatedIdentifierName"], row["relatedIdentifierValue"]),
            ),
            "breakdowns": breakdowns(payload.get("breakdowns")),
            "items": items,
        }

    @classmethod
    def _transaction_fingerprint(cls, payload: Mapping[str, object]) -> tuple[str, str]:
        serialized = json.dumps(
            cls._normalized_transaction(payload),
            sort_keys=True,
            separators=(",", ":"),
        )
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

    @staticmethod
    def _utc(value: datetime) -> datetime:
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)

    @staticmethod
    def _public_collection_row(row: sqlite3.Row) -> dict[str, Any]:
        return {key: row[key] for key in row.keys()}

    def _public_backfill_row(self, row: sqlite3.Row) -> dict[str, Any]:
        result = self._public_collection_row(row)
        result["database_growth_bytes"] = max(
            0, self.database_path.stat().st_size - int(row["starting_database_bytes"] or 0)
        )
        return result

    def _create_collection_run(
        self,
        *,
        kind: str,
        job_type: str,
        parent_job_id: str | None = None,
        source_id: str,
        marketplace_id: str,
        marketplace_name: str,
        currency: str,
        transaction_status: str,
        coverage_start: datetime,
        coverage_end: datetime,
    ) -> str:
        if transaction_status not in TRANSACTION_STATUSES:
            raise ValueError(f"Unsupported Amazon transaction status: {transaction_status}.")
        if job_type not in TRANSACTION_JOB_TYPES:
            raise ValueError(f"Unsupported Amazon transaction job type: {job_type}.")
        start, end = self._utc(coverage_start), self._utc(coverage_end)
        if start >= end or end - start > timedelta(days=180):
            raise ValueError("Amazon transaction windows must be positive and no longer than 180 days.")
        run_id = uuid4().hex
        now = _utc_now()
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO amazon_transaction_runs
                   (run_id, kind, job_type, parent_job_id, source_id, marketplace_id, marketplace_name, currency,
                    transaction_status, coverage_start, coverage_end, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?)""",
                (run_id, kind, job_type, parent_job_id, source_id, marketplace_id, marketplace_name, currency,
                 transaction_status, start.isoformat(), end.isoformat(), now, now),
            )
        return run_id

    def _stage_transaction_page(
        self, run_id: str, transactions: list[dict[str, object]], next_token: str | None,
        rate_limit: str | None, throttled_attempts: int = 0,
    ) -> dict[str, int]:
        inserted = 0
        duplicates = 0
        now = _utc_now()
        with self._connect() as connection:
            run = connection.execute(
                "SELECT * FROM amazon_transaction_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if run is None or run["status"] != "running":
                raise ValueError("Transaction collection run is not active.")
            for payload in transactions:
                transaction_id = str(payload.get("transactionId") or "").strip()
                if not transaction_id:
                    duplicates += 1
                    continue
                normalized, fingerprint = self._transaction_fingerprint(payload)
                total = payload.get("totalAmount")
                currency = str(
                    total.get("currencyCode") if isinstance(total, Mapping) else run["currency"]
                ).upper()
                status = str(payload.get("transactionStatus") or run["transaction_status"]).upper()
                raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
                cursor = connection.execute(
                    """INSERT OR IGNORE INTO amazon_transaction_staging
                       (run_id, transaction_id, status, currency, normalized_json, fingerprint,
                        raw_payload_zlib, raw_payload_bytes)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (run_id, transaction_id, status, currency, normalized, fingerprint,
                     zlib.compress(raw), len(raw)),
                )
                inserted += cursor.rowcount
                duplicates += int(cursor.rowcount == 0)
            connection.execute(
                """UPDATE amazon_transaction_runs SET next_token=?, pages_completed=pages_completed+1,
                   transactions_received=transactions_received+?, duplicates_ignored=duplicates_ignored+?,
                   rate_limit=?, throttled_attempts=throttled_attempts+?, updated_at=? WHERE run_id=?""",
                (next_token, len(transactions), duplicates, rate_limit,
                 max(0, int(throttled_attempts)), now, run_id),
            )
        return {"staged": inserted, "duplicates": duplicates}

    def _promote_collection_run(self, run_id: str) -> dict[str, int]:
        """Promote one fully paginated unit and its checkpoint in one SQLite commit."""
        inserted = 0
        updated = 0
        conflicts = 0
        now = _utc_now()
        with self._connect() as connection:
            run = connection.execute(
                "SELECT * FROM amazon_transaction_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if run is None or run["status"] != "running" or run["next_token"]:
                raise ValueError("Only a fully paginated transaction run can be promoted.")
            rows = connection.execute(
                "SELECT * FROM amazon_transaction_staging WHERE run_id=? ORDER BY transaction_id, fingerprint",
                (run_id,),
            ).fetchall()
            for row in rows:
                connection.execute(
                    """INSERT OR IGNORE INTO amazon_transaction_payloads
                       (fingerprint, payload_zlib, uncompressed_bytes, first_run_id, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (row["fingerprint"], row["raw_payload_zlib"], row["raw_payload_bytes"], run_id, now),
                )
                current = connection.execute(
                    """SELECT * FROM amazon_transaction_state
                       WHERE source_id=? AND marketplace_id=? AND transaction_id=?""",
                    (run["source_id"], run["marketplace_id"], row["transaction_id"]),
                ).fetchone()
                if current is not None and current["fingerprint"] == row["fingerprint"]:
                    continue
                normalized = json.loads(row["normalized_json"])
                observed_at = str(normalized.get("postedDate") or "") or None
                decision = transition_decision(
                    current, row["status"], observed_at, now, row["fingerprint"]
                )
                cursor = connection.execute(
                    """INSERT OR IGNORE INTO amazon_transaction_observations
                       (source_id, marketplace_id, transaction_id, retrieved_at, status, currency,
                        payload_json, fingerprint, conflict, run_id, observation_at, previous_status,
                        conflict_category, included_in_totals, resolution_reason)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (run["source_id"], run["marketplace_id"], row["transaction_id"], now,
                     row["status"], row["currency"], row["normalized_json"], row["fingerprint"],
                     int(decision["conflict"]), run_id, observed_at,
                     current["status"] if current else None, decision["category"],
                     int(decision["included"]), decision["reason"]),
                )
                inserted += cursor.rowcount
                if decision["conflict"]:
                    conflicts += 1
                    connection.execute(
                        """UPDATE amazon_transaction_state SET conflict=1,
                           conflict_category=?, included_in_totals=0, resolution_reason=?
                           WHERE source_id=? AND marketplace_id=? AND transaction_id=?""",
                        (decision["category"], decision["reason"], run["source_id"],
                         run["marketplace_id"], row["transaction_id"]),
                    )
                if decision["promote"]:
                    connection.execute(
                        """INSERT INTO amazon_transaction_state
                           (source_id, marketplace_id, transaction_id, marketplace_name, currency,
                            status, retrieved_at, payload_json, fingerprint, conflict, active_run_id,
                            observation_at, conflict_category, included_in_totals, resolution_reason)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(source_id, marketplace_id, transaction_id) DO UPDATE SET
                             marketplace_name=excluded.marketplace_name, currency=excluded.currency,
                             status=excluded.status, retrieved_at=excluded.retrieved_at,
                             payload_json=excluded.payload_json, fingerprint=excluded.fingerprint,
                             conflict=excluded.conflict, active_run_id=excluded.active_run_id,
                             observation_at=excluded.observation_at,
                             conflict_category=excluded.conflict_category,
                             included_in_totals=excluded.included_in_totals,
                             resolution_reason=excluded.resolution_reason""",
                        (run["source_id"], run["marketplace_id"], row["transaction_id"],
                         run["marketplace_name"], row["currency"], row["status"], now,
                         row["normalized_json"], row["fingerprint"], int(decision["conflict"]),
                         run_id, observed_at, decision["category"],
                         int(decision["included"]), decision["reason"]),
                    )
                    updated += 1
            if run["kind"] == "incremental":
                connection.execute(
                    """INSERT INTO amazon_transaction_checkpoints
                       (source_id, marketplace_id, marketplace_name, currency, status, coverage_start,
                        coverage_end, retrieved_at, pagination_complete, last_error, successful_run_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, NULL, ?)
                       ON CONFLICT(source_id, marketplace_id, status) DO UPDATE SET
                         marketplace_name=excluded.marketplace_name, currency=excluded.currency,
                         coverage_start=excluded.coverage_start, coverage_end=excluded.coverage_end,
                         retrieved_at=excluded.retrieved_at, pagination_complete=1, last_error=NULL,
                         successful_run_id=excluded.successful_run_id""",
                    (run["source_id"], run["marketplace_id"], run["marketplace_name"], run["currency"],
                     run["transaction_status"], run["coverage_start"], run["coverage_end"], now, run_id),
                )
            connection.execute(
                """UPDATE amazon_transaction_runs SET status='completed', pagination_complete=1,
                   observations_inserted=?, current_state_updated=?, status_conflicts=?,
                   completed_at=?, updated_at=? WHERE run_id=?""",
                (inserted, updated, conflicts, now, now, run_id),
            )
            connection.execute("DELETE FROM amazon_transaction_staging WHERE run_id=?", (run_id,))
        return {"observations_inserted": inserted, "current_state_updated": updated, "status_conflicts": conflicts}

    def create_transaction_backfill(
        self, *, source_id: str, marketplace_id: str, marketplace_name: str,
        currency: str, transaction_status: str, overall_start: datetime,
        overall_end: datetime, slice_hours: int = 24,
        job_type: str = "HISTORICAL_BACKFILL",
    ) -> dict[str, Any]:
        self._source_row(source_id)
        start, end = self._utc(overall_start), self._utc(overall_end)
        if start >= end or end - start > timedelta(days=180):
            raise ValueError("Backfill range must be positive and no longer than 180 days.")
        if transaction_status not in TRANSACTION_STATUSES:
            raise ValueError(f"Unsupported Amazon transaction status: {transaction_status}.")
        if slice_hours < 1 or slice_hours > 24 * 30:
            raise ValueError("Backfill slice must be between 1 hour and 30 days.")
        if job_type not in {"CANARY", "HISTORICAL_BACKFILL"}:
            raise ValueError("Transaction jobs must be CANARY or HISTORICAL_BACKFILL.")
        slice_end = min(start + timedelta(hours=slice_hours), end)
        job_id = uuid4().hex
        now = _utc_now()
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO amazon_transaction_backfill_jobs
                   (job_id, job_type, source_id, marketplace_id, marketplace_name, currency, transaction_status,
                    overall_start, overall_end, current_slice_start, current_slice_end, slice_hours,
                    status, starting_database_bytes, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?)""",
                (job_id, job_type, source_id, marketplace_id, marketplace_name, currency.upper(), transaction_status,
                 start.isoformat(), end.isoformat(), start.isoformat(), slice_end.isoformat(),
                 slice_hours, self.database_path.stat().st_size, now, now),
            )
        return self.get_transaction_backfill(job_id)

    def get_transaction_backfill(self, job_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM amazon_transaction_backfill_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
        if row is None:
            raise ValueError("Transaction backfill job not found.")
        return self._public_backfill_row(row)

    def list_transaction_backfills(self, source_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM amazon_transaction_backfill_jobs WHERE source_id=? ORDER BY created_at DESC",
                (source_id,),
            ).fetchall()
        return [self._public_backfill_row(row) for row in rows]

    def pause_transaction_backfill(self, job_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute(
                """UPDATE amazon_transaction_backfill_jobs
                   SET status='paused', pause_reason='MANUAL', updated_at=?
                   WHERE job_id=? AND status IN ('running','failed')""",
                (_utc_now(), job_id),
            )
        return self.get_transaction_backfill(job_id)

    def resume_transaction_backfill(self, job_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute(
                """UPDATE amazon_transaction_backfill_jobs SET status='running', last_error=NULL,
                   pause_reason=NULL, consecutive_throttles=0, updated_at=?
                   WHERE job_id=? AND status IN ('paused','failed')""",
                (_utc_now(), job_id),
            )
            connection.execute(
                "UPDATE amazon_transaction_runs SET status='running', last_error=NULL, updated_at=? WHERE run_id=(SELECT active_run_id FROM amazon_transaction_backfill_jobs WHERE job_id=?) AND status='failed'",
                (_utc_now(), job_id),
            )
        return self.get_transaction_backfill(job_id)

    def _pause_backfill(self, job_id: str, reason: str) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute(
                """UPDATE amazon_transaction_backfill_jobs
                   SET status='paused', pause_reason=?, updated_at=? WHERE job_id=?""",
                (reason, _utc_now(), job_id),
            )
        return self.get_transaction_backfill(job_id)

    def _backfill_pause_reason(self, job: Mapping[str, object]) -> str | None:
        source = self._source_row(str(job["source_id"]))
        if source["last_error"] or source["status"] not in {"Connected", "Current"}:
            return "SOURCE_SYNC_UNHEALTHY"
        latest_sync = transaction_timestamp(source["last_sync_at"])
        maximum_age = max(5, int(os.getenv(
            "AI_CASHFLOW_TRANSACTION_BACKFILL_MAX_SOURCE_SYNC_AGE_MINUTES", "15"
        )))
        if latest_sync and datetime.now(timezone.utc) - latest_sync > timedelta(minutes=maximum_age):
            return "SOURCE_SYNC_UNHEALTHY"
        maximum_growth = max(1, int(os.getenv(
            "AI_CASHFLOW_TRANSACTION_BACKFILL_MAX_DB_GROWTH_BYTES", str(128 * 1024 * 1024)
        )))
        if self.database_path.stat().st_size - int(job["starting_database_bytes"] or 0) > maximum_growth:
            return "DATABASE_GROWTH_LIMIT"
        minimum_free = max(0, int(os.getenv(
            "AI_CASHFLOW_TRANSACTION_BACKFILL_MIN_FREE_DISK_BYTES", str(1024 * 1024 * 1024)
        )))
        if shutil.disk_usage(self.database_path.parent).free < minimum_free:
            return "LOW_DISK_SPACE"
        maximum_throttles = max(1, int(os.getenv(
            "AI_CASHFLOW_TRANSACTION_BACKFILL_MAX_CONSECUTIVE_THROTTLES", "3"
        )))
        if int(job["consecutive_throttles"] or 0) >= maximum_throttles:
            return "PERSISTENT_AMAZON_THROTTLING"
        transactions = int(job["transactions_received"] or 0)
        maximum_conflict_rate = min(1.0, max(0.0, float(os.getenv(
            "AI_CASHFLOW_TRANSACTION_BACKFILL_MAX_CONFLICT_RATE", "0.05"
        ))))
        if transactions and int(job["status_conflicts"] or 0) / transactions > maximum_conflict_rate:
            return "STATUS_CONFLICT_RATE"
        return None

    def _fetch_transaction_page(
        self, client: Any, marketplace_id: str, status: str, start: datetime,
        end: datetime, next_token: str | None,
    ) -> dict[str, Any]:
        paged = getattr(client, "list_transactions_page", None)
        if callable(paged):
            return paged(marketplace_id, status, start, end, next_token)
        legacy = getattr(client, "list_transactions_by_status", None)
        if not callable(legacy) or next_token:
            raise ValueError("Amazon transaction pagination is unavailable.")
        return {"transactions": legacy(marketplace_id, status, start), "next_token": None, "rate_limit": None}

    def run_transaction_backfill(
        self, job_id: str, client: Any, *, max_pages: int = 5, max_seconds: float = 20,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + max(0.01, max_seconds)
        pages = 0
        while pages < max_pages and time.monotonic() < deadline:
            job = self.get_transaction_backfill(job_id)
            if job["status"] != "running":
                return job
            pause_reason = self._backfill_pause_reason(job)
            if pause_reason:
                return self._pause_backfill(job_id, pause_reason)
            run_id = job["active_run_id"]
            if not run_id:
                run_id = self._create_collection_run(
                    kind="backfill", job_type=job["job_type"], parent_job_id=job_id,
                    source_id=job["source_id"], marketplace_id=job["marketplace_id"],
                    marketplace_name=job["marketplace_name"], currency=job["currency"],
                    transaction_status=job["transaction_status"],
                    coverage_start=datetime.fromisoformat(job["current_slice_start"]),
                    coverage_end=datetime.fromisoformat(job["current_slice_end"]),
                )
                with self._connect() as connection:
                    connection.execute(
                        "UPDATE amazon_transaction_backfill_jobs SET active_run_id=?, updated_at=? WHERE job_id=?",
                        (run_id, _utc_now(), job_id),
                    )
                job = self.get_transaction_backfill(job_id)
            with self._connect() as connection:
                run = connection.execute("SELECT * FROM amazon_transaction_runs WHERE run_id=?", (run_id,)).fetchone()
            try:
                page = self._fetch_transaction_page(
                    client, job["marketplace_id"], job["transaction_status"],
                    datetime.fromisoformat(job["current_slice_start"]),
                    datetime.fromisoformat(job["current_slice_end"]), run["next_token"],
                )
                token = page.get("next_token")
                seen = set(json.loads(run["seen_tokens_json"]))
                if token and token in seen:
                    raise ValueError("Amazon repeated a finance transaction page token.")
                if token:
                    seen.add(token)
                self._stage_transaction_page(
                    run_id, list(page.get("transactions") or []), token,
                    str(page.get("rate_limit") or "") or None,
                    int(page.get("throttled_attempts") or 0),
                )
                pages += 1
                with self._connect() as connection:
                    connection.execute(
                        "UPDATE amazon_transaction_runs SET seen_tokens_json=?, updated_at=? WHERE run_id=?",
                        (json.dumps(sorted(seen)), _utc_now(), run_id),
                    )
                    connection.execute(
                        """UPDATE amazon_transaction_backfill_jobs SET next_token=?,
                           pages_completed=pages_completed+1,
                           transactions_received=transactions_received+?,
                           consecutive_throttles=CASE WHEN ? > 0
                             THEN consecutive_throttles + 1 ELSE 0 END,
                           updated_at=? WHERE job_id=?""",
                        (token, len(page.get("transactions") or []),
                         int(page.get("throttled_attempts") or 0), _utc_now(), job_id),
                    )
                job = self.get_transaction_backfill(job_id)
                pause_reason = self._backfill_pause_reason(job)
                if pause_reason:
                    return self._pause_backfill(job_id, pause_reason)
                if token:
                    continue
                metrics = self._promote_collection_run(run_id)
                job = self.get_transaction_backfill(job_id)
                current_end = datetime.fromisoformat(job["current_slice_end"])
                overall_end = datetime.fromisoformat(job["overall_end"])
                done = current_end >= overall_end
                next_end = min(current_end + timedelta(hours=job["slice_hours"]), overall_end)
                now = _utc_now()
                with self._connect() as connection:
                    connection.execute(
                        """UPDATE amazon_transaction_backfill_jobs SET
                           current_slice_start=?, current_slice_end=?, active_run_id=NULL, next_token=NULL,
                           observations_inserted=observations_inserted+?,
                           current_state_updated=current_state_updated+?,
                           status_conflicts=status_conflicts+?, status=?, last_error=NULL,
                           updated_at=?, completed_at=? WHERE job_id=?""",
                        (current_end.isoformat(), next_end.isoformat(), metrics["observations_inserted"],
                         metrics["current_state_updated"], metrics["status_conflicts"],
                         "completed" if done else "running", now,
                         now if done else None, job_id),
                    )
                job = self.get_transaction_backfill(job_id)
                pause_reason = self._backfill_pause_reason(job)
                if pause_reason:
                    return self._pause_backfill(job_id, pause_reason)
                if done:
                    break
            except Exception as exc:
                now = _utc_now()
                with self._connect() as connection:
                    connection.execute(
                        "UPDATE amazon_transaction_runs SET status='failed', last_error=?, updated_at=? WHERE run_id=?",
                        (str(exc)[:300], now, run_id),
                    )
                    connection.execute(
                        "UPDATE amazon_transaction_backfill_jobs SET status='failed', last_error=?, updated_at=? WHERE job_id=?",
                        (str(exc)[:300], now, job_id),
                    )
                break
        return self.get_transaction_backfill(job_id)

    def run_incremental_transaction_collection(
        self, source_id: str, client: Any, *, now: datetime | None = None,
        max_pages: int = 5, max_seconds: float = 20,
    ) -> dict[str, Any]:
        source = self.get_source(source_id)
        current = self._utc(now or datetime.now(timezone.utc))
        safety_minutes = max(2, int(os.getenv("AI_CASHFLOW_TRANSACTION_SAFETY_DELAY_MINUTES", "2")))
        overlap_hours = max(1, int(os.getenv("AI_CASHFLOW_TRANSACTION_OVERLAP_HOURS", "6")))
        lookback_hours = max(overlap_hours, int(os.getenv("AI_CASHFLOW_TRANSACTION_LOOKBACK_HOURS", "24")))
        coverage_end = current - timedelta(minutes=safety_minutes)
        deadline = time.monotonic() + max(0.01, max_seconds)
        remaining_pages = max_pages
        errors = 0
        completed = 0
        running = 0
        marketplaces = [
            row for row in source.get("marketplaces", [])
            if row.get("is_participating") and str(row.get("name", "")).lower().startswith("amazon")
        ]
        for marketplace in marketplaces:
            marketplace_id = str(marketplace.get("id") or "")
            if not marketplace_id:
                continue
            for status in TRANSACTION_STATUSES:
                if remaining_pages <= 0 or time.monotonic() >= deadline:
                    running += 1
                    continue
                with self._connect() as connection:
                    run = connection.execute(
                        """SELECT * FROM amazon_transaction_runs WHERE kind='incremental'
                           AND source_id=? AND marketplace_id=? AND transaction_status=?
                           AND status='running' ORDER BY created_at LIMIT 1""",
                        (source_id, marketplace_id, status),
                    ).fetchone()
                    checkpoint = connection.execute(
                        """SELECT * FROM amazon_transaction_checkpoints
                           WHERE source_id=? AND marketplace_id=? AND status=?""",
                        (source_id, marketplace_id, status),
                    ).fetchone()
                if run is None:
                    start = (
                        datetime.fromisoformat(checkpoint["coverage_end"]) - timedelta(hours=overlap_hours)
                        if checkpoint and checkpoint["pagination_complete"]
                        else coverage_end - timedelta(hours=lookback_hours)
                    )
                    run_id = self._create_collection_run(
                        kind="incremental", job_type="INCREMENTAL_SYNC",
                        source_id=source_id, marketplace_id=marketplace_id,
                        marketplace_name=str(marketplace.get("name") or marketplace_id),
                        currency=str(marketplace.get("currency") or "").upper(),
                        transaction_status=status, coverage_start=start, coverage_end=coverage_end,
                    )
                else:
                    run_id = run["run_id"]
                while remaining_pages > 0 and time.monotonic() < deadline:
                    with self._connect() as connection:
                        run = connection.execute("SELECT * FROM amazon_transaction_runs WHERE run_id=?", (run_id,)).fetchone()
                    try:
                        page = self._fetch_transaction_page(
                            client, marketplace_id, status,
                            datetime.fromisoformat(run["coverage_start"]),
                            datetime.fromisoformat(run["coverage_end"]), run["next_token"],
                        )
                        token = page.get("next_token")
                        seen = set(json.loads(run["seen_tokens_json"]))
                        if token and token in seen:
                            raise ValueError("Amazon repeated a finance transaction page token.")
                        if token:
                            seen.add(token)
                        self._stage_transaction_page(
                            run_id, list(page.get("transactions") or []), token,
                            str(page.get("rate_limit") or "") or None,
                            int(page.get("throttled_attempts") or 0),
                        )
                        remaining_pages -= 1
                        with self._connect() as connection:
                            connection.execute(
                                "UPDATE amazon_transaction_runs SET seen_tokens_json=?, updated_at=? WHERE run_id=?",
                                (json.dumps(sorted(seen)), _utc_now(), run_id),
                            )
                        if token:
                            continue
                        self._promote_collection_run(run_id)
                        completed += 1
                        break
                    except Exception as exc:
                        errors += 1
                        now_text = _utc_now()
                        with self._connect() as connection:
                            connection.execute(
                                "UPDATE amazon_transaction_runs SET status='failed', last_error=?, updated_at=? WHERE run_id=?",
                                (str(exc)[:300], now_text, run_id),
                            )
                            connection.execute("DELETE FROM amazon_transaction_staging WHERE run_id=?", (run_id,))
                        break
                else:
                    running += 1
        status = "partial" if errors else ("running" if running else "completed")
        return {"status": status, "completed_units": completed, "running_units": running, "errors": errors}

    def transaction_collection_diagnostics(self, source_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            checkpoints = connection.execute(
                "SELECT * FROM amazon_transaction_checkpoints WHERE source_id=? ORDER BY marketplace_id, status",
                (source_id,),
            ).fetchall()
            runs = connection.execute(
                "SELECT * FROM amazon_transaction_runs WHERE source_id=? ORDER BY created_at DESC LIMIT 100",
                (source_id,),
            ).fetchall()
            typed_runs = connection.execute(
                "SELECT * FROM amazon_transaction_runs WHERE source_id=? AND kind='backfill'",
                (source_id,),
            ).fetchall()
            jobs = connection.execute(
                "SELECT * FROM amazon_transaction_backfill_jobs WHERE source_id=? ORDER BY created_at DESC",
                (source_id,),
            ).fetchall()
            counts = connection.execute(
                """SELECT
                   (SELECT COUNT(*) FROM amazon_transaction_observations WHERE source_id=?),
                   (SELECT COUNT(*) FROM amazon_transaction_state WHERE source_id=?),
                   (SELECT COUNT(*) FROM amazon_transaction_state WHERE source_id=? AND conflict=1)""",
                (source_id, source_id, source_id),
            ).fetchone()
            status_events = connection.execute(
                """SELECT transaction_id, previous_status, status, observation_at, retrieved_at,
                          conflict_category, included_in_totals, resolution_reason
                   FROM amazon_transaction_observations
                   WHERE source_id=? AND previous_status IS NOT NULL AND previous_status<>status
                   ORDER BY retrieved_at DESC LIMIT 100""",
                (source_id,),
            ).fetchall()
        def job_progress(row: sqlite3.Row) -> dict[str, Any]:
            result = self._public_backfill_row(row)
            scoped_runs = [
                run for run in typed_runs
                if run["job_type"] == row["job_type"]
                and run["parent_job_id"] == row["job_id"]
                and run["marketplace_id"] == row["marketplace_id"]
                and run["currency"] == row["currency"]
                and run["transaction_status"] == row["transaction_status"]
                and run["coverage_start"] >= row["overall_start"]
                and run["coverage_end"] <= row["overall_end"]
            ]
            expected = max(1, math.ceil((
                datetime.fromisoformat(row["overall_end"])
                - datetime.fromisoformat(row["overall_start"])
            ).total_seconds() / (int(row["slice_hours"]) * 3600)))
            completed = [run for run in scoped_runs if run["status"] == "completed"]
            failed = [run for run in scoped_runs if run["status"] == "failed"]
            result.update({
                "completed_start": min((run["coverage_start"] for run in completed), default=None),
                "completed_end": max((run["coverage_end"] for run in completed), default=None),
                "completed_slice_count": len(completed),
                "pending_slice_count": max(0, expected - len(completed) - len(failed)),
                "failed_slice_count": len(failed),
                "estimated_remaining_work": None,
            })
            return result

        incremental_runs = [
            self._public_collection_row(row)
            for row in runs if row["job_type"] == "INCREMENTAL_SYNC"
        ]
        canaries = [job_progress(row) for row in jobs if row["job_type"] == "CANARY"]
        historical = [
            job_progress(row) for row in jobs if row["job_type"] == "HISTORICAL_BACKFILL"
        ]
        legacy = [
            job_progress(row) for row in jobs if row["job_type"] == "LEGACY_UNCLASSIFIED"
        ]

        return {
            "source_id": source_id,
            "checkpoints": [self._public_collection_row(row) for row in checkpoints],
            "runs": [self._public_collection_row(row) for row in runs],
            "incremental_collection": {
                "status": "ACTIVE" if checkpoints else "NOT_STARTED",
                "last_successful_run": max(
                    (row["completed_at"] for row in incremental_runs if row["completed_at"]),
                    default=None,
                ),
                "runs": incremental_runs,
            },
            "validation_canaries": canaries,
            "historical_backfills": historical,
            "legacy_unclassified": legacy,
            "backfills": historical,
            "observation_count": counts[0],
            "current_state_count": counts[1],
            "status_conflict_count": counts[2],
            "status_events": [
                {
                    "transaction_id_masked": f"***{str(row['transaction_id'])[-4:]}",
                    "previous_status": row["previous_status"],
                    "observed_status": row["status"],
                    "observation_timestamp": row["observation_at"],
                    "retrieval_timestamp": row["retrieved_at"],
                    "conflict_category": row["conflict_category"],
                    "included_in_totals": bool(row["included_in_totals"]),
                    "resolution_reason": row["resolution_reason"],
                }
                for row in status_events
            ],
            "database_bytes": self.database_path.stat().st_size,
        }

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
        run_id: str | None = None,
    ) -> None:
        """Atomically append observations and advance deterministic current state."""
        if requested_status not in TRANSACTION_STATUSES:
            raise ValueError(f"Unsupported Amazon transaction status: {requested_status}.")
        retrieved_at = retrieved_at.astimezone(timezone.utc)
        retrieved_text = retrieved_at.isoformat()
        start = coverage_start or retrieved_at - timedelta(days=179)
        end = coverage_end or retrieved_at
        run_id = run_id or uuid4().hex
        observed_ids: set[str] = set()
        with self._connect() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO amazon_transaction_runs
                   (run_id, kind, source_id, marketplace_id, marketplace_name, currency,
                    transaction_status, coverage_start, coverage_end, status,
                    pagination_complete, created_at, updated_at, completed_at)
                   VALUES (?, 'incremental', ?, ?, ?, ?, ?, ?, ?, 'completed', 1, ?, ?, ?)""",
                (run_id, source_id, marketplace_id, marketplace_name, currency, requested_status,
                 start.isoformat(), end.isoformat(), retrieved_text, retrieved_text, retrieved_text),
            )
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
                raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
                connection.execute(
                    """INSERT OR IGNORE INTO amazon_transaction_payloads
                       (fingerprint, payload_zlib, uncompressed_bytes, first_run_id, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (fingerprint, zlib.compress(raw), len(raw), run_id, retrieved_text),
                )
                current = connection.execute(
                    """SELECT * FROM amazon_transaction_state
                       WHERE source_id=? AND marketplace_id=? AND transaction_id=?""",
                    (source_id, marketplace_id, transaction_id),
                ).fetchone()
                if current is not None and current["fingerprint"] == fingerprint:
                    continue
                observed_at = str(payload.get("postedDate") or "") or None
                decision = transition_decision(
                    current, status, observed_at, retrieved_text, fingerprint
                )
                connection.execute(
                    """INSERT OR IGNORE INTO amazon_transaction_observations
                       (source_id, marketplace_id, transaction_id, retrieved_at, status,
                        currency, payload_json, fingerprint, conflict, run_id, observation_at,
                        previous_status, conflict_category, included_in_totals, resolution_reason)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (source_id, marketplace_id, transaction_id, retrieved_text, status,
                     row_currency, serialized, fingerprint, int(decision["conflict"]), run_id,
                     observed_at, current["status"] if current else None,
                     decision["category"], int(decision["included"]), decision["reason"]),
                )
                if decision["conflict"]:
                    connection.execute(
                        """UPDATE amazon_transaction_state SET conflict=1,
                           conflict_category=?, included_in_totals=0, resolution_reason=?
                           WHERE source_id=? AND marketplace_id=? AND transaction_id=?""",
                        (decision["category"], decision["reason"],
                         source_id, marketplace_id, transaction_id),
                    )
                    LOGGER.warning(
                        "amazon_transaction_state_conflict source=%s marketplace=%s transaction=%s current=%s observed=%s",
                        source_id, marketplace_id, transaction_id[-8:], current["status"], status,
                    )
                if decision["promote"]:
                    connection.execute(
                        """INSERT INTO amazon_transaction_state
                           (source_id, marketplace_id, transaction_id, marketplace_name, currency,
                            status, retrieved_at, payload_json, fingerprint, conflict, active_run_id,
                            observation_at, conflict_category, included_in_totals, resolution_reason)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(source_id, marketplace_id, transaction_id) DO UPDATE SET
                             marketplace_name=excluded.marketplace_name, currency=excluded.currency,
                             status=excluded.status, retrieved_at=excluded.retrieved_at,
                             payload_json=excluded.payload_json, fingerprint=excluded.fingerprint,
                             conflict=excluded.conflict, active_run_id=excluded.active_run_id,
                             observation_at=excluded.observation_at,
                             conflict_category=excluded.conflict_category,
                             included_in_totals=excluded.included_in_totals,
                             resolution_reason=excluded.resolution_reason""",
                        (source_id, marketplace_id, transaction_id, marketplace_name, row_currency,
                         status, retrieved_text, serialized, fingerprint, int(decision["conflict"]),
                         run_id, observed_at, decision["category"],
                         int(decision["included"]), decision["reason"]),
                    )
            if observed_ids:
                connection.execute(
                    "CREATE TEMP TABLE IF NOT EXISTS observed_transaction_ids (id TEXT PRIMARY KEY)"
                )
                connection.execute("DELETE FROM observed_transaction_ids")
                connection.executemany(
                    "INSERT INTO observed_transaction_ids (id) VALUES (?)",
                    ((transaction_id,) for transaction_id in observed_ids),
                )
                connection.execute(
                    """DELETE FROM amazon_transaction_state
                       WHERE source_id=? AND marketplace_id=? AND status=? AND retrieved_at<?
                       AND NOT EXISTS (
                         SELECT 1 FROM observed_transaction_ids observed
                         WHERE observed.id=amazon_transaction_state.transaction_id
                       )""",
                    (source_id, marketplace_id, requested_status, retrieved_text),
                )
                connection.execute("DROP TABLE observed_transaction_ids")
            else:
                connection.execute(
                    """DELETE FROM amazon_transaction_state
                       WHERE source_id=? AND marketplace_id=? AND status=? AND retrieved_at<?""",
                    (source_id, marketplace_id, requested_status, retrieved_text),
                )
            connection.execute(
                """INSERT INTO amazon_transaction_checkpoints
                   (source_id, marketplace_id, marketplace_name, currency, status, coverage_start, coverage_end,
                    retrieved_at, pagination_complete, last_error, successful_run_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                   ON CONFLICT(source_id, marketplace_id, status) DO UPDATE SET
                     marketplace_name=excluded.marketplace_name, currency=excluded.currency,
                     coverage_start=excluded.coverage_start, coverage_end=excluded.coverage_end,
                     retrieved_at=excluded.retrieved_at, successful_run_id=excluded.successful_run_id,
                     pagination_complete=excluded.pagination_complete, last_error=NULL""",
                (source_id, marketplace_id, marketplace_name, currency, requested_status, start.isoformat(), end.isoformat(),
                 retrieved_text, int(pagination_complete), run_id),
            )
            connection.execute(
                """UPDATE amazon_transaction_runs SET transactions_received=?,
                   observations_inserted=(SELECT COUNT(*) FROM amazon_transaction_observations WHERE run_id=?),
                   current_state_updated=(SELECT COUNT(*) FROM amazon_transaction_state WHERE active_run_id=?),
                   updated_at=?, completed_at=?, pagination_complete=? WHERE run_id=?""",
                (len(transactions), run_id, run_id, retrieved_text, retrieved_text,
                 int(pagination_complete), run_id),
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
        where = (
            "source_id=? AND active_run_id IN "
            "(SELECT run_id FROM amazon_transaction_runs WHERE status='completed')"
            + (" AND currency=?" if requested else "")
        )
        params: tuple[object, ...] = (source_id, requested) if requested else (source_id,)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM amazon_transaction_state WHERE {where}", params
            ).fetchall()
            observation_where = (
                "source_id=? AND run_id IN "
                "(SELECT run_id FROM amazon_transaction_runs WHERE status='completed')"
                + (" AND currency=?" if requested else "")
            )
            observation_count = connection.execute(
                f"SELECT COUNT(*) FROM amazon_transaction_observations WHERE {observation_where}", params
            ).fetchone()[0]
            checkpoints = connection.execute(
                "SELECT * FROM amazon_transaction_checkpoints WHERE source_id=?",
                (source_id,),
            ).fetchall()
            failed_runs = connection.execute(
                """SELECT marketplace_id, currency, transaction_status, last_error
                   FROM amazon_transaction_runs WHERE source_id=? AND status='failed'""",
                (source_id,),
            ).fetchall()
            collection_runs = connection.execute(
                "SELECT * FROM amazon_transaction_runs WHERE source_id=? AND kind='backfill'",
                (source_id,),
            ).fetchall()
            backfill_jobs = connection.execute(
                "SELECT * FROM amazon_transaction_backfill_jobs WHERE source_id=?",
                (source_id,),
            ).fetchall()

        def summarize(selected: list[sqlite3.Row], selected_currency: str) -> dict[str, Any]:
            amounts = {status: Decimal("0") for status in TRANSACTION_STATUSES}
            counts = {status: 0 for status in TRANSACTION_STATUSES}
            composition: dict[str, Decimal] = {}
            visible = [
                row for row in selected
                if not row["conflict"] and row["included_in_totals"]
            ]
            for row in visible:
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
            checkpoint_start = min(
                (row["coverage_start"] for row in relevant_checkpoints), default=None
            )
            checkpoint_end = max(
                (row["coverage_end"] for row in relevant_checkpoints), default=None
            )
            historical_jobs = [
                row for row in backfill_jobs
                if row["currency"] == selected_currency
                and row["job_type"] == "HISTORICAL_BACKFILL"
            ]
            requested_start = min(
                (row["overall_start"] for row in historical_jobs), default=None
            )
            requested_end = max(
                (row["overall_end"] for row in historical_jobs), default=None
            )
            historical_runs = [
                row for row in collection_runs
                if row["currency"] == selected_currency
                and row["job_type"] == "HISTORICAL_BACKFILL"
                and requested_start
                and row["coverage_start"] >= requested_start
                and row["coverage_end"] <= requested_end
            ]
            completed_runs = [row for row in historical_runs if row["status"] == "completed"]
            failed_slice_count = sum(row["status"] == "failed" for row in historical_runs)
            expected_slice_count = sum(
                max(1, math.ceil((
                    datetime.fromisoformat(row["overall_end"])
                    - datetime.fromisoformat(row["overall_start"])
                ).total_seconds() / (int(row["slice_hours"]) * 3600)))
                for row in historical_jobs
            )
            completed_slice_count = len({
                (row["marketplace_id"], row["transaction_status"], row["coverage_start"], row["coverage_end"])
                for row in completed_runs
            })
            pending_slice_count = max(
                0, expected_slice_count - completed_slice_count - failed_slice_count
            )
            required_scopes = {
                (row["marketplace_id"], row["status"]) for row in relevant_checkpoints
            }
            completed_job_scopes = {
                (row["marketplace_id"], row["transaction_status"])
                for row in historical_jobs if row["status"] == "completed"
            }
            coverage_complete = (
                bool(required_scopes)
                and required_scopes <= completed_job_scopes
                and all(row["status"] == "completed" for row in historical_jobs)
            )
            coverage_complete = coverage_complete and not pending_slice_count and not failed_slice_count
            completed_historical_start = min(
                (row["coverage_start"] for row in completed_runs), default=None
            )
            completed_historical_end = max(
                (row["coverage_end"] for row in completed_runs), default=None
            )
            if not historical_jobs:
                historical_status = "NOT_STARTED"
            elif any(row["status"] == "failed" for row in historical_jobs) or failed_slice_count:
                historical_status = "FAILED"
            elif any(row["status"] == "paused" for row in historical_jobs):
                historical_status = "PAUSED"
            elif coverage_complete:
                historical_status = "COMPLETE"
            elif completed_slice_count:
                historical_status = "PARTIALLY_COMPLETE"
            elif any(row["status"] == "running" for row in historical_jobs):
                historical_status = "RUNNING"
            else:
                historical_status = "PARTIALLY_COMPLETE"
            coverage = {
                "observed_start": checkpoint_start,
                "observed_end": checkpoint_end,
                "transaction_count": len(visible),
                "has_observed_data": bool(relevant_checkpoints),
                "historical_backfill_status": historical_status,
                "requested_historical_start": requested_start,
                "requested_historical_end": requested_end,
                "completed_historical_start": completed_historical_start,
                "completed_historical_end": completed_historical_end,
                "is_historically_complete": coverage_complete,
                "requested_start": requested_start,
                "requested_end": requested_end,
                "completed_start": min(
                    (value for value in (checkpoint_start, completed_historical_start) if value),
                    default=None,
                ),
                "completed_end": max(
                    (value for value in (checkpoint_end, completed_historical_end) if value),
                    default=None,
                ),
                "is_complete": coverage_complete,
                "has_gaps": not coverage_complete,
                "completed_slice_count": completed_slice_count,
                "pending_slice_count": pending_slice_count,
                "failed_slice_count": failed_slice_count,
                "coverage_percentage": 100 if coverage_complete else None,
                "last_successful_collection_at": max(
                    (row["retrieved_at"] for row in relevant_checkpoints), default=None
                ),
                "classification": "COMPLETE_COVERAGE" if coverage_complete else "PARTIAL_COVERAGE",
                "backfill_status": historical_status.lower(),
            }
            partial_failure = (
                any(bool(row["last_error"]) for row in relevant_checkpoints)
                or any(row["currency"] == selected_currency for row in failed_runs)
            )
            released_flow = amounts["RELEASED"] + amounts["DEFERRED_RELEASED"]
            return {
                "classification": "AMAZON_TRANSACTION_DERIVED",
                "currency": selected_currency,
                "marketplaces": sorted({
                    row["marketplace_name"] for row in relevant_checkpoints if row["marketplace_name"]
                }),
                "count": len(visible),
                "deferred_count": counts["DEFERRED"],
                "deferred_amount": str(amounts["DEFERRED"].quantize(Decimal("0.01"))),
                "released_count": counts["RELEASED"],
                "released_amount": str(amounts["RELEASED"].quantize(Decimal("0.01"))),
                "deferred_released_count": counts["DEFERRED_RELEASED"],
                "deferred_released_amount": str(amounts["DEFERRED_RELEASED"].quantize(Decimal("0.01"))),
                "released_flow_count": counts["RELEASED"] + counts["DEFERRED_RELEASED"],
                "released_flow_amount": str(released_flow.quantize(Decimal("0.01"))),
                "conflict_count": sum(int(row["conflict"]) for row in selected),
                "excluded_count": len(selected) - len(visible),
                "totals_partial": not coverage_complete or len(selected) != len(visible) or partial_failure,
                "coverage": coverage,
                "reconciliation_state": coverage["classification"],
                "composition": {
                    key: str(value.quantize(Decimal("0.01")))
                    for key, value in composition.items() if value
                },
                "coverage_start": checkpoint_start,
                "coverage_end": checkpoint_end,
                "retrieved_at": max((row["retrieved_at"] for row in relevant_checkpoints), default=None),
                "pagination_complete": bool(relevant_checkpoints) and all(row["pagination_complete"] for row in relevant_checkpoints),
                "partial_failure": partial_failure,
            }

        if requested:
            result = summarize(rows, requested)
            result.update({"currency": requested, "observation_count": observation_count})
            return result
        currencies = sorted(
            {row["currency"] for row in rows} |
            {row["currency"] for row in checkpoints if row["currency"]}
        )
        by_currency = {
            code: summarize([row for row in rows if row["currency"] == code], code)
            for code in currencies
        }
        coverages = [row["coverage"] for row in by_currency.values()]
        complete = bool(coverages) and all(row["is_complete"] for row in coverages)
        coverage = {
            "requested_start": min((row["requested_start"] for row in coverages if row["requested_start"]), default=None),
            "requested_end": max((row["requested_end"] for row in coverages if row["requested_end"]), default=None),
            "completed_start": min((row["completed_start"] for row in coverages if row["completed_start"]), default=None),
            "completed_end": max((row["completed_end"] for row in coverages if row["completed_end"]), default=None),
            "is_complete": complete,
            "has_gaps": not complete,
            "completed_slice_count": sum(row["completed_slice_count"] for row in coverages),
            "pending_slice_count": sum(row["pending_slice_count"] for row in coverages),
            "failed_slice_count": sum(row["failed_slice_count"] for row in coverages),
            "coverage_percentage": 100 if complete else None,
            "last_successful_collection_at": max((row["last_successful_collection_at"] for row in coverages if row["last_successful_collection_at"]), default=None),
            "classification": "COMPLETE_COVERAGE" if complete else "PARTIAL_COVERAGE",
        }
        return {
            "classification": "AMAZON_TRANSACTION_DERIVED",
            "observation_count": observation_count,
            "coverage": coverage,
            "reconciliation_state": coverage["classification"],
            "by_currency": by_currency,
        }

    def transaction_reconciliation_report(self, source_id: str) -> dict[str, Any]:
        """Internal-only trend evidence; three retrieval snapshots are required."""
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT retrieved_at, currency, status, payload_json
                   FROM amazon_transaction_observations
                   WHERE source_id=? AND run_id IN
                      (SELECT run_id FROM amazon_transaction_runs WHERE status='completed')
                     AND included_in_totals=1 AND conflict=0
                   ORDER BY retrieved_at""",
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
        collection_enabled = os.getenv(
            "AI_CASHFLOW_TRANSACTION_COLLECTION_ENABLED", "false"
        ).strip().lower() in {"1", "true", "yes", "on"}
        transaction_collection_status = "disabled"
        transaction_rows = 0
        transaction_errors = 0
        if collection_enabled:
            collection = self.run_incremental_transaction_collection(
                source_id,
                client,
                max_pages=max(1, int(os.getenv("AI_CASHFLOW_TRANSACTION_MAX_PAGES_PER_SYNC", "5"))),
                max_seconds=max(1, int(os.getenv("AI_CASHFLOW_TRANSACTION_MAX_SECONDS_PER_SYNC", "20"))),
            )
            transaction_collection_status = collection["status"]
            transaction_errors = collection["errors"]
            with self._connect() as connection:
                transaction_rows = connection.execute(
                    """SELECT COALESCE(SUM(observations_inserted), 0)
                       FROM amazon_transaction_runs WHERE source_id=? AND updated_at>=?""",
                    (source_id, started_at),
                ).fetchone()[0]
        completed_at = _utc_now()
        sync_status = "partial" if transaction_errors else "completed"
        message = (
            f"Ingested {ingested} new settlement report(s); stored {transaction_rows} "
            f"transaction observation(s)."
        )
        if not collection_enabled:
            message += " Transaction collection is disabled."
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
            "open_balance_sync_status": "completed",
            "transaction_collection_enabled": collection_enabled,
            "transaction_collection_status": transaction_collection_status,
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
