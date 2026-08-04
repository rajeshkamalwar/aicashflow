import logging
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from starlette.requests import Request

from ai_cashflow.api.app import create_app
from ai_cashflow.api.security import SecuritySettings
from ai_cashflow.api.security import authenticate_proxy
from security_helpers import (
    ACTIVE_KEY,
    PREVIOUS_KEY,
    PROXY_SECRET,
    create_test_app,
    machine_headers,
    proxy_headers,
    security_settings,
    test_client,
)


class AuthenticationSecurityTests(unittest.TestCase):
    @staticmethod
    def _request(app, host: str, headers: dict[str, str] | None = None) -> Request:
        return Request({
            "type": "http",
            "app": app,
            "method": "GET",
            "path": "/auth/session",
            "raw_path": b"/auth/session",
            "query_string": b"",
            "scheme": "http",
            "server": ("test", 80),
            "client": (host, 50000),
            "headers": [
                (name.lower().encode("latin-1"), value.encode("latin-1"))
                for name, value in (headers or {}).items()
            ],
        })

    def test_proxy_requires_assertion_and_trusted_peer(self):
        app = create_test_app()
        trusted = test_client(app, authenticated=False)
        username = {"X-AI-Cashflow-Authenticated-User": "admin"}
        self.assertEqual(trusted.get("/auth/session", headers=username).status_code, 401)
        self.assertEqual(
            trusted.get(
                "/auth/session",
                headers={**username, "X-AI-Cashflow-Proxy-Assertion": "wrong"},
            ).status_code,
            401,
        )
        self.assertEqual(
            trusted.get("/auth/session", headers=proxy_headers()).status_code,
            200,
        )
        with self.assertRaisesRegex(Exception, "401"):
            authenticate_proxy(
                self._request(app, "203.0.113.7", proxy_headers()),
                app.state.security_settings,
            )

    def test_empty_username_and_x_admin_user_spoof_are_rejected(self):
        client = test_client(create_test_app(), authenticated=False)
        empty = proxy_headers("")
        self.assertEqual(client.get("/auth/session", headers=empty).status_code, 401)
        self.assertEqual(
            client.get("/auth/session", headers={"X-Admin-User": "admin"}).status_code,
            401,
        )

    def test_case_sensitive_admin_and_machine_route_matrix(self):
        app = create_test_app()
        client = test_client(app, authenticated=False)
        normal = proxy_headers("Admin")
        self.assertEqual(client.get("/auth/session", headers=normal).json()["is_admin"], False)
        self.assertEqual(client.get("/admin/integrations/amazon", headers=normal).status_code, 403)
        spoofed_admin = {**proxy_headers("analyst"), "X-Admin-User": "admin"}
        self.assertEqual(
            client.get("/admin/integrations/amazon", headers=spoofed_admin).status_code,
            403,
        )
        self.assertEqual(client.get("/", headers=machine_headers()).status_code, 401)
        self.assertEqual(
            client.get("/admin/integrations/amazon", headers=machine_headers()).status_code,
            403,
        )

    def test_active_and_previous_machine_keys_rotate(self):
        client = test_client(create_test_app(), authenticated=False)
        self.assertEqual(client.get("/ready", headers=machine_headers(ACTIVE_KEY)).status_code, 200)
        self.assertEqual(client.get("/ready", headers=machine_headers(PREVIOUS_KEY)).status_code, 200)
        self.assertEqual(client.get("/ready", headers=machine_headers("invalid-previous")).status_code, 401)

    def test_machine_only_endpoint_ignores_proxy_identity(self):
        client = test_client(create_test_app(), authenticated=False)
        proxy_only = proxy_headers("admin")
        self.assertEqual(client.post("/phase0/api-ingest", json=[], headers=proxy_only).status_code, 401)
        both = {**proxy_only, **machine_headers()}
        response = client.post("/phase0/api-ingest", json=[], headers=both)
        self.assertNotEqual(response.status_code, 401)

    def test_browser_mutations_require_origin_and_custom_header(self):
        client = test_client(create_test_app(), authenticated=False)
        bare = proxy_headers("admin", mutation=False)
        wrong_origin = {**proxy_headers(), "Origin": "https://evil.example"}
        self.assertEqual(client.post("/phase0/run-report", headers=bare).status_code, 403)
        self.assertEqual(client.post("/phase0/run-report", headers=wrong_origin).status_code, 403)

    def test_health_is_exact_and_sensitive_values_never_leak(self):
        app = create_test_app()
        client = test_client(app, authenticated=False)
        with self.assertLogs("ai_cashflow.auth", level=logging.WARNING) as captured:
            denied = client.get(
                "/auth/session",
                headers={
                    "X-AI-Cashflow-Authenticated-User": "admin",
                    "X-AI-Cashflow-Proxy-Assertion": PROXY_SECRET + "wrong",
                },
            )
        health = client.get("/health")
        ready = client.get("/ready", headers=machine_headers())
        session = client.get("/auth/session", headers=proxy_headers())
        html = client.get("/", headers=proxy_headers()).text
        javascript = client.get("/assets/app.js", headers=proxy_headers()).text
        combined = "\n".join(captured.output) + denied.text + health.text + ready.text + session.text + html + javascript
        self.assertEqual(health.json(), {"status": "ok"})
        self.assertNotIn(PROXY_SECRET, combined)
        self.assertNotIn(ACTIVE_KEY, combined)

    def test_development_bypass_is_loopback_development_only(self):
        settings = security_settings()
        dev = SecuritySettings(**{**settings.__dict__, "environment": "development", "dev_auth_bypass": True})
        app = create_app(dev)
        self.assertEqual(
            authenticate_proxy(self._request(app, "127.0.0.1"), dev).username,
            "development",
        )
        with self.assertRaisesRegex(Exception, "401"):
            authenticate_proxy(self._request(app, "203.0.113.7"), dev)
        invalid = SecuritySettings(**{**settings.__dict__, "dev_auth_bypass": True})
        with self.assertRaisesRegex(ValueError, "development bypass"):
            invalid.validate_startup()

    def test_production_configuration_fails_closed_without_secrets(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            environment = {
                "AI_CASHFLOW_ENV": "production",
                "AI_CASHFLOW_DATABASE_PATH": str(Path(temp_dir) / "cashflow.sqlite3"),
            }
            with patch.dict(os.environ, environment, clear=True):
                settings = SecuritySettings.from_env()
            with self.assertRaises(ValueError) as raised:
                settings.validate_startup()
            self.assertNotIn(PROXY_SECRET, str(raised.exception))

    def test_production_docs_are_disabled(self):
        settings = SecuritySettings(**{**security_settings().__dict__, "environment": "production"})
        app = create_app(settings)
        client = test_client(app)
        self.assertEqual(client.get("/docs").status_code, 404)
        self.assertEqual(client.get("/redoc").status_code, 404)
        self.assertEqual(client.get("/openapi.json").status_code, 404)

    def test_normal_financial_response_has_no_diagnostics(self):
        class Service:
            def get_amazon_financial_position(self, *_):
                return {"status": "available", "financial_event_group_diagnostics": [{"secret": True}]}

        from ai_cashflow.api.routes import get_phase0_service

        app = create_test_app()
        app.dependency_overrides[get_phase0_service] = lambda: Service()
        response = test_client(app).get(
            "/phase0/amazon/financial-position", params={"source_id": "source-1"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("financial_event_group_diagnostics", response.json())


if __name__ == "__main__":
    unittest.main()
