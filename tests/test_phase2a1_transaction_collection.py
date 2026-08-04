import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

from ai_cashflow.integrations import AmazonSourceRegistry


UTC = timezone.utc


def transaction(transaction_id: str, status: str = "DEFERRED", amount: str = "1.00"):
    return {
        "transactionId": transaction_id,
        "transactionStatus": status,
        "transactionType": "Shipment",
        "postedDate": "2026-08-01T00:00:00Z",
        "totalAmount": {"currencyCode": "CAD", "currencyAmount": amount},
        "relatedIdentifiers": [
            {"relatedIdentifierName": "ORDER_ID", "relatedIdentifierValue": "ORDER-1"}
        ],
        "breakdowns": [{
            "breakdownType": "Principal",
            "breakdownAmount": {"currencyCode": "CAD", "currencyAmount": amount},
        }],
    }


class PagedClient:
    def __init__(self, pages=None, failure=None):
        self.pages = pages or {}
        self.failure = failure
        self.calls = []

    def list_settlement_reports(self):
        return []

    def list_transactions_page(
        self, marketplace_id, status, posted_after, posted_before, next_token=None
    ):
        self.calls.append((marketplace_id, status, posted_after, posted_before, next_token))
        if self.failure:
            raise self.failure
        return self.pages.get(next_token, {"transactions": [], "next_token": None, "rate_limit": "0.5"})


