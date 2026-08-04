from decimal import Decimal
import unittest

from ai_cashflow.config import AppConfig


class ConfigTests(unittest.TestCase):
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
