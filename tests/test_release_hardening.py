import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from ai_cashflow.api.app import create_app
from ai_cashflow.api.preflight import validate_production
from ai_cashflow.api.routes import get_phase0_service
from security_helpers import (
    create_test_app,
    machine_headers,
    proxy_headers,
    security_settings,
    test_client,
)


ROOT = Path(__file__).resolve().parents[1]


class ReleaseHardeningTests(unittest.TestCase):
    def test_dashboard_frontend_is_resilient_to_currency_and_api_failures(self):
        javascript = (ROOT / "src/ai_cashflow/web/app.js").read_text(encoding="utf-8")

        checks = {
            "all currencies": 'currency ? `&currency=${encodeURIComponent(currency)}` : ""',
            "CAD": "currency === state.filters.currency",
            "BRL": "available_currencies",
            "MXN": "available_currencies",
            "USD": "available_currencies",
            "missing optional fields": "Array.isArray(position?.financial_values)",
            "null reserve funds payout": "candidate.amount !== null",
            "empty settlement groups": "Array.isArray(expected) ? expected : []",
            "API 401": "showAccessMessage(401)",
            "API 403": "showAccessMessage(403)",
            "API 500": "position?.message ||",
            "isolated panel API failure": 'fetchJson("/phase0/marketplace-ar").catch',
            "sync success": "Source sync completed with current Amazon data.",
            "sync failure": "Source synchronization",
            "sync reset": "button.textContent = \"Sync selected source\";",
            "isolated renderer failure": 'renderSafely("Financial cards", renderTreasuryOverview)',
            "stale response protection": "const isLatestRequest = _financialPositionRequests.get(sourceId) === requestId",
        }
        for scenario, expected in checks.items():
            with self.subTest(scenario=scenario):
                self.assertIn(expected, javascript)

        self.assertLess(
            javascript.index('button.textContent = "Sync selected source";'),
            javascript.index("async function loadAmazonFinancialPosition"),
        )
        self.assertIn("finally {", javascript[javascript.index("async function syncSelectedSource"):javascript.index("async function loadAmazonFinancialPosition")])
        self.assertIn("Build __RELEASE_ID__", (ROOT / "src/ai_cashflow/web/app.html").read_text(encoding="utf-8"))

    def test_release_id_versions_assets_and_html_is_not_cached(self):
        app = create_test_app()
        client = test_client(app, authenticated=False)
        with patch.dict(os.environ, {"AI_CASHFLOW_RELEASE_ID": "abc123def456"}):
            response = client.get("/app", headers=proxy_headers())

        self.assertEqual(response.status_code, 200)
        self.assertIn('/assets/app.js?v=abc123def456', response.text)
        self.assertIn('/assets/app.css?v=abc123def456', response.text)
        self.assertIn("Build abc123def456", response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_changing_release_id_changes_javascript_url(self):
        client = test_client(create_test_app(), authenticated=False)
        with patch.dict(os.environ, {"AI_CASHFLOW_RELEASE_ID": "release-one"}):
            first = client.get("/", headers=proxy_headers()).text
        with patch.dict(os.environ, {"AI_CASHFLOW_RELEASE_ID": "release-two"}):
            second = client.get("/", headers=proxy_headers()).text

        self.assertIn("/assets/app.js?v=release-one", first)
        self.assertIn("/assets/app.js?v=release-two", second)
        self.assertNotEqual(first, second)

    def test_versioned_assets_are_immutable_and_no_service_worker_exists(self):
        client = test_client(create_test_app(), authenticated=False)
        response = client.get("/assets/app.js?v=release-one", headers=proxy_headers())

        self.assertEqual(response.status_code, 200)
        self.assertIn("immutable", response.headers["cache-control"])
        javascript = (ROOT / "src/ai_cashflow/web/app.js").read_text(encoding="utf-8")
        self.assertNotIn("serviceWorker", javascript)

    def test_canonical_www_origin_is_accepted_and_apex_is_rejected(self):
        settings = security_settings()
        settings = type(settings)(**{
            **settings.__dict__,
            "public_origin": "https://www.aicashflow.pro",
        })
        app = create_app(settings)
        app.dependency_overrides[get_phase0_service] = lambda: SimpleNamespace(
            run_report=lambda: SimpleNamespace(status="generated", reports_dir=ROOT / "reports")
        )

        @app.middleware("http")
        async def use_loopback_test_peer(request, call_next):
            request.scope["client"] = ("127.0.0.1", 50000)
            return await call_next(request)

        client = test_client(app, authenticated=False)
        accepted = {
            **proxy_headers("admin"),
            "Origin": "https://www.aicashflow.pro",
            "X-AI-Cashflow-Request": "browser",
        }
        rejected = {**accepted, "Origin": "https://aicashflow.pro"}

        self.assertNotEqual(client.post("/phase0/run-report", headers=accepted).status_code, 403)
        self.assertEqual(client.post("/phase0/run-report", headers=rejected).status_code, 403)

    def test_json_request_above_byte_limit_returns_413(self):
        settings = security_settings()
        settings = type(settings)(**{**settings.__dict__, "max_upload_bytes": 128})
        client = test_client(create_app(settings), authenticated=False)
        body = json.dumps({"report_type": "bank_receipts", "rows": [{"value": "x" * 256}]})

        response = client.post(
            "/phase0/api-ingest",
            content=body,
            headers={**machine_headers(), "Content-Type": "application/json"},
        )

        self.assertEqual(response.status_code, 413)

    def test_preflight_accepts_complete_configuration_and_rejects_apex_origin(self):
        with self.subTest("valid"):
            from tempfile import TemporaryDirectory

            with TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                for name in ("samples", "reports", "config"):
                    (root / name).mkdir()
                environment = {
                    "AI_CASHFLOW_ENV": "production",
                    "AI_CASHFLOW_DEV_AUTH_BYPASS": "false",
                    "AI_CASHFLOW_TRUSTED_PROXIES": "127.0.0.1/32,::1/128",
                    "AI_CASHFLOW_PROXY_ASSERTION_SECRET": "p" * 32,
                    "AI_CASHFLOW_ADMIN_USERS": "admin",
                    "AI_CASHFLOW_API_KEY": "k" * 32,
                    "AI_CASHFLOW_MASTER_KEY": Fernet.generate_key().decode("ascii"),
                    "AI_CASHFLOW_DATABASE_PATH": str(root / "operations.sqlite3"),
                    "AI_CASHFLOW_PUBLIC_ORIGIN": "https://www.aicashflow.pro",
                    "AI_CASHFLOW_MAX_UPLOAD_BYTES": "10485760",
                    "AI_CASHFLOW_SAMPLES_DIR": str(root / "samples"),
                    "AI_CASHFLOW_REPORTS_DIR": str(root / "reports"),
                    "AI_CASHFLOW_TENANT_CONFIG_PATH": str(root / "config" / "tenant.local.json"),
                    "AI_CASHFLOW_SMOKE_BASIC_AUTH": "admin:password",
                    "AI_CASHFLOW_SMOKE_SOURCE_ID": "source-1",
                }
                with patch.dict(os.environ, environment, clear=True):
                    validate_production()
                environment["AI_CASHFLOW_TRANSACTION_VISIBILITY_ENABLED"] = "true"
                environment["AI_CASHFLOW_TRANSACTION_COLLECTION_ENABLED"] = "false"
                with patch.dict(os.environ, environment, clear=True):
                    with self.assertRaisesRegex(ValueError, "visibility requires collection"):
                        validate_production()
                environment["AI_CASHFLOW_TRANSACTION_COLLECTION_ENABLED"] = "true"
                with patch.dict(os.environ, environment, clear=True):
                    validate_production()
                environment["AI_CASHFLOW_TRANSACTION_VISIBILITY_ENABLED"] = "false"
                environment["AI_CASHFLOW_PUBLIC_ORIGIN"] = "https://aicashflow.pro"
                with patch.dict(os.environ, environment, clear=True):
                    with self.assertRaisesRegex(ValueError, "canonical"):
                        validate_production()
                environment["AI_CASHFLOW_PUBLIC_ORIGIN"] = "https://www.aicashflow.pro"
                environment["AI_CASHFLOW_MAX_UPLOAD_BYTES"] = str(12 * 1024 * 1024)
                with patch.dict(os.environ, environment, clear=True):
                    with self.assertRaisesRegex(ValueError, "Nginx-aligned"):
                        validate_production()

    def test_ingestion_rejects_excess_rows_malformed_later_row_and_type(self):
        client = test_client(create_test_app(), authenticated=False)

        too_many = client.post(
            "/phase0/api-ingest",
            json={"report_type": "transactions", "rows": [{} for _ in range(10_001)]},
            headers=machine_headers(),
        )
        malformed = client.post(
            "/phase0/api-ingest",
            json={"report_type": "transactions", "rows": [{}, "bad"]},
            headers=machine_headers(),
        )
        unsupported = client.post(
            "/phase0/api-ingest",
            json={"report_type": "unknown", "rows": [{}]},
            headers=machine_headers(),
        )

        self.assertEqual(too_many.status_code, 422)
        self.assertEqual(malformed.status_code, 422)
        self.assertEqual(unsupported.status_code, 422)

    def test_normal_bank_json_ingestion_still_works(self):
        class Service:
            def ingest_api_data(self, source_name, rows, report_type):
                return {
                    "status": "ingested",
                    "source": source_name,
                    "rows_ingested": len(rows),
                    "report_type": report_type,
                }

        app = create_test_app()
        app.dependency_overrides[get_phase0_service] = Service
        response = test_client(app, authenticated=False).post(
            "/phase0/api-ingest",
            json={
                "source_name": "bank-feed",
                "report_type": "bank_receipts",
                "rows": [{"date": "2026-08-04", "amount": "10.00"}],
            },
            headers=machine_headers(),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["rows_ingested"], 1)

    def test_release_scripts_are_pinned_atomic_bounded_and_reversible(self):
        deploy = (ROOT / "deploy.sh").read_text(encoding="utf-8")
        rollback = (ROOT / "rollback.sh").read_text(encoding="utf-8")
        lock = (ROOT / "requirements.lock").read_text(encoding="utf-8")
        environment = (ROOT / ".env.example").read_text(encoding="utf-8")
        runbook = (ROOT / "docs" / "runbook.md").read_text(encoding="utf-8")
        verifier = (ROOT / "verify-production.sh").read_text(encoding="utf-8")

        self.assertIn("--commit", deploy)
        self.assertIn("status --porcelain", deploy)
        self.assertIn("requirements.lock", deploy)
        self.assertIn("--require-hashes", deploy)
        self.assertLess(deploy.index("validate-production"), deploy.index("ln -s"))
        self.assertIn("mv -T", deploy)
        self.assertIn("restore_previous_release", deploy)
        self.assertIn("verified current release must be bootstrapped", deploy)
        for flag in (
            "AI_CASHFLOW_TRANSACTION_COLLECTION_ENABLED",
            "AI_CASHFLOW_TRANSACTION_VISIBILITY_ENABLED",
        ):
            self.assertIn(f"{flag}=false", environment)
            self.assertIn(flag, runbook)
        self.assertIn("transaction flags", verifier)
        self.assertIn("Restart=on-failure", deploy)
        self.assertIn("StartLimitIntervalSec=", deploy)
        self.assertIn("StartLimitBurst=", deploy)
        self.assertNotIn("Restart=always", deploy)
        self.assertIn("previous", rollback.lower())
        self.assertIn("mv -T", rollback)
        self.assertIn("--hash=sha256:", lock)

    def test_nginx_has_canonical_redirect_and_explicit_body_limit(self):
        nginx = (ROOT / "nginx_vhost.conf").read_text(encoding="utf-8")

        self.assertIn("return 301 https://www.aicashflow.pro$request_uri;", nginx)
        self.assertIn("server_name www.aicashflow.pro;", nginx)
        self.assertIn("client_max_body_size 11m;", nginx)

    def test_verifier_output_contract_never_echoes_secrets(self):
        secret = "do-not-print-this-secret-value"
        verifier = (ROOT / "verify-production.sh").read_text(encoding="utf-8")

        self.assertNotIn("set -x", verifier)
        self.assertNotIn('echo "$AI_CASHFLOW_', verifier)
        self.assertNotIn(secret, verifier)
        self.assertIn('echo "[PASS] $label"', verifier)
        self.assertIn('echo "[FAIL] $label"', verifier)


if __name__ == "__main__":
    unittest.main()