class Phase2A1TransactionCollectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "operations.sqlite3"
        self.key = Fernet.generate_key().decode()
        self.registry = AmazonSourceRegistry(self.db, self.key)
        self.source = self.registry.upsert({
            "name": "Canada",
            "client_id": "a",
            "client_secret": "b",
            "refresh_token": "c",
        })
        self.registry.record_connection_test(self.source["id"], [{
            "marketplaceDetails": {"marketplaceId": "CA", "marketplaceName": "Amazon.ca"},
            "totalAmount": {"currencyCode": "CAD", "currencyAmount": 0},
        }])
        self.start = datetime(2026, 8, 1, tzinfo=UTC)
        self.end = datetime(2026, 8, 4, tzinfo=UTC)

    def tearDown(self):
        self.temp.cleanup()

    def create_job(self, **overrides):
        values = {
            "source_id": self.source["id"],
            "marketplace_id": "CA",
            "marketplace_name": "Amazon.ca",
            "currency": "CAD",
            "transaction_status": "DEFERRED",
            "overall_start": self.start,
            "overall_end": self.end,
            "slice_hours": 24,
        }
        values.update(overrides)
        return self.registry.create_transaction_backfill(**values)

    def test_backfill_splits_a_large_interval_into_immutable_daily_slices(self):
        job = self.create_job()

        self.assertEqual(job["current_slice_start"], self.start.isoformat())
        self.assertEqual(job["current_slice_end"], (self.start + timedelta(days=1)).isoformat())

    def test_backfill_yields_after_page_budget_and_resumes_persisted_next_token(self):
        client = PagedClient({
            None: {"transactions": [transaction("T1")], "next_token": "page-2", "rate_limit": "0.5"},
            "page-2": {"transactions": [transaction("T2")], "next_token": None, "rate_limit": "0.5"},
        })
        job = self.create_job(overall_end=self.start + timedelta(days=1))

        first = self.registry.run_transaction_backfill(job["job_id"], client, max_pages=1)
        restarted = AmazonSourceRegistry(self.db, self.key)
        second = restarted.run_transaction_backfill(job["job_id"], client, max_pages=1)

        self.assertEqual(first["status"], "running")
        self.assertEqual(first["next_token"], "page-2")
        self.assertEqual(second["status"], "completed")
        self.assertEqual(self.registry.transaction_visibility(self.source["id"], "CAD")["deferred_count"], 2)

    def test_backfill_can_pause_and_resume_without_losing_progress(self):
        job = self.create_job()
        paused = self.registry.pause_transaction_backfill(job["job_id"])
        resumed = self.registry.resume_transaction_backfill(job["job_id"])

        self.assertEqual(paused["status"], "paused")
        self.assertEqual(resumed["status"], "running")

    def test_backfill_processes_more_than_100_pages_without_truncation(self):
        class LargeClient(PagedClient):
            def list_transactions_page(self, marketplace_id, status, posted_after, posted_before, next_token=None):
                page = int(next_token or 0)
                following = str(page + 1) if page < 100 else None
                return {
                    "transactions": [transaction(f"T-{page}")],
                    "next_token": following,
                    "rate_limit": "0.5",
                }

        job = self.create_job(overall_end=self.start + timedelta(days=1))
        client = LargeClient()
        for _ in range(101):
            result = self.registry.run_transaction_backfill(job["job_id"], client, max_pages=1)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["pages_completed"], 101)
        self.assertEqual(self.registry.transaction_visibility(self.source["id"], "CAD")["count"], 101)

    def test_failed_backfill_page_resumes_from_the_persisted_token(self):
        job = self.create_job(overall_end=self.start + timedelta(days=1))
        first = PagedClient({
            None: {"transactions": [transaction("T1")], "next_token": "page-2", "rate_limit": "0.5"},
        })
        self.registry.run_transaction_backfill(job["job_id"], first, max_pages=1)
        failed = self.registry.run_transaction_backfill(
            job["job_id"], PagedClient(failure=RuntimeError("temporary")), max_pages=1
        )
        self.registry.resume_transaction_backfill(job["job_id"])
        recovered = self.registry.run_transaction_backfill(
            job["job_id"], PagedClient({
                "page-2": {"transactions": [transaction("T2")], "next_token": None, "rate_limit": "0.5"},
            }), max_pages=1,
        )

        self.assertEqual(failed["status"], "failed")
        self.assertEqual(recovered["status"], "completed")
        self.assertEqual(self.registry.transaction_visibility(self.source["id"], "CAD")["count"], 2)

    def test_partial_backfill_pages_are_not_visible_until_slice_completes(self):
        client = PagedClient({
            None: {"transactions": [transaction("T1")], "next_token": "page-2", "rate_limit": "0.5"},
        })
        job = self.create_job(overall_end=self.start + timedelta(days=1))

        self.registry.run_transaction_backfill(job["job_id"], client, max_pages=1)

        self.assertEqual(self.registry.transaction_visibility(self.source["id"], "CAD")["deferred_count"], 0)

    def test_legacy_partial_rows_without_a_completed_run_are_not_visible(self):
        payload = '{"transactionId":"legacy","transactionStatus":"DEFERRED","totalAmount":{"currencyCode":"CAD","currencyAmount":"99"}}'
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                """INSERT INTO amazon_transaction_state
                   (source_id, marketplace_id, transaction_id, marketplace_name, currency,
                    status, retrieved_at, payload_json, fingerprint, conflict)
                   VALUES (?, 'CA', 'legacy', 'Amazon.ca', 'CAD', 'DEFERRED', ?, ?, 'legacy', 0)""",
                (self.source["id"], self.start.isoformat(), payload),
            )

        self.assertEqual(self.registry.transaction_visibility(self.source["id"], "CAD")["count"], 0)

    def test_repeated_identical_observation_is_stored_once(self):
        retrieved = datetime(2026, 8, 4, 10, tzinfo=UTC)
        for offset in (0, 5, 10):
            self.registry.record_transaction_snapshot(
                self.source["id"], "CA", "Amazon.ca", "CAD", "DEFERRED",
                [transaction("T1")], retrieved + timedelta(minutes=offset),
            )

        result = self.registry.transaction_visibility(self.source["id"], "CAD")

        self.assertEqual(result["observation_count"], 1)
        self.assertEqual(result["deferred_count"], 1)

    def test_financial_change_adds_one_observation_and_updates_current_state(self):
        retrieved = datetime(2026, 8, 4, 10, tzinfo=UTC)
        self.registry.record_transaction_snapshot(
            self.source["id"], "CA", "Amazon.ca", "CAD", "DEFERRED",
            [transaction("T1", amount="1.00")], retrieved,
        )
        self.registry.record_transaction_snapshot(
            self.source["id"], "CA", "Amazon.ca", "CAD", "DEFERRED",
            [transaction("T1", amount="2.00")], retrieved + timedelta(minutes=5),
        )

        result = self.registry.transaction_visibility(self.source["id"], "CAD")

        self.assertEqual(result["observation_count"], 2)
        self.assertEqual(result["deferred_amount"], "2.00")

    def test_failed_incremental_run_does_not_advance_successful_checkpoint(self):
        now = datetime(2026, 8, 4, 12, tzinfo=UTC)
        with patch.dict("os.environ", {"AI_CASHFLOW_TRANSACTION_COLLECTION_ENABLED": "true"}):
            first = self.registry.run_incremental_transaction_collection(
                self.source["id"], PagedClient(), now=now, max_pages=3
            )
            before = self.registry.transaction_collection_diagnostics(self.source["id"])
            failed = self.registry.run_incremental_transaction_collection(
                self.source["id"], PagedClient(failure=RuntimeError("upstream")),
                now=now + timedelta(hours=1), max_pages=3,
            )
            after = self.registry.transaction_collection_diagnostics(self.source["id"])

        self.assertEqual(first["status"], "completed")
        self.assertEqual(failed["status"], "partial")
        self.assertEqual(before["checkpoints"], after["checkpoints"])

    def test_incremental_overlap_does_not_duplicate_observations_or_totals(self):
        page = {"transactions": [transaction("T1")], "next_token": None, "rate_limit": "0.5"}
        now = datetime(2026, 8, 4, 12, tzinfo=UTC)
        with patch.dict("os.environ", {"AI_CASHFLOW_TRANSACTION_COLLECTION_ENABLED": "true"}):
            self.registry.run_incremental_transaction_collection(
                self.source["id"], PagedClient({None: page}), now=now, max_pages=10
            )
            self.registry.run_incremental_transaction_collection(
                self.source["id"], PagedClient({None: page}), now=now + timedelta(hours=1), max_pages=10
            )

        result = self.registry.transaction_visibility(self.source["id"], "CAD")
        self.assertEqual(result["observation_count"], 1)
        self.assertEqual(result["deferred_amount"], "1.00")

    def test_ordinary_source_sync_uses_incremental_pages_not_historical_backfill(self):
        client = PagedClient()
        with patch.dict("os.environ", {"AI_CASHFLOW_TRANSACTION_COLLECTION_ENABLED": "true"}):
            result = self.registry.sync_source(self.source["id"], client, self.root / "samples")

        self.assertEqual(result["open_balance_sync_status"], "completed")
        self.assertFalse(self.registry.list_transaction_backfills(self.source["id"]))
        self.assertTrue(client.calls)
        self.assertLessEqual(client.calls[0][3] - client.calls[0][2], timedelta(days=2))

    def test_status_regression_is_retained_as_conflict_but_not_promoted(self):
        first = datetime(2026, 8, 4, 10, tzinfo=UTC)
        self.registry.record_transaction_snapshot(
            self.source["id"], "CA", "Amazon.ca", "CAD", "DEFERRED_RELEASED",
            [transaction("T1", "DEFERRED_RELEASED")], first,
        )
        self.registry.record_transaction_snapshot(
            self.source["id"], "CA", "Amazon.ca", "CAD", "DEFERRED",
            [transaction("T1", "DEFERRED")], first + timedelta(minutes=5),
        )

        result = self.registry.transaction_visibility(self.source["id"], "CAD")

        self.assertEqual(result["deferred_count"], 0)
        self.assertEqual(result["deferred_released_count"], 0)
        self.assertEqual(result["conflict_count"], 1)

    def test_repeated_token_fails_without_promoting_partial_state(self):
        client = PagedClient({
            None: {"transactions": [transaction("T1")], "next_token": "same", "rate_limit": "0.5"},
            "same": {"transactions": [transaction("T2")], "next_token": "same", "rate_limit": "0.5"},
        })
        job = self.create_job(overall_end=self.start + timedelta(days=1))

        self.registry.run_transaction_backfill(job["job_id"], client, max_pages=1)
        result = self.registry.run_transaction_backfill(job["job_id"], client, max_pages=1)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.registry.transaction_visibility(self.source["id"], "CAD")["count"], 0)

    def test_50001_identical_transactions_do_not_multiply_on_repeated_sync(self):
        rows = [transaction(f"T-{index}") for index in range(50_001)]
        retrieved = datetime(2026, 8, 4, 10, tzinfo=UTC)
        self.registry.record_transaction_snapshot(
            self.source["id"], "CA", "Amazon.ca", "CAD", "DEFERRED", rows, retrieved
        )
        self.registry.record_transaction_snapshot(
            self.source["id"], "CA", "Amazon.ca", "CAD", "DEFERRED", rows,
            retrieved + timedelta(minutes=5),
        )

        with sqlite3.connect(self.db) as connection:
            observations = connection.execute(
                "SELECT COUNT(*) FROM amazon_transaction_observations"
            ).fetchone()[0]
            states = connection.execute(
                "SELECT COUNT(*) FROM amazon_transaction_state"
            ).fetchone()[0]

        self.assertEqual(observations, 50_001)
        self.assertEqual(states, 50_001)


if __name__ == "__main__":
    unittest.main()
