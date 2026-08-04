from pathlib import Path
import tempfile
import unittest

from ai_cashflow.api.routes import get_phase0_service
from ai_cashflow.api.services import Phase0Service
from ai_cashflow.config import AppConfig, TenantConfig
from security_helpers import create_test_app, test_client as TestClient


def create_app():
    return create_test_app()


class TenantUpdateTests(unittest.TestCase):
    def test_service_persists_tenant_settings_to_local_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "tenant.local.json"
            service = Phase0Service(
                AppConfig(
                    tenant=TenantConfig.default(),
                    tenant_config_path=config_path,
                )
            )

            updated = service.update_tenant_settings(
                {
                    "product_name": "Client Treasury",
                    "tenant_name": "Client Group",
                    "poc_entity": "Client Entity",
                    "demo_mode": False,
                    "brand": {
                        "primary_color": "#2457a6",
                        "logo_text": "CT",
                    },
                    "currencies": ["USD", "EUR"],
                    "reconciliation": {
                        "date_tolerance_days": 4,
                        "amount_tolerance": "2.50",
                    },
                    "sources": {
                        "marketplaces": [
                            {
                                "name": "Marketplace Alpha",
                                "type": "API",
                                "status": "Configured",
                            }
                        ],
                        "banks": [
                            {
                                "name": "Bank Alpha",
                                "type": "Bank",
                                "status": "Configured",
                            }
                        ],
                    },
                }
            )

            self.assertEqual(updated["product_name"], "Client Treasury")
            self.assertTrue(config_path.exists())
            reloaded = TenantConfig.from_file(config_path)
            self.assertEqual(reloaded.tenant_name, "Client Group")
            self.assertEqual(reloaded.sources.marketplaces[0].name, "Marketplace Alpha")

    def test_put_tenant_settings_endpoint_persists_payload(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "tenant.local.json"
            service = Phase0Service(
                AppConfig(
                    tenant=TenantConfig.default(),
                    tenant_config_path=config_path,
                )
            )
            app = create_app()
            app.dependency_overrides[get_phase0_service] = lambda: service
            client = TestClient(app)

            response = client.put(
                "/tenant/settings",
                json={
                    "product_name": "Portal Treasury",
                    "tenant_name": "Portal Group",
                    "poc_entity": "Portal Entity",
                    "demo_mode": True,
                    "brand": {
                        "primary_color": "#123456",
                        "logo_text": "PT",
                    },
                    "currencies": ["USD"],
                    "reconciliation": {
                        "date_tolerance_days": 2,
                        "amount_tolerance": "0.75",
                    },
                    "sources": {
                        "marketplaces": [],
                        "banks": [],
                    },
                },
            )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["product_name"], "Portal Treasury")
            self.assertEqual(
                client.get("/tenant/settings").json()["brand"]["logo_text"],
                "PT",
            )
            self.assertTrue(config_path.exists())


if __name__ == "__main__":
    unittest.main()
