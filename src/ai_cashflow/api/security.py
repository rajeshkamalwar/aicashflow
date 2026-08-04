"""Fail-closed request authentication and route authorization."""

from dataclasses import dataclass
import ipaddress
import logging
import os
from pathlib import Path
import secrets
from typing import Union
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from fastapi import Depends, HTTPException, Request


LOGGER = logging.getLogger("ai_cashflow.auth")
AUTHENTICATION_DETAIL = "Authentication required."
PERMISSION_DETAIL = "You do not have permission."
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


@dataclass(frozen=True)
class ProxyUser:
    username: str
    is_admin: bool = False


@dataclass(frozen=True)
class ProxyAdministrator:
    username: str
    is_admin: bool = True


@dataclass(frozen=True)
class MachineClient:
    username: str = "machine"
    is_admin: bool = False


@dataclass(frozen=True)
class DevelopmentPrincipal:
    username: str = "development"
    is_admin: bool = True


Principal = Union[ProxyUser, ProxyAdministrator, MachineClient, DevelopmentPrincipal]


def _boolean(value: str | None) -> bool:
    if value is None or not value.strip():
        return False
    normalized = value.strip().lower()
    if normalized not in {"true", "false"}:
        raise ValueError("Boolean security configuration must be true or false.")
    return normalized == "true"


@dataclass(frozen=True)
class SecuritySettings:
    environment: str
    dev_auth_bypass: bool
    trusted_proxies: tuple[str, ...]
    proxy_assertion_secret: str
    admin_users: frozenset[str]
    api_key: str
    api_key_previous: str | None
    master_key: str
    database_path: Path
    public_origin: str
    max_upload_bytes: int

    @classmethod
    def from_env(cls) -> "SecuritySettings":
        return cls(
            environment=os.getenv("AI_CASHFLOW_ENV", "").strip().lower(),
            dev_auth_bypass=_boolean(os.getenv("AI_CASHFLOW_DEV_AUTH_BYPASS")),
            trusted_proxies=tuple(
                part.strip()
                for part in os.getenv("AI_CASHFLOW_TRUSTED_PROXIES", "").split(",")
                if part.strip()
            ),
            proxy_assertion_secret=os.getenv("AI_CASHFLOW_PROXY_ASSERTION_SECRET", ""),
            admin_users=frozenset(
                part.strip()
                for part in os.getenv("AI_CASHFLOW_ADMIN_USERS", "").split(",")
                if part.strip()
            ),
            api_key=os.getenv("AI_CASHFLOW_API_KEY", ""),
            api_key_previous=os.getenv("AI_CASHFLOW_API_KEY_PREVIOUS") or None,
            master_key=os.getenv("AI_CASHFLOW_MASTER_KEY", ""),
            database_path=Path(os.getenv("AI_CASHFLOW_DATABASE_PATH", "")),
            public_origin=os.getenv("AI_CASHFLOW_PUBLIC_ORIGIN", "").rstrip("/"),
            max_upload_bytes=int(os.getenv("AI_CASHFLOW_MAX_UPLOAD_BYTES", str(10 * 1024 * 1024))),
        )

    def validate_startup(self) -> None:
        if self.environment not in {"development", "test", "production"}:
            raise ValueError("AI_CASHFLOW_ENV must be development, test, or production.")
        if self.dev_auth_bypass and self.environment != "development":
            raise ValueError("The development bypass is valid only in development.")
        if self.max_upload_bytes <= 0:
            raise ValueError("AI_CASHFLOW_MAX_UPLOAD_BYTES must be positive.")
        if self.environment != "production":
            return

        errors: list[str] = []
        if self.dev_auth_bypass:
            errors.append("development bypass must be disabled")
        networks = self._trusted_networks(errors)
        if not networks:
            errors.append("trusted proxies are required")
        if len(self.proxy_assertion_secret.encode("utf-8")) < 32:
            errors.append("proxy assertion secret must contain at least 32 bytes")
        if not self.admin_users:
            errors.append("administrator allowlist is required")
        if len(self.api_key.encode("utf-8")) < 32:
            errors.append("active machine API key must contain at least 32 bytes")
        if self.api_key_previous and len(self.api_key_previous.encode("utf-8")) < 32:
            errors.append("previous machine API key must contain at least 32 bytes")
        try:
            Fernet(self.master_key.encode("ascii"))
        except Exception:
            errors.append("master key is missing or invalid")
        if not self.database_path.is_absolute():
            errors.append("database path must be absolute")
        else:
            parent = self.database_path.parent
            if not parent.is_dir() or not os.access(parent, os.R_OK | os.W_OK):
                errors.append("database path is inaccessible")
        origin = urlsplit(self.public_origin)
        if (
            origin.scheme != "https"
            or not origin.netloc
            or origin.path not in {"", "/"}
            or origin.query
            or origin.fragment
        ):
            errors.append("public origin must be an HTTPS origin")
        if errors:
            raise ValueError("Invalid production security configuration: " + "; ".join(dict.fromkeys(errors)))

    def _trusted_networks(
        self, errors: list[str] | None = None
    ) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
        networks = []
        for value in self.trusted_proxies:
            try:
                network = ipaddress.ip_network(value, strict=False)
            except ValueError:
                if errors is not None:
                    errors.append("trusted proxy entry is invalid")
                continue
            minimum = 24 if network.version == 4 else 64
            if network.prefixlen < minimum:
                if errors is not None:
                    errors.append("trusted proxy range is overly broad")
                continue
            networks.append(network)
        return tuple(networks)

    def peer_is_trusted(self, host: str) -> bool:
        try:
            peer = ipaddress.ip_address(host)
        except ValueError:
            return False
        return any(peer in network for network in self._trusted_networks())


