import asyncio
from pathlib import Path
import tempfile
import unittest

from cryptography.fernet import Fernet
from ai_cashflow.api.app import create_app
from ai_cashflow.api.security import SecuritySettings
from ai_cashflow.api.routes import get_amazon_source_registry
from security_helpers import PROXY_SECRET, create_test_app, proxy_headers, test_client


ROOT = Path(__file__).resolve().parents[1]


class SecurityArtifactTests(unittest.TestCase):
    def production_settings(self, root: Path) -> SecuritySettings:
        return SecuritySettings(
            environment="production",
            dev_auth_bypass=False,
            trusted_proxies=("127.0.0.1/32", "::1/128"),
            proxy_assertion_secret="p" * 32,
            admin_users=frozenset({"admin"}),
            api_key="k" * 32,
            api_key_previous="r" * 32,
            master_key=Fernet.generate_key().decode("ascii"),
            database_path=root / "operations.sqlite3",
            public_origin="https://cashflow.example",
            max_upload_bytes=1024,
        )

    def test_valid_production_settings_pass_and_unsafe_variants_fail(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = self.production_settings(Path(temp_dir))
            settings.validate_startup()
            invalid = (
                {"trusted_proxies": ("0.0.0.0/0",)},
                {"proxy_assertion_secret": "short"},
                {"admin_users": frozenset()},
                {"api_key": "short"},
                {"master_key": "invalid"},
                {"database_path": Path("relative.sqlite3")},
                {"public_origin": "http://cashflow.example"},
            )
            for change in invalid:
                with self.subTest(change=next(iter(change))):
                    candidate = SecuritySettings(**{**settings.__dict__, **change})
                    with self.assertRaises(ValueError):
                        candidate.validate_startup()

    def test_invalid_production_configuration_aborts_application_startup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            invalid = SecuritySettings(**{
                **self.production_settings(Path(temp_dir)).__dict__,
                "proxy_assertion_secret": "short",
            })
            app = create_app(invalid)
            async def start() -> None:
                async with app.router.lifespan_context(app):
                    pass
            with self.assertRaises(ValueError):
                asyncio.run(start())

    def test_sanitized_sources_exclude_sensitive_and_internal_fields(self):
        class Registry:
            def list_sources(self):
                return [{
                    "id": "source-1",
                    "name": "Books Canada",
                    "status": "Connected",
                    "enabled": True,
                    "last_sync_at": "2026-08-04T12:00:00Z",
                    "seller_id": "seller-secret",
                    "application_id": "application-secret",
                    "credentials": {"refresh_token_configured": True},
                    "last_error": "raw internal error",
                    "database_path": "/secret/path",
                }]

        app = create_test_app()
        app.dependency_overrides[get_amazon_source_registry] = Registry
        response = test_client(app, authenticated=False).get(
            "/phase0/sources", headers=proxy_headers("analyst")
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            [{
                "id": "source-1",
                "name": "Books Canada",
                "status": "Connected",
                "enabled": True,
                "last_sync_at": "2026-08-04T12:00:00Z",
            }],
        )

    def test_session_response_is_minimal_and_assets_require_browser_auth(self):
        app = create_test_app()
        unauthenticated = test_client(app, authenticated=False)
        self.assertEqual(unauthenticated.get("/assets/app.js").status_code, 401)
        session = unauthenticated.get("/auth/session", headers=proxy_headers("analyst"))
        self.assertEqual(session.json(), {"username": "analyst", "is_admin": False})

    def test_frontend_has_one_fetch_boundary_and_no_browser_secret_handling(self):
        javascript = (ROOT / "src/ai_cashflow/web/app.js").read_text(encoding="utf-8")
        html = (ROOT / "src/ai_cashflow/web/app.html").read_text(encoding="utf-8")
        self.assertEqual(javascript.count("fetch("), 1)
        self.assertNotIn("X-API-Key", javascript + html)
        self.assertNotIn("x-api-key", (javascript + html).lower())
        self.assertIn("Authentication required", javascript)
        self.assertIn("You do not have permission", javascript)
        self.assertIn('fetchJson("/auth/session")', javascript)

    def test_nginx_uses_protected_assertion_include_and_clears_spoof_headers(self):
        nginx = (ROOT / "nginx_vhost.conf").read_text(encoding="utf-8")
        self.assertIn("/etc/nginx/aicashflow/proxy-assertion.conf", nginx)
        self.assertIn('proxy_set_header X-AI-Cashflow-Authenticated-User "";', nginx)
        self.assertIn('proxy_set_header X-AI-Cashflow-Proxy-Assertion "";', nginx)
        self.assertIn('proxy_set_header X-Admin-User "";', nginx)
        self.assertNotIn(PROXY_SECRET, nginx)
        self.assertIn("location = /phase0/api-ingest", nginx)
        self.assertNotIn("location /phase0", nginx)

    def test_service_command_disables_proxy_headers_and_uses_one_worker(self):
        deploy = (ROOT / "deploy.sh").read_text(encoding="utf-8")
        self.assertIn("--host 127.0.0.1", deploy)
        self.assertIn("--workers 1", deploy)
        self.assertIn("--no-proxy-headers", deploy)
        self.assertIn("EnvironmentFile=", deploy)


if __name__ == "__main__":
    unittest.main()
