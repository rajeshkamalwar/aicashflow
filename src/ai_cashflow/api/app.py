"""FastAPI application factory."""

import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from ai_cashflow.api.routes import router
from ai_cashflow.api.security import SecuritySettings, authenticate_proxy
from ai_cashflow.amazon_sp_api import AmazonSpApiClient
from ai_cashflow.api.services import Phase0Service
from ai_cashflow.config import AppConfig
from ai_cashflow.integrations import AmazonIntegrationManager, AmazonSourceRegistry


def _run_scheduled_amazon_sync() -> None:
    key = os.getenv("AI_CASHFLOW_MASTER_KEY", "")
    if not key:
        return
    config = AppConfig()
    registry = AmazonSourceRegistry(config.database_path, key)
    source_ids = registry.due_source_ids()
    if registry.list_sources():
        if not source_ids:
            return
        service = Phase0Service(config)
        synced = False
        proof_source_ids = []
        for source_id in source_ids:
            try:
                result = registry.sync_source(
                    source_id,
                    AmazonSpApiClient(registry.credentials(source_id)),
                    config.samples_dir,
                    collect_transactions=False,
                )
                service.audit("amazon.source.sync.scheduled", f"{source_id}: {result['message']}")
                synced = True
                if result["reports_ingested"] or service.cashflow_proof_due(source_id):
                    proof_source_ids.append(source_id)
            except Exception as exc:
                registry.record_sync_failure(source_id, str(exc))
                service.audit("amazon.source.sync.failed", f"{source_id}: {str(exc)[:400]}")
        if synced:
            service.run_report()
        for source_id in proof_source_ids:
            try:
                service.run_cashflow_proof(source_id=source_id)
            except Exception as exc:
                service.audit(
                    "reconciliation.proof.failed",
                    f"{source_id}: {str(exc)[:400]}",
                )
        return
    manager = AmazonIntegrationManager(config.database_path, key)
    if not manager.is_sync_due():
        return
    service = Phase0Service(config)
    try:
        result = manager.sync(AmazonSpApiClient(manager.credentials()), config.samples_dir)
        service.run_report()
        service.audit("amazon.sync.scheduled", result["message"])
    except Exception as exc:
        manager.record_sync_failure(str(exc))
        service.audit("amazon.sync.failed", str(exc)[:500])


def _run_scheduled_transaction_sync() -> None:
    if os.getenv("AI_CASHFLOW_TRANSACTION_COLLECTION_ENABLED", "false").lower() not in {
        "1", "true", "yes", "on",
    }:
        return
    key = os.getenv("AI_CASHFLOW_MASTER_KEY", "")
    if not key:
        return
    config = AppConfig()
    registry = AmazonSourceRegistry(config.database_path, key)
    now = datetime.now(timezone.utc)
    for source in registry.list_sources():
        if not source["enabled"] or source["status"] != "Connected" or not source["last_sync_at"]:
            continue
        last_sync = datetime.fromisoformat(source["last_sync_at"])
        if now - last_sync >= timedelta(minutes=4):
            continue
        registry.run_incremental_transaction_collection(
            source["id"], AmazonSpApiClient(registry.credentials(source["id"])),
            max_pages=max(1, int(os.getenv("AI_CASHFLOW_TRANSACTION_MAX_PAGES_PER_SYNC", "5"))),
            max_seconds=max(1, int(os.getenv("AI_CASHFLOW_TRANSACTION_MAX_SECONDS_PER_SYNC", "20"))),
        )


async def _scheduler_loop(worker, sleep=asyncio.sleep) -> None:
    while True:
        try:
            await worker()
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        await sleep(60)


async def _amazon_scheduler() -> None:
    async def worker() -> None:
        started = datetime.now(timezone.utc)
        key = os.getenv("AI_CASHFLOW_MASTER_KEY", "")
        registry = AmazonSourceRegistry(AppConfig().database_path, key) if key else None
        if registry:
            registry.record_scheduler_state(
                "open_balance_sync", started_at=started.isoformat(),
                lock_owner="open_balance_scheduler",
            )
        try:
            await asyncio.to_thread(_run_scheduled_amazon_sync)
        except Exception:
            if registry:
                registry.record_scheduler_state("open_balance_sync", failed=True)
            raise
        if registry:
            completed = datetime.now(timezone.utc)
            registry.record_scheduler_state(
                "open_balance_sync", completed_at=completed.isoformat(),
                duration_ms=int((completed - started).total_seconds() * 1000),
            )

    await _scheduler_loop(worker)


async def _amazon_transaction_scheduler() -> None:
    async def worker() -> None:
        started = datetime.now(timezone.utc)
        key = os.getenv("AI_CASHFLOW_MASTER_KEY", "")
        registry = AmazonSourceRegistry(AppConfig().database_path, key) if key else None
        if registry:
            registry.record_scheduler_state(
                "incremental_transaction_sync", started_at=started.isoformat(),
                lock_owner="transaction_scheduler",
            )
        try:
            await asyncio.to_thread(_run_scheduled_transaction_sync)
        except Exception:
            if registry:
                registry.record_scheduler_state("incremental_transaction_sync", failed=True)
            raise
        if registry:
            completed = datetime.now(timezone.utc)
            registry.record_scheduler_state(
                "incremental_transaction_sync", completed_at=completed.isoformat(),
                duration_ms=int((completed - started).total_seconds() * 1000),
            )

    await _scheduler_loop(worker)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    app.state.security_settings.validate_startup()
    tasks = (
        [asyncio.create_task(_amazon_scheduler()), asyncio.create_task(_amazon_transaction_scheduler())]
        if os.getenv("AI_CASHFLOW_MASTER_KEY") else []
    )
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task


def create_app(security_settings: SecuritySettings | None = None) -> FastAPI:
    settings = security_settings or SecuritySettings.from_env()
    production = settings.environment == "production"
    app = FastAPI(
        title="AI Cashflow API",
        version="0.1.0",
        lifespan=_lifespan,
        docs_url=None if production else "/docs",
        redoc_url=None if production else "/redoc",
        openapi_url=None if production else "/openapi.json",
    )
    app.state.security_settings = settings

    @app.middleware("http")
    async def enforce_request_policy(request: Request, call_next):
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip()
        if content_type == "application/json":
            try:
                declared_size = int(request.headers.get("content-length", "0"))
            except ValueError:
                declared_size = 0
            if declared_size > settings.max_upload_bytes:
                return JSONResponse(
                    status_code=413,
                    content={"detail": "JSON request is too large."},
                )
            chunks: list[bytes] = []
            size = 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > settings.max_upload_bytes:
                    return JSONResponse(
                        status_code=413,
                        content={"detail": "JSON request is too large."},
                    )
                chunks.append(chunk)
            request._body = b"".join(chunks)
        if request.url.path == "/assets" or request.url.path.startswith("/assets/"):
            try:
                authenticate_proxy(request, settings)
            except HTTPException as exc:
                return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
        response = await call_next(request)
        if request.url.path == "/assets" or request.url.path.startswith("/assets/"):
            if request.query_params.get("v"):
                response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            else:
                response.headers["Cache-Control"] = "no-cache"
        return response

    web_dir = Path(__file__).resolve().parents[1] / "web"
    app.mount("/assets", StaticFiles(directory=web_dir), name="assets")
    app.include_router(router)
    return app


app = create_app()
