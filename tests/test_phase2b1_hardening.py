import asyncio
import sqlite3
import tempfile
import unittest
import zlib
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

from ai_cashflow.api.services import Phase0Service
from ai_cashflow.config import AppConfig
from ai_cashflow.integrations import AmazonSourceRegistry


UTC = timezone.utc


def transaction(transaction_id: str, amount: str = "1.00") -> dict[str, object]:
    return {
        "transactionId": transaction_id,
        "transactionStatus": "DEFERRED_RELEASED",
        "transactionType": "Shipment",
        "postedDate": "2026-04-30T12:00:00Z",
        "totalAmount": {"currencyCode": "CAD", "currencyAmount": amount},
        "breakdowns": [{
            "breakdownType": "Principal",
            "breakdownAmount": {"currencyCode": "CAD", "currencyAmount": amount},
        }],
    }


class TwoPageClient:
    def __init__(self) -> None:
        self.calls: list[str | None] = []

    def list_transactions_page(self, *_args, next_token=None):
        self.calls.append(next_token)
        if next_token is None:
            return {"transactions": [transaction("T1")], "next_token": "page-2"}
        return {"transactions": [transaction("T2")], "next_token": None}


class NoFetchClient:
    def __init__(self) -> None:
        self.calls = 0

    def list_transactions_page(self, *_args, **_kwargs):
        self.calls += 1
        raise AssertionError("completed staging must be promoted without an Amazon call")


