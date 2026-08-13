import unittest
from datetime import date, datetime, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
import csv

from cryptography.fernet import Fernet

from ai_cashflow.api.routes import (
    get_amazon_integration_manager,
    get_amazon_source_registry,
    get_phase0_service,
)
from ai_cashflow.api.services import Phase0Service
from ai_cashflow.config import AppConfig
from ai_cashflow.integrations import AmazonIntegrationManager, AmazonSourceRegistry
from security_helpers import create_test_app, machine_headers, test_client as TestClient


PHASE0_FIXTURES = Path(__file__).resolve().parent / "fixtures/phase0_samples"


def create_app():
    return create_test_app()


class ApiAppTests(unittest.TestCase):
    def test_marketplace_ar_and_api_status_remain_available(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app = create_app()
            app.dependency_overrides[get_phase0_service] = lambda: Phase0Service(AppConfig(
                samples_dir=PHASE0_FIXTURES,
                reports_dir=root / "reports",
                database_path=root / "ops.db",
            ))
            client = TestClient(app)

            marketplace_ar = client.get("/phase0/marketplace-ar")
            api_status = client.get("/phase0/api-status")

        self.assertEqual(marketplace_ar.status_code, 200)
        self.assertEqual(api_status.status_code, 200)

    def test_marketplace_activity_skips_partial_csv_row_and_returns_200(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            reports = root / "reports"
            reports.mkdir()
            (reports / "marketplace_activity.csv").write_text(
                "posted_at,source_id,settlement_id,type,marketplace,currency,total,status,release_date,order_id,source_file\n"
                "2026-08-04T19:34:01Z,,,,,,,,,,\n",
                encoding="utf-8",
            )
            app = create_app()
            app.dependency_overrides[get_phase0_service] = lambda: Phase0Service(AppConfig(
                samples_dir=root / "samples",
                reports_dir=reports,
                database_path=root / "ops.db",
            ))

            response = TestClient(app).get("/phase0/marketplace-activity")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "partial")
        self.assertEqual(response.json()["rows"], [])
        self.assertEqual(response.json()["diagnostics"]["skipped_row_count"], 1)

    def test_marketplace_activity_failure_is_panel_scoped_in_frontend(self):
        app_js = (Path(__file__).resolve().parents[1] / "src" / "ai_cashflow" / "web" / "app.js").read_text(encoding="utf-8")

        self.assertIn('fetchJson("/phase0/marketplace-activity").catch', app_js)
        self.assertIn("Only successful, positive Closed transfers.", app_js)
        self.assertIn('state.marketplaceActivityStatus', app_js)
        self.assertIn('Marketplace activity is temporarily unavailable.', app_js)

    def test_phase2a_transaction_visibility_wording_is_coverage_qualified(self):
        web_root = Path(__file__).resolve().parents[1] / "src" / "ai_cashflow" / "web"
        content = (web_root / "app.html").read_text(encoding="utf-8") + (web_root / "app.js").read_text(encoding="utf-8")
        self.assertIn("API-Derived Deferred Transactions", content)
        self.assertIn("Released During Observed Period", content)
        self.assertIn("Previously Deferred, Released During Observed Period", content)
        self.assertIn("Most Recent Completed Payout", content)
        self.assertIn("Only successful, positive Closed transfers.", content)
        self.assertNotIn("Observed within the last 179 days", content)
        self.assertIn("Historical coverage is incomplete.", content)
        self.assertIn("Partial coverage", content)
        self.assertIn("Transaction Composition — Partial Coverage", content)
        self.assertIn("successfully collected Amazon API coverage", content)
        self.assertIn("Collection Progress", content)
        self.assertIn("Incremental collection", content)
        self.assertIn("Validation canaries", content)
        self.assertIn("Historical backfill", content)
        self.assertIn("Historical backfill has not started.", content)
        self.assertNotIn("Backfill completed", content)
        self.assertIn("AMAZON_TRANSACTION_DERIVED", content)
        self.assertNotIn("open plus deferred", content.lower())
        self.assertIn('const enabled = Boolean(position?.transaction_visibility_enabled);', content)
        self.assertIn('if (!enabled) return;', content)

    def test_account_balance_summary_is_snapshot_and_currency_scoped(self):
        web_root = Path(__file__).resolve().parents[1] / "src" / "ai_cashflow" / "web"
        app_js = (web_root / "app.js").read_text(encoding="utf-8")
        app_html = (web_root / "app.html").read_text(encoding="utf-8")

        self.assertIn("Account Balance", app_html)
        self.assertIn("Standard orders", app_html)
        self.assertIn("Deferred transactions", app_html)
        self.assertIn("All Accounts", app_html)
        self.assertIn("Funds Available", app_html)
        self.assertIn("Amazon has not supplied an authoritative funds-available value through the current API source.", app_html)
        self.assertIn("renderAccountBalanceSummary(position)", app_js)
        self.assertIn("position.snapshot_id", app_js)
        self.assertIn("state.filters.currency", app_js)
        self.assertIn("position.currency_scope === selectedCurrency", app_js)
        self.assertIn('financialValues(position, "FUNDS_AVAILABLE")', app_js)
        self.assertNotIn('renderAvailability(position, "RESERVE_ADJUSTED_FUNDS", "account-balance', app_js)

    def test_settlement_status_uses_the_financial_position_group_count(self):
        app_js = (Path(__file__).resolve().parents[1] / "src" / "ai_cashflow" / "web" / "app.js").read_text(encoding="utf-8")

        self.assertIn("position?.openSettlementGroupCount", app_js)
        self.assertNotIn('["Open settlement groups", expected.length', app_js)
        self.assertIn("position.group_count", app_js)

    def test_overview_loads_current_cash_positions_for_all_connected_sources(self):
        app_js = (Path(__file__).resolve().parents[1] / "src" / "ai_cashflow" / "web" / "app.js").read_text(encoding="utf-8")

        self.assertIn("await loadAmazonFinancialPositions();", app_js)
        self.assertIn("state.amazonFinancialPositions[source.id]?.status", app_js)
        self.assertIn('"Amazon-reported open balance"', app_js)
        self.assertIn("position.totals_by_currency", app_js)
        self.assertNotIn('"Historical payouts"', app_js)

    def test_overview_displays_amazon_validated_multi_currency_balances(self):
        app_js = (Path(__file__).resolve().parents[1] / "src" / "ai_cashflow" / "web" / "app.js").read_text(encoding="utf-8")
        app_html = (Path(__file__).resolve().parents[1] / "src" / "ai_cashflow" / "web" / "app.html").read_text(encoding="utf-8")

        self.assertIn('position?.mode === "amazon_open_balances"', app_js)
        self.assertIn("position.totals_by_currency", app_js)
        self.assertIn("Amazon-Reported Open Balances", app_html)
        self.assertIn("Current reserve", app_html)
        self.assertIn("Funds available after reserve", app_html)
        self.assertIn("Upcoming payout", app_html)
        self.assertIn("Technical processing status is separate from Seller Central reconciliation", app_html)
        self.assertIn('not_verified: "Not Verified"', app_js)
        self.assertIn("Estimated USD Equivalent", app_js)
        self.assertNotIn("Expected marketplace payouts", app_html + app_js)
        self.assertNotIn("Unscheduled", app_html + app_js)
        self.assertNotIn("All-currency payout view ready", app_js)
        self.assertIn('renderAvailability(position, "CURRENT_RESERVE"', app_js)
        self.assertIn('renderAvailability(position, "RESERVE_ADJUSTED_FUNDS"', app_js)
        self.assertIn('renderAvailability(position, "UPCOMING_PAYOUT"', app_js)
        self.assertIn("&currency=", app_js)

    def test_overview_labels_open_financial_event_groups_as_settlement_groups(self):
        web_root = Path(__file__).resolve().parents[1] / "src" / "ai_cashflow" / "web"
        app_js = (web_root / "app.js").read_text(encoding="utf-8")
        app_html = (web_root / "app.html").read_text(encoding="utf-8")

        self.assertIn('"Open settlement groups"', app_js)
        self.assertIn("Amazon financial event groups currently being processed.", app_js)
        self.assertIn("Settlement status", app_html)
        self.assertIn("View open groups", app_html)
        self.assertNotIn("Pending marketplace payouts", app_js + app_html)
        self.assertNotIn("Open payouts", app_js + app_html)

    def test_cashflow_proof_endpoints_are_protected_and_paginated(self):
        class FakeService:
            config = SimpleNamespace(api_key="", max_upload_bytes=1024 * 1024)

            def import_bank_statement(self, **payload):
                return {
                    "file_name": payload["file_name"],
                    "row_count": 1,
                    "file_sha256": "safe-hash",
                    "proofs_matched": 1,
                }

            def run_cashflow_proof(self):
                return {"status": "completed", "window_days": 90}

            def get_cashflow_proof_summary(self):
                return {"source_coverage": "1/1", "verified_count": 1}

            def list_cashflow_proof_items(self, *, limit, offset):
                return {
                    "items": [{"status": "Verified"}],
                    "pagination": {"limit": limit, "offset": offset, "total": 1},
                }

        app = create_app()
        app.dependency_overrides[get_phase0_service] = lambda: FakeService()
        client = TestClient(app)
        unauthenticated = TestClient(app, authenticated=False)
        statement = (
            "receipt_id,bank_account,currency,receipt_date,amount,reference\n"
            "R1,OPERATING,USD,2026-07-02,37.85,GROUP-1\n"
        )
        denied = unauthenticated.post(
            "/admin/reconciliation/bank-statements",
            files={"file": ("statement.csv", statement, "text/csv")},
            data={"bank_source": "Main bank"},
        )
        imported = client.post(
            "/admin/reconciliation/bank-statements",
            files={"file": ("statement.csv", statement, "text/csv")},
            data={"bank_source": "Main bank"},
        )
        run = client.post("/admin/reconciliation/proofs/run")
        summary = client.get("/phase0/reconciliation/proof")
        items = client.get(
            "/phase0/reconciliation/proof/items",
            params={"limit": 25, "offset": 5},
        )

        self.assertEqual(denied.status_code, 401)
        self.assertEqual(imported.status_code, 200)
        self.assertEqual(run.json()["window_days"], 90)
        self.assertEqual(summary.json()["source_coverage"], "1/1")
        self.assertEqual(items.json()["pagination"]["limit"], 25)
        self.assertEqual(items.json()["pagination"]["offset"], 5)

    def test_amazon_financial_position_endpoint_returns_current_usd_statement(self):
        class FakeService:
            config = SimpleNamespace(api_key="")

            def get_amazon_financial_position(self, source_id, currency=None):
                self.source_id = source_id
                self.currency = currency
                return {
                    "status": "ready",
                    "currency": "USD",
                    "source_id": source_id,
                    "amounts": {"funds_available": "13096.75"},
                }

        app = create_app()
        service = FakeService()
        app.dependency_overrides[get_phase0_service] = lambda: service

        response = TestClient(app).get(
            "/phase0/amazon/financial-position",
            params={"source_id": "books-au", "currency": "AUD"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["currency"], "USD")
        self.assertEqual(response.json()["amounts"]["funds_available"], "13096.75")
        self.assertEqual(service.source_id, "books-au")
        self.assertEqual(service.currency, "AUD")

    def test_canonical_payout_state_endpoint_reads_the_persisted_service_model(self):
        class FakeService:
            config = SimpleNamespace(api_key="")

            def get_canonical_payout_state(self, source_id, currency=None):
                self.request = (source_id, currency)
                return {
                    "availability": "DATA_AVAILABLE",
                    "financial_event_group_snapshot_id": "persisted-snapshot",
                    "completed_payout_count": 1,
                    "completed_payout_total": "10.00",
                }

        app = create_app()
        service = FakeService()
        app.dependency_overrides[get_phase0_service] = lambda: service

        response = TestClient(app).get(
            "/phase0/amazon/canonical-payout-state",
            params={"source_id": "books-ca", "currency": "CAD"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(service.request, ("books-ca", "CAD"))
        self.assertEqual(response.json()["financial_event_group_snapshot_id"], "persisted-snapshot")

    def test_canonical_payout_state_endpoint_supports_the_report_all_source_scope(self):
        class FakeService:
            config = SimpleNamespace(api_key="")
            def get_canonical_payout_state(self, source_id=None, currency=None):
                self.request = (source_id, currency)
                return {"availability": "DATA_AVAILABLE"}
        app = create_app(); service = FakeService()
        app.dependency_overrides[get_phase0_service] = lambda: service
        response = TestClient(app).get("/phase0/amazon/canonical-payout-state")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(service.request, (None, None))

    def test_amazon_source_api_manages_multiple_masked_profiles(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_app()
            registry = AmazonSourceRegistry(
                Path(temp_dir) / "ops.db", Fernet.generate_key().decode()
            )
            app.dependency_overrides[get_amazon_source_registry] = lambda: registry
            client = TestClient(app)

            created = client.post("/admin/integrations/amazon/sources", json={
                "name": "Books Amazon.Ca/Mx",
                "application_id": "app-id",
                "endpoint": "https://sellingpartnerapi-na.amazon.com",
                "marketplace": "Canada / Mexico",
                "client_id": "client-value",
                "client_secret": "secret-value",
                "refresh_token": "token-value",
            })
            listed = client.get("/admin/integrations/amazon/sources")

        self.assertEqual(created.status_code, 200)
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(len(listed.json()), 1)
        self.assertTrue(listed.json()[0]["credentials"]["refresh_token_configured"])
        self.assertNotIn("client-value", listed.text)
        self.assertNotIn("secret-value", listed.text)
        self.assertNotIn("token-value", listed.text)

    def test_amazon_source_connection_test_uses_payment_transactions(self):
        class FakeClient:
            def __init__(self, _config):
                pass

            def probe_payments_access(self):
                return [{
                    "marketplaceDetails": {
                        "marketplaceId": "A2EUQ1WTGCTBG2",
                        "marketplaceName": "Amazon.ca",
                    },
                    "totalAmount": {"currencyCode": "CAD", "currencyAmount": 10},
                }]

        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_app()
            registry = AmazonSourceRegistry(
                Path(temp_dir) / "ops.db", Fernet.generate_key().decode()
            )
            source = registry.upsert({
                "name": "Canada",
                "client_id": "client",
                "client_secret": "secret",
                "refresh_token": "token",
            })
            app.dependency_overrides[get_amazon_source_registry] = lambda: registry
            client = TestClient(app)
            with patch("ai_cashflow.api.routes.AmazonSpApiClient", FakeClient):
                response = client.post(
                    f"/admin/integrations/amazon/sources/{source['id']}/test"
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["source"]["marketplaces"][0]["currency"], "CAD")

    def test_amazon_admin_settings_never_return_credentials(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_app()
            manager = AmazonIntegrationManager(
                Path(temp_dir) / "ops.db", Fernet.generate_key().decode()
            )
            app.dependency_overrides[get_amazon_integration_manager] = lambda: manager
            client = TestClient(app)

            saved = client.put("/admin/integrations/amazon", json={
                "application_id": "amzn1.sp.solution.test",
                "endpoint": "https://sellingpartnerapi-na.amazon.com",
                "marketplace": "Canada",
                "sync_frequency_minutes": 60,
                "enabled": True,
                "client_id": "secret-client-id",
                "client_secret": "secret-client-secret",
                "refresh_token": "secret-refresh-token",
            })
            loaded = client.get("/admin/integrations/amazon")

        self.assertEqual(saved.status_code, 200)
        self.assertEqual(loaded.status_code, 200)
        self.assertTrue(loaded.json()["credentials"]["refresh_token_configured"])
        self.assertNotIn("secret-client", loaded.text)
        self.assertNotIn("secret-refresh", loaded.text)

    def test_transaction_backfill_and_diagnostics_are_admin_only(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_app()
            registry = AmazonSourceRegistry(
                Path(temp_dir) / "ops.db", Fernet.generate_key().decode()
            )
            source = registry.upsert({
                "name": "Canada", "client_id": "a", "client_secret": "b", "refresh_token": "c",
            })
            app.dependency_overrides[get_amazon_source_registry] = lambda: registry
            denied = TestClient(app, authenticated=False).get(
                f"/admin/integrations/amazon/sources/{source['id']}/transaction-collection"
            )
            client = TestClient(app)
            created = client.post(
                f"/admin/integrations/amazon/sources/{source['id']}/transaction-backfills",
                json={
                    "marketplace_id": "CA",
                    "marketplace_name": "Amazon.ca",
                    "currency": "CAD",
                    "transaction_status": "DEFERRED",
                    "overall_start": datetime(2026, 8, 1, tzinfo=timezone.utc).isoformat(),
                    "overall_end": datetime(2026, 8, 2, tzinfo=timezone.utc).isoformat(),
                    "slice_hours": 24,
                },
            )
            diagnostics = client.get(
                f"/admin/integrations/amazon/sources/{source['id']}/transaction-collection"
            )

        self.assertEqual(denied.status_code, 401)
        self.assertEqual(created.status_code, 200)
        self.assertEqual(diagnostics.status_code, 200)
        self.assertEqual(diagnostics.json()["backfills"][0]["status"], "running")
        self.assertEqual(diagnostics.json()["backfills"][0]["completed_slice_count"], 0)
        self.assertEqual(diagnostics.json()["backfills"][0]["pending_slice_count"], 1)
        self.assertEqual(diagnostics.json()["backfills"][0]["failed_slice_count"], 0)
        self.assertIn("database_growth_bytes", diagnostics.json()["backfills"][0])

    def test_amazon_admin_settings_require_proxy_authenticated_admin_in_production(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_app()
            manager = AmazonIntegrationManager(
                Path(temp_dir) / "ops.db", Fernet.generate_key().decode()
            )
            app.dependency_overrides[get_amazon_integration_manager] = lambda: manager
            denied = TestClient(app, authenticated=False).get("/admin/integrations/amazon")
            allowed = TestClient(app).get("/admin/integrations/amazon")

        self.assertEqual(denied.status_code, 401)
        self.assertEqual(allowed.status_code, 200)

    def test_health_endpoint(self):
        client = TestClient(create_app())

        response = client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_root_endpoint_serves_the_dashboard(self):
        client = TestClient(create_app())

        response = client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertIn("Cashflow Command Center", response.text)

    def test_phase0_summary_endpoint(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_app()
            service = Phase0Service(
                AppConfig(
                    samples_dir=PHASE0_FIXTURES,
                    reports_dir=Path(temp_dir) / "reports",
                    database_path=Path(temp_dir) / "ops.db",
                )
            )
            app.dependency_overrides[get_phase0_service] = lambda: service
            client = TestClient(app)

            response = client.get("/phase0/summary")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["matched_payouts"], 4)

    def test_marketplace_ar_endpoint_returns_usd_snapshot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ar_dir = root / "samples" / "marketplace_ar"
            ar_dir.mkdir(parents=True)
            fx_path = root / "marketplace_ar_fx_rates.csv"
            with fx_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=["rate_date", "currency", "usd_rate", "rate_source"],
                )
                writer.writeheader()
                writer.writerow({
                    "rate_date": "2026-07-09", "currency": "CAD",
                    "usd_rate": "0.70", "rate_source": "ECB euro reference rates",
                })
            registry = root / "marketplace_ar_sources.csv"
            with registry.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
                        "source_id", "source_name", "active", "connector_type",
                        "connector_status", "verification_status",
                    ],
                )
                writer.writeheader()
                writer.writerow({
                    "source_id": "amazon-ca",
                    "source_name": "Amazon CA",
                    "active": "true",
                    "connector_type": "amazon_sp_api",
                    "connector_status": "ready",
                    "verification_status": "approved",
                })
            with (ar_dir / "snapshot.csv").open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
                        "snapshot_date", "rate_date", "rate_source", "source_id",
                        "account_id", "venue", "group", "local_amount",
                        "currency", "usd_rate",
                    ],
                )
                writer.writeheader()
                writer.writerow({
                    "snapshot_date": "2026-07-13",
                    "rate_date": "2026-07-09",
                    "rate_source": "ECB euro reference rates",
                    "source_id": "amazon-ca",
                    "account_id": "amazon-ca",
                    "venue": "Amazon CA",
                    "group": "Amazon",
                    "local_amount": "100",
                    "currency": "CAD",
                    "usd_rate": "0.70",
                })
            app = create_app()
            service = Phase0Service(
                AppConfig(
                    samples_dir=root / "samples",
                    reports_dir=root / "reports",
                    database_path=root / "ops.db",
                    marketplace_ar_registry_path=registry,
                    marketplace_ar_fx_path=fx_path,
                )
            )
            app.dependency_overrides[get_phase0_service] = lambda: service
            response = TestClient(app).get("/phase0/marketplace-ar")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["currency"], "USD")
        self.assertEqual(response.json()["total_ar_usd"], "70.00")
        self.assertTrue(response.json()["coverage_verified"])

    def test_marketplace_ar_api_ingest_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            fx_path = root / "marketplace_ar_fx_rates.csv"
            with fx_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=["rate_date", "currency", "usd_rate", "rate_source"],
                )
                writer.writeheader()
                writer.writerow({
                    "rate_date": date.today().isoformat(), "currency": "USD",
                    "usd_rate": "1", "rate_source": "Fixed USD",
                })
            registry = root / "marketplace_ar_sources.csv"
            with registry.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
                        "source_id", "source_name", "active", "connector_type",
                        "connector_status", "verification_status",
                    ],
                )
                writer.writeheader()
                writer.writerow({
                    "source_id": "alibris", "source_name": "Alibris",
                    "active": "true", "connector_type": "api",
                    "connector_status": "ready",
                    "verification_status": "approved",
                })
            service = Phase0Service(AppConfig(
                samples_dir=root / "samples",
                reports_dir=root / "reports",
                database_path=root / "ops.db",
                marketplace_ar_registry_path=registry,
                marketplace_ar_fx_path=fx_path,
                marketplace_ar_ingest_mode="api_only",
            ))
            app = create_app()
            app.dependency_overrides[get_phase0_service] = lambda: service
            client = TestClient(app)
            today = date.today().isoformat()
            payload = {
                "source_name": "Alibris",
                "report_type": "marketplace_ar",
                "rows": [{
                    "snapshot_date": today,
                    "rate_date": today,
                    "rate_source": "Injected rate",
                    "source_id": "alibris",
                    "account_id": "alibris",
                    "venue": "Alibris",
                    "group": "Own Webstores / Other",
                    "local_amount": "25",
                    "currency": "USD",
                    "usd_rate": "99",
                }],
            }

            first = client.post("/phase0/api-ingest", json=payload, headers=machine_headers())
            second = client.post("/phase0/api-ingest", json=payload, headers=machine_headers())
            unknown_payload = {
                **payload,
                "rows": [{
                    **payload["rows"][0],
                    "source_id": "unknown-source",
                    "account_id": "unknown-source",
                }],
            }
            rejected = client.post("/phase0/api-ingest", json=unknown_payload, headers=machine_headers())
            summary = client.get("/phase0/marketplace-ar")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["status"], "ingested")
        self.assertEqual(second.json()["status"], "already_ingested")
        self.assertEqual(rejected.status_code, 422)
        self.assertEqual(summary.json()["total_ar_usd"], "25.00")
        self.assertEqual(summary.json()["row_count"], 1)
        self.assertTrue(summary.json()["api_only_active"])
        self.assertTrue(summary.json()["api_cutover_ready"])
        self.assertTrue(summary.json()["display_ready"])

    def test_input_readiness_endpoint_identifies_configuration_gaps(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_app()
            service = Phase0Service(
                AppConfig(
                    samples_dir=PHASE0_FIXTURES,
                    reports_dir=Path(temp_dir) / "reports",
                    database_path=Path(temp_dir) / "ops.db",
                )
            )
            app.dependency_overrides[get_phase0_service] = lambda: service
            client = TestClient(app)

            response = client.get("/phase0/input-readiness")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "configuration_required")
        self.assertGreater(body["blocking_count"], 0)
        check_names = [check["name"] for check in body["checks"]]
        self.assertTrue(any(name.startswith("API credential — ") for name in check_names))
        self.assertIn("Cash confirmation source", check_names)
        self.assertIn("Calculation audit", check_names)
        self.assertIn("Automated alerts", check_names)
        self.assertIn("Forecast automation", check_names)

    def test_calculation_audit_endpoint_rechecks_report_totals(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_app()
            service = Phase0Service(
                AppConfig(
                    samples_dir=PHASE0_FIXTURES,
                    reports_dir=Path(temp_dir) / "reports",
                    database_path=Path(temp_dir) / "ops.db",
                )
            )
            app.dependency_overrides[get_phase0_service] = lambda: service
            client = TestClient(app)

            response = client.get("/phase0/calculation-audit")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "passed")
        self.assertEqual(body["failed_count"], 0)
        check_names = [check["name"] for check in body["checks"]]
        self.assertIn("Matched amount", check_names)
        self.assertIn("Marketplace net activity", check_names)

    def test_run_report_endpoint(self):
        client = TestClient(create_app())

        response = client.post("/phase0/run-report")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "generated")

    def test_dashboard_endpoint_returns_html(self):
        client = TestClient(create_app())

        response = client.get("/reports/cash-position")

        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertIn("Cash Position Report", response.text)

    def test_app_shell_endpoint_returns_html(self):
        client = TestClient(create_app())

        response = client.get("/app")

        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertIn("data-view=\"overview\"", response.text)
        for view_id in ("overview", "marketplace-ar", "payouts", "sources", "admin"):
            self.assertIn(f'data-view="{view_id}"', response.text)
        for retired_view_id in ("exceptions", "reports", "source-health", "reconciliation", "data-quality", "forecast", "reserves", "settings"):
            self.assertNotIn(f'data-view="{retired_view_id}"', response.text)
        self.assertIn("Amazon financial position", response.text)
        self.assertIn("Amazon-Reported Open Balances", response.text)
        self.assertNotIn("Confirmed bank cash", response.text)
        self.assertNotIn("Expected marketplace payouts", response.text)
        for position_id in (
            "overview-confirmed-cash",
            "overview-reserve-cash",
            "overview-funds-cash",
            "overview-upcoming-cash",
        ):
            self.assertIn(f'id="{position_id}"', response.text)
        self.assertIn("Marketplace AR", response.text)
        self.assertIn('id="marketplace-ar-total"', response.text)
        self.assertNotIn('id="marketplace-switcher"', response.text)
        self.assertNotIn('id="account-filter-note"', response.text)
        self.assertNotIn('id="treasury-horizon-select"', response.text)
        self.assertNotIn('id="source-basis-headline"', response.text)
        self.assertIn("Checking system", response.text)
        self.assertNotIn("Checking API", response.text)
        self.assertNotIn("API online", response.text)
        self.assertNotIn("FastMediaMX | Canada", response.text)
        self.assertIn("Amazon Accounts", response.text)
        self.assertNotIn("Upload Reports", response.text)
        self.assertNotIn("Upload Marketplace Reports", response.text)
        self.assertIn("Group / Client", response.text)
        self.assertIn("Primary Entity", response.text)
        self.assertIn("Amazon accounts are managed in the encrypted SP-API registry", response.text)
        self.assertNotIn("Marketplace Report Sources", response.text)
        self.assertNotIn('id="marketplace-sources-editor"', response.text)
        self.assertNotIn("Executive Snapshot", response.text)
        self.assertNotIn("What Finance Should Check", response.text)
        self.assertNotIn("Source Coverage", response.text)
        self.assertNotIn("Amazon Setup", response.text)
        self.assertNotIn("Upload Amazon Reports", response.text)
        self.assertNotIn(">Data Sources<", response.text)
        self.assertNotIn("Ergode Inc. POC", response.text)
        self.assertIn("id=\"tenant-settings-form\"", response.text)
        self.assertIn("id=\"save-settings\"", response.text)
        self.assertIn("Amazon SP-API Integration", response.text)
        self.assertIn('id="amazon-refresh-token"', response.text)
        self.assertIn('id="amazon-test-connection"', response.text)
        self.assertIn('id="amazon-sync-now"', response.text)
        self.assertIn('id="amazon-source-select"', response.text)
        self.assertIn('id="amazon-source-list"', response.text)
        self.assertIn('id="amazon-account-identity"', response.text)
        self.assertIn('id="amazon-source-name"', response.text)
        self.assertIn('id="amazon-add-source"', response.text)
        self.assertNotIn("data-overview-tab", response.text)
        self.assertNotIn('id="sources-table"', response.text)
        self.assertNotIn('id="add-marketplace-modal"', response.text)
        self.assertNotIn('id="add-marketplace-btn"', response.text)
        self.assertNotIn("+ Add Marketplace", response.text)
        self.assertNotIn("id=\"sample-upload-form\"", response.text)
        self.assertNotIn("id=\"upload-file\"", response.text)
        self.assertNotIn('value="banks"', response.text)
        self.assertIn("id=\"run-history-list\"", response.text)
        self.assertIn("id=\"report-links\"", response.text)
        self.assertNotIn("Integration Reference", response.text)
        self.assertNotIn("Phase 0", response.text)

        app_js = (Path(__file__).resolve().parents[1] / "src" / "ai_cashflow" / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn('if (!isBackground && document.getElementById("admin")', app_js)
        self.assertIn("function amazonSourceStatusDetails", app_js)
        self.assertNotIn("function renderSources", app_js)
        self.assertNotIn("function openAddMarketplaceModal", app_js)

    def test_tenant_settings_endpoint(self):
        client = TestClient(create_app())

        response = client.get("/tenant/settings")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("product_name", body)
        self.assertIn("tenant_name", body)
        self.assertIn("sources", body)
        self.assertIn("brand", body)
        self.assertEqual(body["tenant_name"], "Ergode Group")
        self.assertEqual(body["poc_entity"], "Ergode Inc.")
        self.assertEqual(body["brand"]["logo_text"], "CC")
        self.assertEqual(body["sources"]["banks"], [])
        marketplace_names = [
            source["name"] for source in body["sources"]["marketplaces"]
        ]
        self.assertTrue(any("FastMediaMX Canada" in name for name in marketplace_names))
        self.assertNotIn("FastMediaMX", body["tenant_name"])
        self.assertNotIn("FastMediaMX", body["poc_entity"])
        entity_names = [
            entity["name"] for entity in body["client_setup"]["entities"]
        ]
        self.assertIn("Ergode Inc.", entity_names)
        self.assertFalse(any("FastMediaMX" in name for name in entity_names))

    def test_tenant_settings_rejects_marketplace_account_as_workspace_identity(self):
        client = TestClient(create_app(), raise_server_exceptions=False)

        response = client.put(
            "/tenant/settings",
            json={
                "product_name": "Cashflow Command Center",
                "tenant_name": "FastMediaMX",
                "poc_entity": "FastMediaMX Canada",
                "brand": {"primary_color": "#087f7a", "logo_text": "FM"},
                "currencies": ["USD"],
                "sources": {
                    "marketplaces": [
                        {
                            "name": "Amazon Seller Central - FastMediaMX Canada",
                            "type": "Marketplace portal report",
                            "status": "Awaiting actual upload",
                        }
                    ],
                    "banks": [],
                },
                "client_setup": {
                    "entities": [
                        {
                            "name": "FastMediaMX Canada",
                            "currency": "USD",
                            "status": "Active seller account",
                        }
                    ]
                },
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("marketplace account", response.json()["detail"])

    def test_app_shell_contains_navigation_hooks(self):
        client = TestClient(create_app())

        response = client.get("/app")

        self.assertEqual(response.status_code, 200)
        for view_id in ("overview", "payouts", "sources", "admin"):
            self.assertIn(f'data-view="{view_id}"', response.text)
        self.assertNotIn('data-view="reconciliation"', response.text)
        self.assertIn('id="global-source-filter"', response.text)
        self.assertIn('id="global-currency-filter"', response.text)
        self.assertIn('id="global-period-filter"', response.text)
        self.assertIn('id="mobile-nav-toggle"', response.text)
        self.assertNotIn('id="poc-entity-label"', response.text)

    def test_reconciliation_detail_endpoints(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_app()
            service = Phase0Service(
                AppConfig(
                    samples_dir=PHASE0_FIXTURES,
                    reports_dir=Path(temp_dir) / "reports",
                    database_path=Path(temp_dir) / "ops.db",
                )
            )
            app.dependency_overrides[get_phase0_service] = lambda: service
            client = TestClient(app)

            matched = client.get("/phase0/matched-payouts")
            unmatched_expected = client.get("/phase0/unmatched-expected")
            unmatched_receipts = client.get("/phase0/unmatched-receipts")

        self.assertEqual(matched.status_code, 200)
        self.assertEqual(unmatched_expected.status_code, 200)
        self.assertEqual(unmatched_receipts.status_code, 200)
        self.assertEqual(len(matched.json()), 4)
        self.assertEqual(len(unmatched_expected.json()), 2)
        self.assertEqual(len(unmatched_receipts.json()), 1)


if __name__ == "__main__":
    unittest.main()
