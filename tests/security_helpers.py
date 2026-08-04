from pathlib import Path

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from ai_cashflow.api.app import create_app
from ai_cashflow.api.security import SecuritySettings


PROXY_SECRET = "proxy-assertion-secret-32-bytes-minimum"
ACTIVE_KEY = "active-machine-api-key-32-bytes-minimum"
PREVIOUS_KEY = "previous-machine-api-key-32-bytes-min"
PUBLIC_ORIGIN = "https://cashflow.test"


def security_settings(root: Path | None = None) -> SecuritySettings:
    return SecuritySettings(
        environment="test",
        dev_auth_bypass=False,
        trusted_proxies=("127.0.0.1/32",),
        proxy_assertion_secret=PROXY_SECRET,
        admin_users=frozenset({"admin"}),
        api_key=ACTIVE_KEY,
        api_key_previous=PREVIOUS_KEY,
        master_key=Fernet.generate_key().decode("ascii"),
        database_path=(root or Path.cwd()) / "test-security.sqlite3",
        public_origin=PUBLIC_ORIGIN,
        max_upload_bytes=10 * 1024 * 1024,
    )


def create_test_app(root: Path | None = None):
    app = create_app(security_settings(root))

    @app.middleware("http")
    async def use_loopback_test_peer(request, call_next):
        request.scope["client"] = ("127.0.0.1", 50000)
        return await call_next(request)

    return app


def proxy_headers(username: str = "admin", *, mutation: bool = True) -> dict[str, str]:
    headers = {
        "X-AI-Cashflow-Authenticated-User": username,
        "X-AI-Cashflow-Proxy-Assertion": PROXY_SECRET,
    }
    if mutation:
        headers.update({
            "Origin": PUBLIC_ORIGIN,
            "X-AI-Cashflow-Request": "browser",
        })
    return headers


def machine_headers(key: str = ACTIVE_KEY) -> dict[str, str]:
    return {"X-API-Key": key}


def test_client(app, *, authenticated: bool = True, **kwargs) -> TestClient:
    client = TestClient(app, **kwargs)
    if authenticated:
        client.headers.update(proxy_headers())
    return client
