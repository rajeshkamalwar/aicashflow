"""Encrypted, backend-managed external integration settings."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
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

    def sync_source(
        self, source_id: str, client: Any, samples_dir: str | Path
    ) -> dict[str, Any]:
        self._source_row(source_id)
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
        completed_at = _utc_now()
        message = f"Ingested {ingested} new settlement report(s)."
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO amazon_source_sync_runs
                    (source_id, started_at, completed_at, status, reports_found, reports_ingested, message)
                VALUES (?, ?, ?, 'completed', ?, ?, ?)
                """,
                (source_id, started_at, completed_at, len(reports), ingested, message),
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
            "status": "completed",
            "reports_found": len(reports),
            "reports_ingested": ingested,
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
