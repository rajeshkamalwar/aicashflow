import tempfile
import unittest
import json
import sqlite3
import gc
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from cryptography.fernet import Fernet

from ai_cashflow.integrations import AmazonSourceRegistry
from ai_cashflow.reporting import generate_phase0_reports


UTC = timezone.utc


def group(group_id: str, amount: str, currency: str = "CAD", *, processing: str = "CLOSED", transfer: str = "SUCCEEDED") -> dict[str, object]:
    return {
        "FinancialEventGroupId": group_id,
        "OriginalTotal": {"CurrencyCode": currency, "CurrencyAmount": amount},
        "ProcessingStatus": processing,
        "FundTransferStatus": transfer,
        "FundTransferDate": "2026-08-02T00:00:00Z",
    }


class CanonicalPayoutStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.registry = AmazonSourceRegistry(
            Path(self.temp.name) / "operations.sqlite3", Fernet.generate_key().decode()
        )
        self.source = self.registry.upsert({
            "name": "Canada", "client_id": "a", "client_secret": "b", "refresh_token": "c",
        })

    def tearDown(self):
        self.registry = None
        gc.collect()
        self.temp.cleanup()

    def test_uninitialized_source_is_not_interpreted_as_empty(self):
        result = self.registry.get_canonical_payout_state(self.source["id"], "CAD")

        self.assertEqual(result["availability"], "DATA_UNAVAILABLE")
        self.assertEqual(result["reason"], "CANONICAL_PAYOUT_STATE_NOT_INITIALIZED")
        self.assertEqual(result["completed_payout_count"], 0)

    def test_initialized_payout_state_is_currency_scoped_and_deterministic(self):
        now = datetime(2026, 8, 13, tzinfo=UTC)
        self.registry.record_financial_event_groups(self.source["id"], [
            group("cad-payout", "10.00"),
            group("usd-payout", "20.00", "USD"),
            group("failed", "30.00", processing="CLOSED", transfer="FAILED"),
            group("charge", "-2.00"),
        ], now, "run-1")

        first = self.registry.get_canonical_payout_state(self.source["id"], "CAD")
        second = self.registry.get_canonical_payout_state(self.source["id"], "CAD")

        self.assertEqual(first["availability"], "DATA_AVAILABLE")
        self.assertEqual(first["completed_payout_count"], 1)
        self.assertEqual(first["completed_payout_total"], "10.00")
        self.assertEqual(first["classification_counts"]["FAILED_SETTLEMENT"], 1)
        self.assertEqual(first["classification_counts"]["COMPLETED_CHARGE"], 1)
        self.assertEqual(first["financial_event_group_snapshot_id"], second["financial_event_group_snapshot_id"])
        self.assertEqual(first["completed_payouts"][0]["currency"], "CAD")

    def test_initialized_empty_source_is_available_empty(self):
        self.registry.record_financial_event_groups(self.source["id"], [], datetime(2026, 8, 13, tzinfo=UTC), "run-1")

        result = self.registry.get_canonical_payout_state(self.source["id"], "CAD")

        self.assertEqual(result["availability"], "DATA_AVAILABLE_EMPTY")
        self.assertIsNone(result["reason"])

    def test_report_uses_the_same_persisted_payout_snapshot(self):
        now = datetime(2026, 8, 13, tzinfo=UTC)
        self.registry.record_financial_event_groups(
            self.source["id"], [group("payout", "10.00")], now, "run-1",
        )
        with sqlite3.connect(self.registry.database_path) as connection:
            connection.execute(
                "create table bank_receipts (receipt_id text, bank_account text, currency text, receipt_date text, amount text, reference text)"
            )
            connection.execute(
                "insert into amazon_transaction_state "
                "(source_id,marketplace_id,transaction_id,marketplace_name,currency,status,retrieved_at,payload_json,fingerprint,conflict,active_run_id,observation_at,conflict_category,included_in_totals,resolution_reason,total_amount,transaction_type,posted_date,composition_json) "
                "values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (self.source["id"], "CA", "transaction", "Amazon.ca", "CAD", "DEFERRED", now.isoformat(), "{}", "fp", 0, "run-1", now.isoformat(), None, 1, None, "1.00", "Order", now.isoformat(), "{}"),
            )
        expected = self.registry.get_canonical_payout_state(self.source["id"])
        output = generate_phase0_reports(
            Path(self.temp.name) / "samples", Path(self.temp.name) / "reports",
            database_path=self.registry.database_path, source_mode="CANONICAL_DATABASE",
            usd_exchange_rates={"USD": Decimal("1"), "CAD": Decimal("1")},
        )
        report = json.loads((output / "report_metadata.json").read_text(encoding="utf-8"))

        state = report["canonical_payouts"]
        self.assertEqual(state["financial_event_group_snapshot_id"], expected["financial_event_group_snapshot_id"])
        self.assertEqual(state["completed_payout_count"], expected["completed_payout_count"])
        self.assertEqual(state["completed_payout_total"], expected["completed_payout_total"])


if __name__ == "__main__":
    unittest.main()