def get_security_settings(request: Request) -> SecuritySettings:
    return request.app.state.security_settings


def _proxy_headers_present(request: Request) -> bool:
    return bool(
        request.headers.get("X-AI-Cashflow-Authenticated-User") is not None
        or request.headers.get("X-AI-Cashflow-Proxy-Assertion") is not None
    )


def _development_principal(request: Request, settings: SecuritySettings) -> DevelopmentPrincipal | None:
    host = request.client.host if request.client else ""
    if settings.environment == "development" and settings.dev_auth_bypass:
        try:
            if ipaddress.ip_address(host).is_loopback:
                return DevelopmentPrincipal()
        except ValueError:
            pass
    return None


def authenticate_proxy(request: Request, settings: SecuritySettings) -> Principal:
    development = _development_principal(request, settings)
    if development and not _proxy_headers_present(request):
        return development
    username = request.headers.get("X-AI-Cashflow-Authenticated-User", "")
    assertion = request.headers.get("X-AI-Cashflow-Proxy-Assertion", "")
    host = request.client.host if request.client else ""
    username_is_valid = bool(
        username
        and username == username.strip()
        and len(username) <= 128
        and all(ord(character) >= 32 and ord(character) != 127 for character in username)
    )
    assertion_is_valid = bool(
        settings.proxy_assertion_secret
        and secrets.compare_digest(
            assertion.encode("utf-8"), settings.proxy_assertion_secret.encode("utf-8")
        )
    )
    if not settings.peer_is_trusted(host) or not username_is_valid or not assertion_is_valid:
        LOGGER.warning("Proxy authentication rejected for %s %s", request.method, request.url.path)
        raise HTTPException(status_code=401, detail=AUTHENTICATION_DETAIL)
    if username in settings.admin_users:
        return ProxyAdministrator(username)
    return ProxyUser(username)


def authenticate_machine(request: Request, settings: SecuritySettings) -> MachineClient:
    candidate = request.headers.get("X-API-Key", "")
    active = (settings.api_key or "\0").encode("utf-8")
    previous = (settings.api_key_previous or "\0").encode("utf-8")
    supplied = candidate.encode("utf-8")
    active_match = secrets.compare_digest(supplied, active)
    previous_match = secrets.compare_digest(supplied, previous)
    if not candidate or not (active_match or previous_match):
        LOGGER.warning("Machine authentication rejected for %s %s", request.method, request.url.path)
        raise HTTPException(status_code=401, detail=AUTHENTICATION_DETAIL)
    return MachineClient()


def _protect_browser_mutation(request: Request, settings: SecuritySettings) -> None:
    if request.method in SAFE_METHODS:
        return
    if (
        not settings.public_origin
        or request.headers.get("Origin", "").rstrip("/") != settings.public_origin
        or request.headers.get("X-AI-Cashflow-Request") != "browser"
    ):
        raise HTTPException(status_code=403, detail=PERMISSION_DETAIL)


def require_proxy_user(
    request: Request,
    settings: SecuritySettings = Depends(get_security_settings),
) -> Principal:
    principal = authenticate_proxy(request, settings)
    _protect_browser_mutation(request, settings)
    return principal


def require_administrator(
    request: Request,
    settings: SecuritySettings = Depends(get_security_settings),
) -> Principal:
    if not _proxy_headers_present(request) and request.headers.get("X-API-Key"):
        authenticate_machine(request, settings)
        raise HTTPException(status_code=403, detail=PERMISSION_DETAIL)
    principal = authenticate_proxy(request, settings)
    if not principal.is_admin:
        raise HTTPException(status_code=403, detail=PERMISSION_DETAIL)
    _protect_browser_mutation(request, settings)
    return principal


def require_machine(
    request: Request,
    settings: SecuritySettings = Depends(get_security_settings),
) -> MachineClient:
    return authenticate_machine(request, settings)


def require_user_or_machine(
    request: Request,
    settings: SecuritySettings = Depends(get_security_settings),
) -> Principal:
    if _proxy_headers_present(request):
        principal = authenticate_proxy(request, settings)
        _protect_browser_mutation(request, settings)
        return principal
    return authenticate_machine(request, settings)


def require_administrator_or_machine(
    request: Request,
    settings: SecuritySettings = Depends(get_security_settings),
) -> Principal:
    if _proxy_headers_present(request):
        principal = authenticate_proxy(request, settings)
        if not principal.is_admin:
            raise HTTPException(status_code=403, detail=PERMISSION_DETAIL)
        _protect_browser_mutation(request, settings)
        return principal
    return authenticate_machine(request, settings)
