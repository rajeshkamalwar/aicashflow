import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.fernet import Fernet

from ai_cashflow.integrations import AmazonSourceRegistry


UTC = timezone.utc


def transaction(
    transaction_id: str,
    status: str = "DEFERRED",
    amount: str = "1.00",
    posted_date: str = "2026-08-01T00:00:00Z",
):
    return {
        "transactionId": transaction_id,
        "transactionStatus": status,
        "transactionType": "Shipment",
        "postedDate": posted_date,
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
            "job_type": "HISTORICAL_BACKFILL",
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

    def test_completed_historical_slices_mark_coverage_complete(self):
        checkpoint_start = self.end + timedelta(hours=14)
        checkpoint_end = checkpoint_start + timedelta(hours=6)
        self.registry.record_transaction_snapshot(
            self.source["id"], "CA", "Amazon.ca", "CAD", "DEFERRED", [], checkpoint_end,
            coverage_start=checkpoint_start, coverage_end=checkpoint_end,
        )
        job = self.create_job()
        client = PagedClient()
        for _ in range(3):
            job = self.registry.run_transaction_backfill(job["job_id"], client)

        visibility = self.registry.transaction_visibility(self.source["id"], "CAD")
        coverage = visibility["coverage"]

        self.assertEqual(job["status"], "completed")
        self.assertTrue(coverage["is_complete"])
        self.assertFalse(coverage["has_gaps"])
        self.assertEqual(coverage["classification"], "COMPLETE_COVERAGE")
        self.assertEqual(coverage["coverage_percentage"], 100)
        self.assertEqual(coverage["completed_slice_count"], 3)
        self.assertEqual(coverage["completed_start"], self.start.isoformat())
        self.assertEqual(coverage["completed_end"], checkpoint_end.isoformat())
        self.assertFalse(visibility["totals_partial"])

    def test_completed_canary_preserves_observed_coverage_without_starting_backfill(self):
        checkpoint_start = self.end + timedelta(hours=14)
        checkpoint_end = checkpoint_start + timedelta(hours=6)
        self.registry.record_transaction_snapshot(
            self.source["id"], "CA", "Amazon.ca", "CAD", "DEFERRED", [], checkpoint_end,
            coverage_start=checkpoint_start, coverage_end=checkpoint_end,
        )
        job = self.create_job(
            job_type="CANARY",
            overall_end=self.start + timedelta(hours=1),
            slice_hours=1,
        )
        self.registry.run_transaction_backfill(job["job_id"], PagedClient())

        visibility = self.registry.transaction_visibility(self.source["id"], "CAD")
        diagnostics = self.registry.transaction_collection_diagnostics(self.source["id"])

        self.assertEqual(visibility["coverage"]["historical_backfill_status"], "NOT_STARTED")
        self.assertFalse(visibility["coverage"]["is_historically_complete"])
        self.assertEqual(visibility["coverage"]["observed_start"], checkpoint_start.isoformat())
        self.assertEqual(len(diagnostics["validation_canaries"]), 1)
        self.assertFalse(diagnostics["historical_backfills"])

    def test_multiple_completed_canaries_do_not_increase_historical_progress(self):
        for offset in (0, 2):
            job = self.create_job(
                job_type="CANARY",
                overall_start=self.start + timedelta(hours=offset),
                overall_end=self.start + timedelta(hours=offset + 1),
                slice_hours=1,
            )
            self.registry.run_transaction_backfill(job["job_id"], PagedClient())

        coverage = self.registry.transaction_visibility(self.source["id"], "CAD")["coverage"]

        self.assertEqual(coverage["historical_backfill_status"], "NOT_STARTED")
        self.assertEqual(coverage["completed_slice_count"], 0)
        self.assertIsNone(coverage["coverage_percentage"])

    def test_legacy_unclassified_job_is_not_historical_completion(self):
        job = self.create_job(overall_end=self.start + timedelta(hours=1), slice_hours=1)
        self.registry.run_transaction_backfill(job["job_id"], PagedClient())
        with sqlite3.connect(self.db) as connection:
            run_id = connection.execute(
                "SELECT run_id FROM amazon_transaction_runs WHERE source_id=? AND job_type='HISTORICAL_BACKFILL'",
                (self.source["id"],),
            ).fetchone()[0]
            connection.execute(
                "UPDATE amazon_transaction_backfill_jobs SET job_type='LEGACY_UNCLASSIFIED' WHERE job_id=?",
                (job["job_id"],),
            )
            connection.execute(
                "UPDATE amazon_transaction_runs SET job_type='LEGACY_UNCLASSIFIED' WHERE run_id=?",
                (run_id,),
            )

        coverage = self.registry.transaction_visibility(self.source["id"], "CAD")["coverage"]
        diagnostics = self.registry.transaction_collection_diagnostics(self.source["id"])

        self.assertEqual(coverage["historical_backfill_status"], "NOT_STARTED")
        self.assertEqual(len(diagnostics["legacy_unclassified"]), 1)

    def test_incremental_runs_are_classified_separately(self):
        result = self.registry.run_incremental_transaction_collection(
            self.source["id"], PagedClient(), now=self.end + timedelta(hours=1), max_pages=6,
        )
        diagnostics = self.registry.transaction_collection_diagnostics(self.source["id"])

        self.assertEqual(result["status"], "completed")
        self.assertTrue(diagnostics["incremental_collection"]["runs"])
        self.assertTrue(all(
            row["job_type"] == "INCREMENTAL_SYNC"
            for row in diagnostics["incremental_collection"]["runs"]
        ))
        self.assertFalse(diagnostics["historical_backfills"])

    def test_incomplete_historical_slices_are_partially_complete(self):
        job = self.create_job()
        self.registry.run_transaction_backfill(job["job_id"], PagedClient(), max_pages=1)

        visibility = self.registry.transaction_visibility(self.source["id"], "CAD")
        coverage = visibility["coverage"]

        self.assertEqual(coverage["historical_backfill_status"], "PARTIALLY_COMPLETE")
        self.assertEqual(coverage["completed_slice_count"], 1)
        self.assertEqual(coverage["pending_slice_count"], 2)
        self.assertTrue(coverage["has_gaps"])
        self.assertFalse(coverage["is_historically_complete"])
        self.assertTrue(visibility["totals_partial"])

    def test_failed_historical_slice_prevents_complete(self):
        job = self.create_job(overall_end=self.start + timedelta(days=1))
        self.registry.run_transaction_backfill(
            job["job_id"], PagedClient(failure=RuntimeError("temporary")), max_pages=1,
        )

        coverage = self.registry.transaction_visibility(self.source["id"], "CAD")["coverage"]

        self.assertEqual(coverage["historical_backfill_status"], "FAILED")
        self.assertEqual(coverage["failed_slice_count"], 1)
        self.assertFalse(coverage["is_historically_complete"])

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
        event = self.registry.transaction_collection_diagnostics(self.source["id"])["status_events"][0]

        self.assertEqual(result["deferred_count"], 0)
        self.assertEqual(result["deferred_released_count"], 0)
        self.assertEqual(result["conflict_count"], 1)
        self.assertEqual(event["conflict_category"], "STATUS_REGRESSION")

    def test_deferred_to_deferred_released_is_the_documented_progression(self):
        first = datetime(2026, 8, 4, 10, tzinfo=UTC)
        self.registry.record_transaction_snapshot(
            self.source["id"], "CA", "Amazon.ca", "CAD", "DEFERRED",
            [transaction("T1", "DEFERRED")], first,
        )
        self.registry.record_transaction_snapshot(
            self.source["id"], "CA", "Amazon.ca", "CAD", "DEFERRED_RELEASED",
            [transaction("T1", "DEFERRED_RELEASED", posted_date="2026-08-02T00:00:00Z")],
            first + timedelta(minutes=5),
        )

        result = self.registry.transaction_visibility(self.source["id"], "CAD")

        self.assertEqual(result["deferred_count"], 0)
        self.assertEqual(result["deferred_released_count"], 1)
        self.assertEqual(result["conflict_count"], 0)

    def test_deferred_to_released_is_unusual_and_excluded(self):
        first = datetime(2026, 8, 4, 10, tzinfo=UTC)
        self.registry.record_transaction_snapshot(
            self.source["id"], "CA", "Amazon.ca", "CAD", "DEFERRED",
            [transaction("T1", "DEFERRED")], first,
        )
        self.registry.record_transaction_snapshot(
            self.source["id"], "CA", "Amazon.ca", "CAD", "RELEASED",
            [transaction("T1", "RELEASED", posted_date="2026-08-02T00:00:00Z")],
            first + timedelta(minutes=5),
        )

        result = self.registry.transaction_visibility(self.source["id"], "CAD")
        event = self.registry.transaction_collection_diagnostics(self.source["id"])["status_events"][0]

        self.assertEqual(result["count"], 0)
        self.assertEqual(result["released_count"], 0)
        self.assertTrue(result["totals_partial"])
        self.assertEqual(event["previous_status"], "DEFERRED")
        self.assertEqual(event["observed_status"], "RELEASED")
        self.assertEqual(event["conflict_category"], "UNUSUAL_DEFERRED_TO_RELEASED")
        self.assertFalse(event["included_in_totals"])
        self.assertNotEqual(event["transaction_id_masked"], "T1")
        self.assertTrue(event["observation_timestamp"])
        self.assertTrue(event["retrieval_timestamp"])
        self.assertTrue(event["resolution_reason"])

    def test_released_to_deferred_released_uses_newer_time_without_double_counting(self):
        first = datetime(2026, 8, 4, 10, tzinfo=UTC)
        self.registry.record_transaction_snapshot(
            self.source["id"], "CA", "Amazon.ca", "CAD", "RELEASED",
            [transaction("T1", "RELEASED")], first,
        )
        self.registry.record_transaction_snapshot(
            self.source["id"], "CA", "Amazon.ca", "CAD", "DEFERRED_RELEASED",
            [transaction("T1", "DEFERRED_RELEASED", posted_date="2026-08-02T00:00:00Z")],
            first + timedelta(minutes=5),
        )

        result = self.registry.transaction_visibility(self.source["id"], "CAD")
        event = self.registry.transaction_collection_diagnostics(self.source["id"])["status_events"][0]

        self.assertEqual(result["count"], 1)
        self.assertEqual(result["released_count"], 0)
        self.assertEqual(result["deferred_released_count"], 1)
        self.assertEqual(event["conflict_category"], "SEMANTIC_RELEASE_STATUS_CHANGE")
        self.assertTrue(event["included_in_totals"])

    def test_released_to_deferred_is_a_regression_conflict(self):
        first = datetime(2026, 8, 4, 10, tzinfo=UTC)
        self.registry.record_transaction_snapshot(
            self.source["id"], "CA", "Amazon.ca", "CAD", "RELEASED",
            [transaction("T1", "RELEASED")], first,
        )
        self.registry.record_transaction_snapshot(
            self.source["id"], "CA", "Amazon.ca", "CAD", "DEFERRED",
            [transaction("T1", "DEFERRED", posted_date="2026-08-02T00:00:00Z")],
            first + timedelta(minutes=5),
        )

        result = self.registry.transaction_visibility(self.source["id"], "CAD")
        event = self.registry.transaction_collection_diagnostics(self.source["id"])["status_events"][0]

        self.assertEqual(result["count"], 0)
        self.assertEqual(event["conflict_category"], "STATUS_REGRESSION")
        self.assertFalse(event["included_in_totals"])

    def test_equal_released_status_timestamps_are_ambiguous_not_fingerprint_ranked(self):
        retrieved = datetime(2026, 8, 4, 10, tzinfo=UTC)
        self.registry.record_transaction_snapshot(
            self.source["id"], "CA", "Amazon.ca", "CAD", "RELEASED",
            [transaction("T1", "RELEASED", amount="1.00")], retrieved,
        )
        self.registry.record_transaction_snapshot(
            self.source["id"], "CA", "Amazon.ca", "CAD", "DEFERRED_RELEASED",
            [transaction("T1", "DEFERRED_RELEASED", amount="9.00")], retrieved,
        )

        result = self.registry.transaction_visibility(self.source["id"], "CAD")
        event = self.registry.transaction_collection_diagnostics(self.source["id"])["status_events"][0]

        self.assertEqual(result["count"], 0)
        self.assertEqual(result["released_count"] + result["deferred_released_count"], 0)
        self.assertEqual(event["conflict_category"], "AMBIGUOUS_RELEASE_STATUS")
        self.assertFalse(event["included_in_totals"])

    def test_combined_released_flow_is_the_sum_of_disjoint_components(self):
        retrieved = datetime(2026, 8, 4, 10, tzinfo=UTC)
        self.registry.record_transaction_snapshot(
            self.source["id"], "CA", "Amazon.ca", "CAD", "RELEASED",
            [transaction("R", "RELEASED", amount="2.00")], retrieved,
        )
        self.registry.record_transaction_snapshot(
            self.source["id"], "CA", "Amazon.ca", "CAD", "DEFERRED_RELEASED",
            [transaction("DR", "DEFERRED_RELEASED", amount="3.00")], retrieved,
        )

        result = self.registry.transaction_visibility(self.source["id"], "CAD")

        self.assertEqual(result["count"], 2)
        self.assertEqual(result["released_flow_count"], 2)
        self.assertEqual(result["released_flow_amount"], "5.00")
        self.assertEqual(
            result["released_flow_count"],
            result["released_count"] + result["deferred_released_count"],
        )

    def test_backfill_pauses_when_database_growth_exceeds_the_limit(self):
        page = {"transactions": [transaction("T1")], "next_token": "next", "rate_limit": "0.5"}
        job = self.create_job(overall_end=self.start + timedelta(hours=1))
        with self.registry._connect() as connection:
            connection.execute(
                "UPDATE amazon_transaction_backfill_jobs SET starting_database_bytes=0 WHERE job_id=?",
                (job["job_id"],),
            )

        with patch.dict("os.environ", {"AI_CASHFLOW_TRANSACTION_BACKFILL_MAX_DB_GROWTH_BYTES": "1"}):
            result = self.registry.run_transaction_backfill(
                job["job_id"], PagedClient({None: page}), max_pages=2
            )

        self.assertEqual(result["status"], "paused")
        self.assertEqual(result["pause_reason"], "DATABASE_GROWTH_LIMIT")

    def test_backfill_pauses_after_persistent_throttling(self):
        pages = {
            None: {"transactions": [transaction("T1")], "next_token": "p2", "rate_limit": "0.5", "throttled_attempts": 1},
            "p2": {"transactions": [transaction("T2")], "next_token": "p3", "rate_limit": "0.5", "throttled_attempts": 1},
        }
        job = self.create_job(overall_end=self.start + timedelta(hours=1))

        with patch.dict("os.environ", {"AI_CASHFLOW_TRANSACTION_BACKFILL_MAX_CONSECUTIVE_THROTTLES": "2"}):
            result = self.registry.run_transaction_backfill(
                job["job_id"], PagedClient(pages), max_pages=3
            )

        self.assertEqual(result["status"], "paused")
        self.assertEqual(result["pause_reason"], "PERSISTENT_AMAZON_THROTTLING")

    def test_backfill_pauses_when_source_sync_health_has_failed(self):
        job = self.create_job(overall_end=self.start + timedelta(hours=1))
        self.registry.record_sync_failure(self.source["id"], "open balance sync failed")

        result = self.registry.run_transaction_backfill(job["job_id"], PagedClient(), max_pages=1)

        self.assertEqual(result["status"], "paused")
        self.assertEqual(result["pause_reason"], "SOURCE_SYNC_UNHEALTHY")

    def test_backfill_pauses_when_free_disk_is_below_the_limit(self):
        job = self.create_job(overall_end=self.start + timedelta(hours=1))

        with patch("ai_cashflow.integrations.shutil.disk_usage", return_value=SimpleNamespace(free=0)):
            result = self.registry.run_transaction_backfill(job["job_id"], PagedClient(), max_pages=1)

        self.assertEqual(result["status"], "paused")
        self.assertEqual(result["pause_reason"], "LOW_DISK_SPACE")

    def test_backfill_pauses_when_conflict_rate_exceeds_the_limit(self):
        retrieved = datetime(2026, 8, 4, 10, tzinfo=UTC)
        self.registry.record_transaction_snapshot(
            self.source["id"], "CA", "Amazon.ca", "CAD", "RELEASED",
            [transaction("T1", "RELEASED")], retrieved,
        )
        job = self.create_job(overall_end=self.start + timedelta(hours=1))
        page = {
            "transactions": [transaction("T1", "DEFERRED", posted_date="2026-08-02T00:00:00Z")],
            "next_token": None,
            "rate_limit": "0.5",
        }

        with patch.dict("os.environ", {"AI_CASHFLOW_TRANSACTION_BACKFILL_MAX_CONFLICT_RATE": "0"}):
            result = self.registry.run_transaction_backfill(
                job["job_id"], PagedClient({None: page}), max_pages=1
            )

        self.assertEqual(result["status"], "paused")
        self.assertEqual(result["pause_reason"], "STATUS_CONFLICT_RATE")

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
