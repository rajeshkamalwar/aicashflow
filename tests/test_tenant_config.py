from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from ai_cashflow.config import AppConfig, TenantConfig


class TenantConfigTests(unittest.TestCase):
    def test_loads_tenant_config_from_json(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "tenant.local.json"
            path.write_text(
                """
{
  "product_name": "TreasuryOS",
  "tenant_name": "Demo Group",
  "poc_entity": "Demo Entity",
  "demo_mode": false,
  "brand": {
    "primary_color": "#123456",
    "logo_text": "TO"
  },
  "currencies": ["USD", "EUR"],
  "reconciliation": {
    "date_tolerance_days": 5,
    "amount_tolerance": "1.25",
    "usd_exchange_rates": {"USD": "1", "EUR": "1.10"},
    "usd_exchange_rates_as_of": "2026-07-16",
    "usd_exchange_rates_source": "ECB reference rates",
    "usd_exchange_rates_max_age_days": 7
  },
  "sources": {
    "marketplaces": [
      {
        "name": "Marketplace One",
        "type": "API",
        "status": "Configured",
        "owner": "Ops",
        "frequency": "Hourly",
        "evidence_type": "API feed"
      }
    ],
    "banks": [
      {"name": "Bank One", "type": "Bank", "status": "Configured"}
    ]
  }
}
""",
                encoding="utf-8",
            )

            tenant = TenantConfig.from_file(path)

            self.assertEqual(tenant.product_name, "TreasuryOS")
            self.assertEqual(tenant.tenant_name, "Demo Group")
            self.assertEqual(tenant.poc_entity, "Demo Entity")
            self.assertFalse(tenant.demo_mode)
            self.assertEqual(tenant.brand.primary_color, "#123456")
            self.assertEqual(tenant.brand.logo_text, "TO")
            self.assertEqual(tenant.currencies, ["USD", "EUR"])
            self.assertEqual(tenant.reconciliation.date_tolerance_days, 5)
            self.assertEqual(tenant.reconciliation.amount_tolerance, Decimal("1.25"))
            self.assertEqual(
                tenant.reconciliation.usd_exchange_rates["EUR"], Decimal("1.10")
            )
            self.assertEqual(
                tenant.reconciliation.usd_exchange_rates_as_of, "2026-07-16"
            )
            self.assertEqual(
                tenant.reconciliation.usd_exchange_rates_source, "ECB reference rates"
            )
            self.assertEqual(tenant.reconciliation.usd_exchange_rates_max_age_days, 7)
            self.assertEqual(tenant.sources.marketplaces[0].name, "Marketplace One")
            self.assertEqual(tenant.sources.marketplaces[0].owner, "Ops")
            self.assertEqual(tenant.sources.marketplaces[0].frequency, "Hourly")
            self.assertEqual(tenant.sources.marketplaces[0].evidence_type, "API feed")
            self.assertEqual(tenant.sources.marketplaces[0].api_endpoint, "")
            self.assertEqual(tenant.sources.marketplaces[0].credential_reference, "")
            self.assertGreaterEqual(len(tenant.client_setup.forecast_horizons), 1)
            self.assertGreaterEqual(len(tenant.client_setup.entities), 1)

    def test_marketplace_api_source_fields_round_trip(self):
        tenant = TenantConfig.from_dict(
            {
                "sources": {
                    "marketplaces": [
                        {
                            "name": "Marketplace API",
                            "type": "Marketplace API",
                            "status": "Configured",
                            "api_endpoint": "https://api.example.test",
                            "credential_reference": "vault/marketplace-api",
                        }
                    ]
                }
            }
        )

        source = tenant.to_dict()["sources"]["marketplaces"][0]

        self.assertEqual(source["api_endpoint"], "https://api.example.test")
        self.assertEqual(source["credential_reference"], "vault/marketplace-api")

    def test_app_config_uses_tenant_reconciliation_settings(self):
        tenant = TenantConfig.default()
        config = AppConfig(tenant=tenant)

        self.assertEqual(
            tenant.client_setup.forecast_horizons,
            ["Daily", "7 days", "14 days", "30 days", "60 days"],
        )
        self.assertEqual(config.supported_currencies, set(tenant.currencies))
        self.assertEqual(
            config.date_tolerance_days,
            tenant.reconciliation.date_tolerance_days,
        )
        self.assertEqual(
            config.amount_tolerance,
            tenant.reconciliation.amount_tolerance,
        )

    def test_client_setup_round_trips_from_dict(self):
        tenant = TenantConfig.from_dict(
            {
                "client_setup": {
                    "entities": [
                        {
                            "name": "Entity A",
                            "currency": "USD",
                            "status": "Active",
                        }
                    ],
                    "planned_outflows": [
                        {
                            "name": "Payroll",
                            "category": "Payroll",
                            "amount": "10000",
                            "frequency": "Monthly",
                        }
                    ],
                    "forecast_horizons": ["7 days", "30 days"],
                    "alert_rules": [
                        {
                            "name": "Funding gap",
                            "trigger": "Forecast below threshold",
                            "recipient": "CFO",
                        }
                    ],
                    "user_roles": [
                        {
                            "name": "Treasury Lead",
                            "role": "Approver",
                            "scope": "All entities",
                        }
                    ],
                    "governance": {
                        "data_retention": "7 years",
                        "hosting_region": "US",
                        "ai_enabled": True,
                        "human_approval_required": True,
                    },
                }
            }
        )

        payload = tenant.to_dict()["client_setup"]

        self.assertEqual(payload["entities"][0]["name"], "Entity A")
        self.assertEqual(payload["planned_outflows"][0]["amount"], "10000")
        self.assertEqual(payload["forecast_horizons"], ["7 days", "30 days"])
        self.assertEqual(payload["alert_rules"][0]["recipient"], "CFO")
        self.assertEqual(payload["user_roles"][0]["role"], "Approver")
        self.assertEqual(payload["governance"]["data_retention"], "7 years")

    def test_local_tenant_config_has_one_daily_forecast_setup(self):
        config_path = Path(__file__).resolve().parents[1] / "config" / "tenant.local.json"
        text = config_path.read_text(encoding="utf-8")
        tenant = TenantConfig.from_file(config_path)

        self.assertEqual(text.count('"client_setup"'), 1)
        self.assertEqual(
            tenant.client_setup.forecast_horizons,
            ["Daily", "7 days", "14 days", "30 days", "60 days"],
        )


if __name__ == "__main__":
    unittest.main()
