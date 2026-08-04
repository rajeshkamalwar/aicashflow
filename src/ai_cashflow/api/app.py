"""FastAPI application factory."""

import asyncio
from contextlib import asynccontextmanager, suppress
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from ai_cashflow.api.routes import router
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


async def _amazon_scheduler() -> None:
    while True:
        await asyncio.to_thread(_run_scheduled_amazon_sync)
        await asyncio.sleep(60)


@asynccontextmanager
async def _lifespan(_: FastAPI):
    task = asyncio.create_task(_amazon_scheduler()) if os.getenv("AI_CASHFLOW_MASTER_KEY") else None
    try:
        yield
    finally:
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


def create_app() -> FastAPI:
    app = FastAPI(title="AI Cashflow API", version="0.1.0", lifespan=_lifespan)
    web_dir = Path(__file__).resolve().parents[1] / "web"
    app.mount("/assets", StaticFiles(directory=web_dir), name="assets")
    app.include_router(router)
    return app


app = create_app()
