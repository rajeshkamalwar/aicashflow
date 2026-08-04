"""Durable operational storage for uploads, runs, and audit events."""

from pathlib import Path
from contextlib import contextmanager
from decimal import Decimal
import sqlite3
from typing import Iterator

from ai_cashflow.models import BankReceipt


class OperationalStore:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def add_upload(self, record: dict[str, str]) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                insert into uploads (file_name, category, path, uploaded_at)
                values (?, ?, ?, ?)
                """,
                (
                    record["file_name"],
                    record["category"],
                    record["path"],
                    record["uploaded_at"],
                ),
            )

    def list_uploads(self) -> list[dict[str, str]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                select file_name, category, path, uploaded_at
                from uploads
                order by uploaded_at, id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_upload(self, file_name: str) -> str | None:
        """Remove the upload record and return the stored file path, or None if not found."""
        with self._connect() as connection:
            row = connection.execute(
                "select path from uploads where file_name = ?", (file_name,)
            ).fetchone()
            if row is None:
                return None
            connection.execute("delete from uploads where file_name = ?", (file_name,))
        return row["path"]

    def add_run(self, record: dict[str, object]) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                insert into report_runs (
                    run_id, status, created_at, matched_payouts,
                    unmatched_expected_payouts, unmatched_bank_receipts,
                    data_quality_issues
                )
                values (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["run_id"],
                    record["status"],
                    record["created_at"],
                    record["matched_payouts"],
                    record["unmatched_expected_payouts"],
                    record["unmatched_bank_receipts"],
                    record["data_quality_issues"],
                ),
            )

    def clear_runs(self) -> int:
        """Delete all run history rows. Returns number of rows deleted."""
        with self._connect() as connection:
            cursor = connection.execute("delete from report_runs")
            return cursor.rowcount

    def list_runs(self) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                select run_id, status, created_at, matched_payouts,
                       unmatched_expected_payouts, unmatched_bank_receipts,
                       data_quality_issues
                from report_runs
                order by created_at, id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def add_audit_event(self, event_type: str, details: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                insert into audit_events (event_type, details, created_at)
                values (?, ?, datetime('now'))
                """,
                (event_type, details),
            )

    def list_audit_events(self) -> list[dict[str, str]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                select event_type, details, created_at
                from audit_events
                order by created_at, id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def import_bank_statement(
        self,
        *,
        file_name: str,
        file_sha256: str,
        bank_source: str,
        receipts: list[BankReceipt],
        imported_at: str,
        column_mapping_json: str | None = None,
    ) -> dict[str, object]:
        receipt_ids = [receipt.receipt_id for receipt in receipts]
        if len(receipt_ids) != len(set(receipt_ids)):
            raise ValueError("The statement contains a duplicate receipt ID.")
        with self._connect() as connection:
            if connection.execute(
                "select 1 from bank_statement_imports where file_sha256 = ?",
                (file_sha256,),
            ).fetchone():
                raise ValueError("This bank statement was already imported.")
            for receipt_id in receipt_ids:
                if connection.execute(
                    "select 1 from bank_receipts where receipt_id = ?",
                    (receipt_id,),
                ).fetchone():
                    raise ValueError(f"Bank receipt ID {receipt_id} already exists.")
            cursor = connection.execute(
                """
                insert into bank_statement_imports (
                    file_name, file_sha256, bank_source, row_count, imported_at,
                    column_mapping_json
                ) values (?, ?, ?, ?, ?, ?)
                """,
                (
                    file_name, file_sha256, bank_source, len(receipts),
                    imported_at, column_mapping_json,
                ),
            )
            import_id = cursor.lastrowid
            connection.executemany(
                """
                insert into bank_receipts (
                    receipt_id, import_id, bank_account, currency,
                    receipt_date, amount, reference
                ) values (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        receipt.receipt_id,
                        import_id,
                        receipt.bank_account,
                        receipt.currency,
                        receipt.receipt_date.isoformat(),
                        str(receipt.amount),
                        receipt.reference,
                    )
                    for receipt in receipts
                ],
            )
        return {
            "file_name": file_name,
            "file_sha256": file_sha256,
            "bank_source": bank_source,
            "row_count": len(receipts),
            "imported_at": imported_at,
        }

    def list_bank_receipts(self) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                select receipt_id, bank_account, currency, receipt_date,
                       amount, reference
                from bank_receipts
                order by receipt_date, receipt_id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def start_proof_run(
        self,
        *,
        window_days: int,
        source_count: int,
        started_at: str,
    ) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                insert into cashflow_proof_runs (
                    window_days, source_count, source_success_count,
                    item_count, started_at, status, message
                ) values (?, ?, 0, 0, ?, 'running', '')
                """,
                (window_days, source_count, started_at),
            )
            return int(cursor.lastrowid)

    def latest_completed_proof_run_at(self) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                select completed_at from cashflow_proof_runs
                where completed_at is not null and status in ('completed', 'partial')
                order by id desc limit 1
                """
            ).fetchone()
        return str(row["completed_at"]) if row else None

    def finish_proof_run(
        self,
        run_id: int,
        *,
        status: str,
        item_count: int,
        source_success_count: int,
        completed_at: str,
        message: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                update cashflow_proof_runs
                set completed_at=?, status=?, item_count=?,
                    source_success_count=?, message=?
                where id=?
                """,
                (
                    completed_at,
                    status,
                    item_count,
                    source_success_count,
                    message[:500],
                    run_id,
                ),
            )

    def upsert_cashflow_proof(self, record: dict[str, object]) -> None:
        fields = (
            "source_id", "source_name", "marketplace_id", "payment_group_id",
            "settlement_id", "report_id", "currency", "amazon_payment_amount",
            "finances_total", "settlement_total", "amazon_variance",
            "payment_date", "bank_receipt_id", "bank_received_amount",
            "bank_variance", "status", "evidence_hash", "checked_at", "last_error",
        )
        values = tuple(record.get(field) for field in fields)
        with self._connect() as connection:
            connection.execute(
                f"""
                insert into cashflow_proofs ({", ".join(fields)})
                values ({", ".join("?" for _ in fields)})
                on conflict(source_id, payment_group_id) do update set
                    source_name=excluded.source_name,
                    marketplace_id=excluded.marketplace_id,
                    settlement_id=excluded.settlement_id,
                    report_id=excluded.report_id,
                    currency=excluded.currency,
                    amazon_payment_amount=excluded.amazon_payment_amount,
                    finances_total=excluded.finances_total,
                    settlement_total=excluded.settlement_total,
                    amazon_variance=excluded.amazon_variance,
                    payment_date=excluded.payment_date,
                    bank_receipt_id=excluded.bank_receipt_id,
                    bank_received_amount=excluded.bank_received_amount,
                    bank_variance=excluded.bank_variance,
                    status=excluded.status,
                    evidence_hash=excluded.evidence_hash,
                    checked_at=excluded.checked_at,
                    last_error=excluded.last_error
                """,
                values,
            )

    def list_cashflow_proofs(
        self, *, limit: int = 100, offset: int = 0
    ) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                select *
                from cashflow_proofs
                order by payment_date desc, source_name, payment_group_id
                limit ? offset ?
                """,
                (max(1, min(int(limit), 200)), max(0, int(offset))),
            ).fetchall()
        return [dict(row) for row in rows]

    def count_cashflow_proofs(self) -> int:
        with self._connect() as connection:
            return int(
                connection.execute(
                    "select count(*) from cashflow_proofs"
                ).fetchone()[0]
            )

    def get_cashflow_proof_summary(
        self, *, enabled_source_count: int
    ) -> dict[str, object]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                select status, currency, amazon_payment_amount,
                       amazon_variance, bank_variance, checked_at
                from cashflow_proofs
                """
            ).fetchall()
            latest_run = connection.execute(
                """
                select * from cashflow_proof_runs
                order by id desc limit 1
                """
            ).fetchone()
        counts = {
            status: sum(1 for row in rows if row["status"] == status)
            for status in ("Pending", "Amazon verified", "Verified", "Variance", "Failed")
        }
        verified_amounts: dict[str, Decimal] = {}
        pending_amounts: dict[str, Decimal] = {}
        unexplained: dict[str, Decimal] = {}
        for row in rows:
            currency = str(row["currency"])
            amount = Decimal(str(row["amazon_payment_amount"] or "0"))
            if row["status"] == "Verified":
                verified_amounts[currency] = verified_amounts.get(currency, Decimal("0")) + amount
            if row["status"] in {"Pending", "Amazon verified"}:
                pending_amounts[currency] = pending_amounts.get(currency, Decimal("0")) + amount
            variance = Decimal(str(row["amazon_variance"] or "0")) + Decimal(
                str(row["bank_variance"] or "0")
            )
            unexplained[currency] = unexplained.get(currency, Decimal("0")) + variance
        successful_sources = (
            int(latest_run["source_success_count"]) if latest_run else 0
        )
        return {
            "status_counts": counts,
            "verified_count": counts["Verified"],
            "source_coverage": f"{successful_sources}/{max(0, enabled_source_count)}",
            "verified_amounts": _money_map(verified_amounts),
            "pending_amounts": _money_map(pending_amounts),
            "unexplained_variances": _money_map(unexplained),
            "last_checked_at": max(
                (str(row["checked_at"]) for row in rows if row["checked_at"]),
                default=None,
            ),
            "latest_run": dict(latest_run) if latest_run else None,
        }

    def has_due_pending_proofs(self, source_id: str, checked_before: str) -> bool:
        with self._connect() as connection:
            return connection.execute(
                """
                select 1 from cashflow_proofs
                where source_id=? and status='Pending' and checked_at < ?
                limit 1
                """,
                (source_id, checked_before),
            ).fetchone() is not None

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                create table if not exists uploads (
                    id integer primary key autoincrement,
                    file_name text not null,
                    category text not null,
                    path text not null,
                    uploaded_at text not null
                );

                create table if not exists report_runs (
                    id integer primary key autoincrement,
                    run_id text not null unique,
                    status text not null,
                    created_at text not null,
                    matched_payouts integer not null,
                    unmatched_expected_payouts integer not null,
                    unmatched_bank_receipts integer not null,
                    data_quality_issues integer not null
                );

                create table if not exists audit_events (
                    id integer primary key autoincrement,
                    event_type text not null,
                    details text not null,
                    created_at text not null
                );

                create table if not exists bank_statement_imports (
                    id integer primary key autoincrement,
                    file_name text not null,
                    file_sha256 text not null unique,
                    bank_source text not null,
                    row_count integer not null,
                    imported_at text not null,
                    column_mapping_json text
                );

                create table if not exists bank_receipts (
                    receipt_id text primary key,
                    import_id integer not null,
                    bank_account text not null,
                    currency text not null,
                    receipt_date text not null,
                    amount text not null,
                    reference text,
                    foreign key (import_id) references bank_statement_imports(id)
                );

                create table if not exists cashflow_proof_runs (
                    id integer primary key autoincrement,
                    window_days integer not null,
                    source_count integer not null,
                    source_success_count integer not null default 0,
                    item_count integer not null default 0,
                    started_at text not null,
                    completed_at text,
                    status text not null,
                    message text not null
                );

                create table if not exists cashflow_proofs (
                    id integer primary key autoincrement,
                    source_id text not null,
                    source_name text not null,
                    marketplace_id text not null,
                    payment_group_id text not null,
                    settlement_id text,
                    report_id text,
                    currency text not null,
                    amazon_payment_amount text not null,
                    finances_total text,
                    settlement_total text,
                    amazon_variance text,
                    payment_date text not null,
                    bank_receipt_id text,
                    bank_received_amount text,
                    bank_variance text,
                    status text not null,
                    evidence_hash text not null,
                    checked_at text not null,
                    last_error text,
                    unique (source_id, payment_group_id)
                );
                """
            )
            columns = {
                row["name"]
                for row in connection.execute(
                    "pragma table_info(bank_statement_imports)"
                ).fetchall()
            }
            if "column_mapping_json" not in columns:
                connection.execute(
                    "alter table bank_statement_imports "
                    "add column column_mapping_json text"
                )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()


def _money_map(values: dict[str, Decimal]) -> dict[str, str]:
    return {
        currency: str(amount.quantize(Decimal("0.01")))
        for currency, amount in sorted(values.items())
    }
