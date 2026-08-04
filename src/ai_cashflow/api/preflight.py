"""Secret-safe production configuration validation used before a release switch."""

import os
from pathlib import Path

from ai_cashflow.api.security import SecuritySettings
from ai_cashflow.config import AppConfig


CANONICAL_ORIGIN = "https://www.aicashflow.pro"
NGINX_ALIGNED_MAX_BYTES = 10 * 1024 * 1024


def validate_production() -> None:
    settings = SecuritySettings.from_env()
    settings.validate_startup()
    if settings.public_origin != CANONICAL_ORIGIN:
        raise ValueError("AI_CASHFLOW_PUBLIC_ORIGIN must use the canonical production origin.")
    if settings.max_upload_bytes > NGINX_ALIGNED_MAX_BYTES:
        raise ValueError("AI_CASHFLOW_MAX_UPLOAD_BYTES exceeds the Nginx-aligned maximum.")
    if ":" not in os.getenv("AI_CASHFLOW_SMOKE_BASIC_AUTH", ""):
        raise ValueError("AI_CASHFLOW_SMOKE_BASIC_AUTH is missing or invalid.")
    if not os.getenv("AI_CASHFLOW_SMOKE_SOURCE_ID", "").strip():
        raise ValueError("AI_CASHFLOW_SMOKE_SOURCE_ID is required.")

    config = AppConfig()
    paths = {
        "samples directory": config.samples_dir,
        "reports directory": config.reports_dir,
        "tenant configuration": config.tenant_config_path,
    }
    for label, path in paths.items():
        candidate = Path(path)
        target = candidate if candidate.is_dir() else candidate.parent
        if not candidate.is_absolute() or not target.is_dir() or not os.access(target, os.R_OK | os.W_OK):
            raise ValueError(f"{label} is not an accessible absolute path.")


if __name__ == "__main__":
    try:
        validate_production()
    except (TypeError, ValueError) as exc:
        raise SystemExit(f"Production validation failed: {exc}") from None
    print("Production validation passed.")
