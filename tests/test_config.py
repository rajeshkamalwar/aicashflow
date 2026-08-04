from decimal import Decimal
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from ai_cashflow.config import AppConfig


class ConfigTests(unittest.TestCase):
    def test_transaction_visibility_is_disabled_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(AppConfig().transaction_visibility_enabled)

    def test_transaction_visibility_can_be_enabled_explicitly(self):
        with patch.dict(os.environ, {"AI_CASHFLOW_TRANSACTION_VISIBILITY_ENABLED": "true"}, clear=True):
            self.assertTrue(AppConfig().transaction_visibility_enabled)

    def test_marketplace_ar_configuration_paths_can_live_outside_the_release(self):
        with patch.dict(os.environ, {
            "AI_CASHFLOW_MARKETPLACE_AR_REGISTRY_PATH": "/var/lib/aicashflow/config/marketplace_ar_sources.csv",
            "AI_CASHFLOW_MARKETPLACE_AR_FX_PATH": "/var/lib/aicashflow/config/marketplace_ar_fx_rates.csv",
        }):
            config = AppConfig()

        self.assertEqual(config.marketplace_ar_registry_path, Path("/var/lib/aicashflow/config/marketplace_ar_sources.csv"))
        self.assertEqual(config.marketplace_ar_fx_path, Path("/var/lib/aicashflow/config/marketplace_ar_fx_rates.csv"))

    def test_default_config_points_to_phase0_paths(self):
        config = AppConfig()

        self.assertEqual(config.samples_dir.name, "samples")
        self.assertEqual(config.reports_dir.name, "phase0")
        self.assertIn("USD", config.supported_currencies)
        self.assertEqual(config.date_tolerance_days, 3)
        self.assertEqual(config.amount_tolerance, Decimal("0.00"))
        self.assertGreater(config.tenant.reconciliation.usd_exchange_rates["JPY"], 0)
        self.assertGreater(config.tenant.reconciliation.usd_exchange_rates["SGD"], 0)


if __name__ == "__main__":
    unittest.main()
