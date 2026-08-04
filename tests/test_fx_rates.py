import json
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from ai_cashflow.fx_rates import (
    build_fx_dataset,
    promote_fx_files,
    validate_fx_dataset,
)


ECB_XML = b"""<?xml version='1.0'?>
<Envelope xmlns='http://www.ecb.int/vocabulary/2002-08-01/eurofxref'>
  <Cube><Cube time='2026-08-04'>
    <Cube currency='USD' rate='1.1515'/>
    <Cube currency='CAD' rate='1.6191'/>
    <Cube currency='BRL' rate='5.8503'/>
    <Cube currency='MXN' rate='19.9072'/>
    <Cube currency='JPY' rate='181.26'/>
  </Cube></Cube>
</Envelope>"""

CBUAE_HTML = """
<table><tbody><tr><td>US Dollar</td><td class='value'>3.6725</td></tr></tbody></table>
"""


class FxRateTests(unittest.TestCase):
    def _dataset(self, required=None, now=None):
        return build_fx_dataset(
            ECB_XML,
            CBUAE_HTML,
            required_currencies=set(required or {"USD", "CAD", "BRL", "MXN", "JPY", "AED"}),
            retrieved_at=now or datetime(2026, 8, 5, tzinfo=timezone.utc),
            max_age_days=7,
        )

    def test_current_rates_are_decimal_complete_and_cross_converted(self):
        dataset = self._dataset()

        self.assertEqual(dataset.effective_date.isoformat(), "2026-08-04")
        self.assertEqual(dataset.base_currency, "USD")
        self.assertEqual(dataset.rates["USD"], Decimal("1"))
        self.assertEqual(dataset.rates["CAD"], Decimal("1.1515") / Decimal("1.6191"))
        self.assertEqual(dataset.rates["AED"], Decimal("1") / Decimal("3.6725"))
        self.assertEqual(dataset.status, "current")

    def test_rates_older_than_limit_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "older than 7 days"):
            self._dataset(now=datetime(2026, 8, 12, 0, 0, 1, tzinfo=timezone.utc))

    def test_missing_required_currency_fails_but_missing_unused_currency_passes(self):
        dataset = self._dataset(required={"USD", "CAD"})
        validate_fx_dataset(dataset, {"USD", "CAD"}, datetime(2026, 8, 5, tzinfo=timezone.utc), 7)
        with self.assertRaisesRegex(ValueError, "required currencies.*CHF"):
            validate_fx_dataset(dataset, {"USD", "CHF"}, datetime(2026, 8, 5, tzinfo=timezone.utc), 7)

    def test_invalid_currency_and_non_positive_or_non_decimal_rates_fail(self):
        for currency, rate, message in (("US", "1", "currency code"), ("CAD", "0", "positive"), ("CAD", "-1", "positive"), ("CAD", "bad", "Decimal")):
            xml = ECB_XML.replace(b"currency='CAD' rate='1.6191'", f"currency='{currency}' rate='{rate}'".encode())
            with self.subTest(currency=currency, rate=rate):
                with self.assertRaisesRegex(ValueError, message):
                    build_fx_dataset(xml, CBUAE_HTML, {"USD"}, datetime(2026, 8, 5, tzinfo=timezone.utc), 7)

    def test_future_effective_date_and_duplicate_currency_fail(self):
        future = ECB_XML.replace(b"2026-08-04", b"2026-08-06")
        duplicate = ECB_XML.replace(
            b"<Cube currency='CAD' rate='1.6191'/>",
            b"<Cube currency='CAD' rate='1.6191'/><Cube currency='CAD' rate='1.6191'/>",
        )
        with self.assertRaisesRegex(ValueError, "future"):
            build_fx_dataset(future, CBUAE_HTML, {"USD"}, datetime(2026, 8, 5, tzinfo=timezone.utc), 7)
        with self.assertRaisesRegex(ValueError, "duplicate currency CAD"):
            build_fx_dataset(duplicate, CBUAE_HTML, {"USD"}, datetime(2026, 8, 5, tzinfo=timezone.utc), 7)

    def test_atomic_promotion_updates_both_files_and_preserves_backups(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            tenant = root / "tenant.json"
            market_fx = root / "marketplace.csv"
            backup = root / "backups"
            tenant.write_text(json.dumps({"reconciliation": {"usd_exchange_rates": {"USD": "1"}, "usd_exchange_rates_as_of": "2026-07-16"}}), encoding="utf-8")
            market_fx.write_text("rate_date,currency,usd_rate,rate_source\n2026-07-09,USD,1,Fixed USD\n", encoding="utf-8")

            result = promote_fx_files(tenant, market_fx, self._dataset(), backup)

            updated = json.loads(tenant.read_text(encoding="utf-8"))["reconciliation"]
            self.assertEqual(updated["usd_exchange_rates_as_of"], "2026-08-04")
            self.assertIn("CAD", updated["usd_exchange_rates"])
            self.assertIn("retrieved_at", updated["usd_exchange_rate_metadata"])
            self.assertIn("2026-08-04,CAD", market_fx.read_text(encoding="utf-8"))
            self.assertTrue(result["tenant_backup"].exists())
            self.assertTrue(result["marketplace_backup"].exists())

    def test_failed_validation_preserves_previous_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            tenant = root / "tenant.json"
            market_fx = root / "marketplace.csv"
            tenant.write_text('{"sentinel":"tenant"}', encoding="utf-8")
            market_fx.write_text("sentinel,marketplace\n", encoding="utf-8")
            before = (tenant.read_bytes(), market_fx.read_bytes())
            dataset = self._dataset(required={"USD"})

            with self.assertRaisesRegex(ValueError, "required currencies.*CHF"):
                promote_fx_files(tenant, market_fx, dataset, root / "backups", required_currencies={"USD", "CHF"})

            self.assertEqual((tenant.read_bytes(), market_fx.read_bytes()), before)

    def test_daily_timer_only_runs_the_fx_refresh(self):
        root = Path(__file__).resolve().parents[1]
        service = (root / "scripts/systemd/aicashflow-fx-refresh.service").read_text(encoding="utf-8")
        timer = (root / "scripts/systemd/aicashflow-fx-refresh.timer").read_text(encoding="utf-8")

        self.assertIn("runuser --user aicashflow", service)
        self.assertIn("-m ai_cashflow.fx_rates", service)
        self.assertNotIn("backfill", service.lower())
        self.assertIn("try-restart aicashflow.service", service)
        self.assertIn("OnCalendar=*-*-*", timer)
        self.assertIn("Persistent=true", timer)
