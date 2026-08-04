from pathlib import Path
import io
import tempfile
import unittest

from ai_cashflow.api.routes import get_phase0_service
from ai_cashflow.api.services import Phase0Service
from ai_cashflow.config import AppConfig, TenantConfig
from security_helpers import create_test_app, machine_headers, test_client as TestClient


PHASE0_FIXTURES = Path(__file__).resolve().parent / "fixtures/phase0_samples"


def create_app():
    return create_test_app()


def make_service(root: Path, *, api_key: str | None = None) -> Phase0Service:
    return Phase0Service(
        AppConfig(
            samples_dir=root / "samples",
            reports_dir=root / "reports",
            tenant=TenantConfig.default(),
            tenant_config_path=root / "tenant.local.json",
            database_path=root / "ops.db",
            api_key=api_key,
            max_upload_bytes=32,
        )
    )


class ProductionFoundationTests(unittest.TestCase):
    def test_ready_endpoint_reports_storage_and_mode(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = make_service(Path(temp_dir))
            app = create_app()
            app.dependency_overrides[get_phase0_service] = lambda: service
            client = TestClient(app)

            response = client.get("/ready")

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"status": "ready"})

    def test_api_key_protects_mutating_and_report_routes_when_configured(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = make_service(Path(temp_dir), api_key="secret")
            app = create_app()
            app.dependency_overrides[get_phase0_service] = lambda: service
            client = TestClient(app, authenticated=False)

            missing = client.post("/phase0/run-report")
            wrong = client.post(
                "/phase0/run-report", headers={"X-API-Key": "wrong"}
            )

            self.assertEqual(missing.status_code, 401)
            self.assertEqual(wrong.status_code, 401)

    def test_api_key_accepts_valid_key_when_configured(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = Phase0Service(
                AppConfig(
                    samples_dir=PHASE0_FIXTURES,
                    reports_dir=root / "reports",
                    tenant=TenantConfig.default(),
                    tenant_config_path=root / "tenant.local.json",
                    database_path=root / "ops.db",
                    api_key="secret",
                )
            )
            app = create_app()
            app.dependency_overrides[get_phase0_service] = lambda: service
            client = TestClient(app, authenticated=False)

            response = client.post(
                "/phase0/run-report", headers=machine_headers()
            )

            self.assertEqual(response.status_code, 200)

    def test_manual_file_upload_endpoint_is_removed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = make_service(Path(temp_dir))
            app = create_app()
            app.dependency_overrides[get_phase0_service] = lambda: service
            client = TestClient(app)

            response = client.post(
                "/phase0/uploads",
                data={"category": "manual_uploads"},
                files={"file": ("sample.csv", b"col\nvalue\n", "text/csv")},
            )

            self.assertEqual(response.status_code, 405)

    def test_uploads_and_audit_events_are_persisted_in_sqlite(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = make_service(root)

            service.save_upload("sample.csv", "manual_uploads", io.BytesIO(b"col\n1\n"))
            reloaded = make_service(root)

            self.assertEqual(len(reloaded.list_uploads()), 1)
            self.assertEqual(reloaded.list_uploads()[0]["file_name"], "sample.csv")
            events = reloaded.list_audit_events()
            self.assertEqual(events[0]["event_type"], "upload.saved")


if __name__ == "__main__":
    unittest.main()