class Phase2B1HardeningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "operations.sqlite3"
        self.key = Fernet.generate_key().decode()
        self.registry = AmazonSourceRegistry(self.db, self.key)
        self.source = self.registry.upsert({
            "name": "Amazon CA/MX",
            "enabled": True,
            "sync_frequency_minutes": 5,
            "client_id": "client",
            "client_secret": "secret",
            "refresh_token": "token",
        })
        self.registry.record_connection_test(self.source["id"], [
            {
                "marketplaceDetails": {
                    "marketplaceId": "CA", "marketplaceName": "Amazon.ca", "countryCode": "CA",
                },
                "totalAmount": {"currencyCode": "CAD", "currencyAmount": 0},
            },
            {
                "marketplaceDetails": {
                    "marketplaceId": "MX", "marketplaceName": "Amazon.com.mx", "countryCode": "MX",
                },
                "totalAmount": {"currencyCode": "MXN", "currencyAmount": 0},
            },
        ])

    def tearDown(self):
        self.temp.cleanup()

    def create_one_slice_job(self):
        start = datetime(2026, 4, 30, 6, tzinfo=UTC)
        return self.registry.create_transaction_backfill(
            source_id=self.source["id"], marketplace_id="CA", marketplace_name="Amazon.ca",
            currency="CAD", transaction_status="DEFERRED_RELEASED",
            overall_start=start, overall_end=start + timedelta(days=1), slice_hours=24,
            job_type="HISTORICAL_BACKFILL",
        )

    def test_fully_fetched_staging_promotes_without_refetch(self):
        job = self.create_one_slice_job()
        client = TwoPageClient()
        with patch.object(
            self.registry,
            "_backfill_pause_reason",
            side_effect=[None, None, None, "DATABASE_GROWTH_LIMIT"],
        ):
            paused = self.registry.run_transaction_backfill(job["job_id"], client, max_pages=2)

        self.assertEqual(paused["status"], "paused")
        self.assertEqual(client.calls, [None, "page-2"])
        self.registry.resume_transaction_backfill(job["job_id"])
        no_fetch = NoFetchClient()
        completed = self.registry.run_transaction_backfill(job["job_id"], no_fetch, max_pages=1)

        self.assertEqual(no_fetch.calls, 0)
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(self.registry.transaction_visibility(self.source["id"], "CAD")["deferred_released_count"], 2)
        with closing(sqlite3.connect(self.db)) as connection, connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM amazon_transaction_staging").fetchone()[0], 0)

    def test_all_and_selected_currency_share_one_financial_snapshot(self):
        class FakePositionClient:
            def __init__(self, _config):
                pass

            def get_current_balances(self, _rates):
                return {
                    "status": "ready", "mode": "amazon_open_balances", "currency": "USD",
                    "as_of": "2026-08-05T08:48:11Z",
                    "totals_by_currency": {"CAD": "100.00", "MXN": "10.00"},
                    "amounts": {"current_balance": "1.00"}, "financial_values": [],
                }

            def get_financial_position(self, _marketplace_id, _rate, *, expected_currency=None):
                return {
                    "status": "ready", "currency": "USD", "source_currency": expected_currency,
                    "as_of": "2026-08-05T08:48:53Z",
                    "totals_by_currency": {expected_currency: "200.00"},
                    "amounts": {"standard_balance": "200.00"},
                    "source_amounts": {"standard_balance": "200.00"},
                    "financial_values": [{"type": "OPEN_BALANCE", "amount": "200.00", "currency": expected_currency}],
                }

        service = Phase0Service(AppConfig(database_path=self.db))
        with (
            patch.dict("os.environ", {"AI_CASHFLOW_MASTER_KEY": self.key}),
            patch("ai_cashflow.api.services.AmazonSpApiClient", FakePositionClient),
        ):
            all_currencies = service.get_amazon_financial_position(self.source["id"])
            selected_cad = service.get_amazon_financial_position(self.source["id"], "CAD")

        self.assertEqual(all_currencies["snapshot_id"], selected_cad["snapshot_id"])
        self.assertEqual(all_currencies["totals_by_currency"]["CAD"], selected_cad["totals_by_currency"]["CAD"])
        self.assertEqual(selected_cad["currency_scope"], "CAD")

    def test_new_transactions_use_normalized_columns_without_duplicate_json(self):
        retrieved = datetime(2026, 8, 5, 8, tzinfo=UTC)
        self.registry.record_transaction_snapshot(
            self.source["id"], "CA", "Amazon.ca", "CAD", "DEFERRED_RELEASED",
            [transaction("T1", "12.34")], retrieved,
        )

        with closing(sqlite3.connect(self.db)) as connection, connection:
            observation = connection.execute(
                "SELECT payload_json,total_amount,transaction_type,posted_date FROM amazon_transaction_observations"
            ).fetchone()
            state = connection.execute(
                "SELECT payload_json,total_amount,transaction_type,posted_date FROM amazon_transaction_state"
            ).fetchone()
            payload = connection.execute(
                "SELECT uncompressed_bytes,compressed_bytes FROM amazon_transaction_payloads"
            ).fetchone()

        self.assertEqual(observation, ("", "12.34", "Shipment", "2026-04-30T12:00:00Z"))
        self.assertEqual(state, observation)
        self.assertLess(payload[1], payload[0])
        self.assertEqual(self.registry.transaction_visibility(self.source["id"], "CAD")["deferred_released_amount"], "12.34")

    def test_scheduler_iteration_failure_is_recorded_and_loop_continues(self):
        from ai_cashflow.api import app as api_app

        calls = 0

        async def worker():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("temporary")

        async def stop_after_two(_seconds):
            if calls >= 2:
                raise asyncio.CancelledError

        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(api_app._scheduler_loop(worker, stop_after_two))
        self.assertEqual(calls, 2)

    def test_promotion_failure_keeps_staging_and_retry_is_idempotent(self):
        job = self.create_one_slice_job()
        client = TwoPageClient()
        with patch.object(
            self.registry, "_backfill_pause_reason",
            side_effect=[None, None, None, "TEST_HOLD"],
        ):
            self.registry.run_transaction_backfill(job["job_id"], client, max_pages=2)
        run_id = self.registry.get_transaction_backfill(job["job_id"])["active_run_id"]
        with patch.object(self.registry, "_transaction_columns", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self.registry._promote_collection_run(run_id)
        with closing(sqlite3.connect(self.db)) as connection, connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM amazon_transaction_staging WHERE run_id=?", (run_id,)
            ).fetchone()[0], 2)
        metrics = self.registry._promote_collection_run(run_id)
        self.assertEqual(metrics["observations_inserted"], 2)
        self.assertEqual(self.registry._promote_collection_run(run_id)["observations_inserted"], 0)

    def test_incomplete_staging_metadata_is_rejected(self):
        job = self.create_one_slice_job()
        run_id = self.registry._create_collection_run(
            kind="backfill", job_type="HISTORICAL_BACKFILL", parent_job_id=job["job_id"],
            source_id=self.source["id"], marketplace_id="CA", marketplace_name="Amazon.ca",
            currency="CAD", transaction_status="DEFERRED_RELEASED",
            coverage_start=datetime(2026, 4, 30, 6, tzinfo=UTC),
            coverage_end=datetime(2026, 5, 1, 6, tzinfo=UTC),
        )
        self.registry._stage_transaction_page(run_id, [transaction("T1")], None, None)
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute(
                "UPDATE amazon_transaction_runs SET transactions_received=2 WHERE run_id=?", (run_id,)
            )
        with self.assertRaisesRegex(ValueError, "metadata"):
            self.registry._promote_collection_run(run_id)

    def test_additive_storage_migration_preserves_subtotal(self):
        self.registry.record_transaction_snapshot(
            self.source["id"], "CA", "Amazon.ca", "CAD", "DEFERRED_RELEASED",
            [transaction("T1", "12.34")], datetime(2026, 8, 5, 8, tzinfo=UTC),
        )
        with closing(sqlite3.connect(self.db)) as connection, connection:
            compressed = connection.execute(
                "SELECT payload_zlib FROM amazon_transaction_payloads"
            ).fetchone()[0]
            payload_json = zlib.decompress(compressed).decode()
            for table in ("amazon_transaction_observations", "amazon_transaction_state"):
                connection.execute(
                    f"UPDATE {table} SET payload_json=?,total_amount=NULL,composition_json='{{}}'",
                    (payload_json,),
                )
        before = self.registry.transaction_visibility(self.source["id"], "CAD")
        migrated = self.registry.migrate_transaction_storage(batch_size=1)
        after = self.registry.transaction_visibility(self.source["id"], "CAD")
        self.assertEqual(migrated["status"], "COMPLETE")
        self.assertEqual(before["deferred_released_amount"], after["deferred_released_amount"])

    def test_open_balance_sync_can_skip_transaction_collection(self):
        class ReportsOnlyClient:
            def list_settlement_reports(self):
                return []

        with patch.dict("os.environ", {"AI_CASHFLOW_TRANSACTION_COLLECTION_ENABLED": "true"}):
            result = self.registry.sync_source(
                self.source["id"], ReportsOnlyClient(), self.root / "samples",
                collect_transactions=False,
            )
        self.assertEqual(result["open_balance_sync_status"], "completed")
        self.assertEqual(result["transaction_observations"], 0)

    def test_scheduler_diagnostics_record_missed_cycle_and_lock_owner(self):
        old = datetime(2026, 8, 5, 8, tzinfo=UTC)
        self.registry.record_scheduler_state(
            "open_balance_sync", completed_at=old.isoformat(), duration_ms=10,
        )
        self.registry.record_scheduler_state(
            "open_balance_sync", started_at=(old + timedelta(minutes=6)).isoformat(),
            lock_owner="open_balance_scheduler", lock_run_id="run-1",
        )
        with closing(sqlite3.connect(self.db)) as connection, connection:
            row = connection.execute(
                "SELECT missed_cycle_count,lock_owner,lock_run_id FROM amazon_scheduler_state"
            ).fetchone()
        self.assertEqual(row, (1, "open_balance_scheduler", "run-1"))


if __name__ == "__main__":
    unittest.main()
