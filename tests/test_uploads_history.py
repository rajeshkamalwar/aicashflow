from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from ai_cashflow.api.app import create_app
from ai_cashflow.api.routes import get_phase0_service
from ai_cashflow.api.services import Phase0Service
from ai_cashflow.config import AppConfig, TenantConfig


PHASE0_FIXTURES = Path(__file__).resolve().parent / "fixtures/phase0_samples"


class UploadsHistoryTests(unittest.TestCase):
    def test_upload_history_is_read_only(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = Phase0Service(
                AppConfig(
                    samples_dir=root / "samples",
                    reports_dir=root / "reports",
                    tenant=TenantConfig.default(),
                    tenant_config_path=root / "tenant.local.json",
                )
            )
            app = create_app()
            app.dependency_overrides[get_phase0_service] = lambda: service
            client = TestClient(app)

            response = client.post(
                "/phase0/uploads",
                data={"category": "manual_uploads"},
                files={"file": ("sample.csv", b"col\nvalue\n", "text/csv")},
            )

            self.assertEqual(response.status_code, 405)
            self.assertFalse((root / "samples/manual_uploads/sample.csv").exists())

            uploads = client.get("/phase0/uploads")
            self.assertEqual(uploads.status_code, 200)
            self.assertEqual(uploads.json(), [])

    def test_run_report_records_history(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = Phase0Service(
                AppConfig(
                    samples_dir=PHASE0_FIXTURES,
                    reports_dir=root / "reports",
                    tenant=TenantConfig.default(),
                    tenant_config_path=root / "tenant.local.json",
                )
            )
            app = create_app()
            app.dependency_overrides[get_phase0_service] = lambda: service
            client = TestClient(app)

            response = client.post("/phase0/run-report")

            self.assertEqual(response.status_code, 200)
            history = client.get("/phase0/runs")
            self.assertEqual(history.status_code, 200)
            self.assertEqual(len(history.json()), 1)
            self.assertEqual(history.json()[0]["status"], "generated")
            self.assertEqual(history.json()[0]["matched_payouts"], 4)


if __name__ == "__main__":
    unittest.main()
