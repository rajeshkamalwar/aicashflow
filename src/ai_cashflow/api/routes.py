"""HTTP routes for the cashflow command center API."""

from pathlib import Path
import json
import os
import re

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse

from ai_cashflow.api.schemas import (
    AmazonIntegrationUpdate,
    AmazonSourceUpdate,
    AmazonTransactionBackfillCreate,
    SellerCentralStatementSnapshotCreate,
    HealthResponse,
    Phase0SummaryResponse,
    RunReportResponse,
)
from ai_cashflow.amazon_sp_api import AmazonSpApiClient, AmazonSpApiError
from ai_cashflow.api.services import Phase0Service
from ai_cashflow.api.security import (
    Principal,
    require_administrator,
    require_administrator_or_machine,
    require_machine,
    require_proxy_user,
    require_user_or_machine,
)
from ai_cashflow.config import AppConfig
from ai_cashflow.integrations import AmazonIntegrationManager, AmazonSourceRegistry


router = APIRouter()
WEB_DIR = Path(__file__).resolve().parents[1] / "web"
ROOT_DIR = Path(__file__).resolve().parents[3]
SCRAPED_DIR = ROOT_DIR / "data" / "scraped" / "seller-central"
API_INGEST_ROW_LIMIT = 10_000
SUPPORTED_API_REPORT_TYPES = frozenset({
    "transactions",
    "settlements",
    "payouts",
    "bank_receipts",
    "marketplace_ar",
})


def _safe_build_value(name: str, filename: str, default: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        path = ROOT_DIR / filename
        value = path.read_text(encoding="utf-8").strip() if path.is_file() else default
    return value if re.fullmatch(r"[A-Za-z0-9._:+-]+", value) else default


def _app_shell() -> HTMLResponse:
    release_id = _safe_build_value(
        "AI_CASHFLOW_RELEASE_ID", "RELEASE_ID", "development"
    )
    built_at = _safe_build_value(
        "AI_CASHFLOW_BUILD_TIMESTAMP", "BUILD_TIMESTAMP", "unknown"
    )
    html = (WEB_DIR / "app.html").read_text(encoding="utf-8")
    html = html.replace("__RELEASE_ID__", release_id).replace("__BUILD_TIMESTAMP__", built_at)
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


def get_phase0_service() -> Phase0Service:
    return Phase0Service(AppConfig())


def get_amazon_integration_manager() -> AmazonIntegrationManager:
    key = os.getenv("AI_CASHFLOW_MASTER_KEY", "")
    if not key:
        raise HTTPException(
            status_code=503,
            detail="The integration encryption key has not been provisioned.",
        )
    return AmazonIntegrationManager(AppConfig().database_path, key)


def get_amazon_source_registry() -> AmazonSourceRegistry:
    key = os.getenv("AI_CASHFLOW_MASTER_KEY", "")
    if not key:
        raise HTTPException(
            status_code=503,
            detail="The integration encryption key has not been provisioned.",
        )
    return AmazonSourceRegistry(AppConfig().database_path, key)


require_super_admin = require_administrator
require_api_key = require_user_or_machine


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok")


@router.get("/ready")
def ready(_: Principal = Depends(require_administrator_or_machine)) -> dict[str, str]:
    return {"status": "ready"}


@router.get("/", response_class=HTMLResponse)
def root(_: Principal = Depends(require_proxy_user)) -> HTMLResponse:
    return _app_shell()


@router.get("/app", response_class=HTMLResponse)
def app_shell(_: Principal = Depends(require_proxy_user)) -> HTMLResponse:
    return _app_shell()


@router.get("/auth/session", response_model=dict[str, object])
def auth_session(principal: Principal = Depends(require_proxy_user)) -> dict[str, object]:
    return {"username": principal.username, "is_admin": principal.is_admin}


@router.get("/phase0/sources", response_model=list[dict[str, object]])
def phase0_sources(
    _: Principal = Depends(require_user_or_machine),
    registry: AmazonSourceRegistry = Depends(get_amazon_source_registry),
) -> list[dict[str, object]]:
    allowed = {
        "id", "name", "marketplace", "marketplaces", "enabled", "status",
        "sync_frequency_minutes", "last_tested_at", "last_sync_at", "updated_at",
    }
    return [{key: value for key, value in source.items() if key in allowed} for source in registry.list_sources()]


@router.get("/admin/integrations/amazon", response_model=dict[str, object])
def get_amazon_integration(
    _: None = Depends(require_super_admin),
    manager: AmazonIntegrationManager = Depends(get_amazon_integration_manager),
) -> dict[str, object]:
    return manager.public()


@router.get("/admin/integrations/amazon/sources", response_model=list[dict[str, object]])
def list_amazon_sources(
    _: None = Depends(require_super_admin),
    registry: AmazonSourceRegistry = Depends(get_amazon_source_registry),
) -> list[dict[str, object]]:
    return registry.list_sources()


@router.post("/admin/integrations/amazon/sources", response_model=dict[str, object])
def create_amazon_source(
    payload: AmazonSourceUpdate,
    _: None = Depends(require_super_admin),
    registry: AmazonSourceRegistry = Depends(get_amazon_source_registry),
    service: Phase0Service = Depends(get_phase0_service),
) -> dict[str, object]:
    try:
        source = registry.upsert(payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    service.audit("amazon.source.created", source["id"])
    return source


@router.put("/admin/integrations/amazon/sources/{source_id}", response_model=dict[str, object])
def update_amazon_source(
    source_id: str,
    payload: AmazonSourceUpdate,
    _: None = Depends(require_super_admin),
    registry: AmazonSourceRegistry = Depends(get_amazon_source_registry),
    service: Phase0Service = Depends(get_phase0_service),
) -> dict[str, object]:
    try:
        source = registry.upsert(payload.model_dump(), source_id)
    except ValueError as exc:
        raise HTTPException(status_code=404 if "not found" in str(exc).lower() else 422, detail=str(exc)) from exc
    service.audit("amazon.source.updated", source_id)
    return source


@router.post("/admin/integrations/amazon/sources/{source_id}/test", response_model=dict[str, object])
def test_amazon_source(
    source_id: str,
    _: None = Depends(require_super_admin),
    registry: AmazonSourceRegistry = Depends(get_amazon_source_registry),
    service: Phase0Service = Depends(get_phase0_service),
) -> dict[str, object]:
    try:
        transactions = AmazonSpApiClient(
            registry.credentials(source_id)
        ).probe_payments_access()
        source = registry.record_connection_test(source_id, transactions)
    except ValueError as exc:
        if "not found" not in str(exc).lower():
            registry.record_connection_test(source_id, error=str(exc))
        raise HTTPException(status_code=404 if "not found" in str(exc).lower() else 422, detail=str(exc)) from exc
    except AmazonSpApiError as exc:
        registry.record_connection_test(source_id, error=str(exc))
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    service.audit("amazon.source.tested", source_id)
    return {"status": "connected", "source": source}


@router.post("/admin/integrations/amazon/sources/{source_id}/sync", response_model=dict[str, object])
def sync_amazon_source(
    source_id: str,
    _: Principal = Depends(require_administrator_or_machine),
    registry: AmazonSourceRegistry = Depends(get_amazon_source_registry),
    service: Phase0Service = Depends(get_phase0_service),
) -> dict[str, object]:
    try:
        result = registry.sync_source(
            source_id,
            AmazonSpApiClient(registry.credentials(source_id)),
            service.config.samples_dir,
        )
        report = service.run_report()
    except ValueError as exc:
        if "not found" not in str(exc).lower():
            registry.record_sync_failure(source_id, str(exc))
        raise HTTPException(status_code=404 if "not found" in str(exc).lower() else 422, detail=str(exc)) from exc
    except AmazonSpApiError as exc:
        registry.record_sync_failure(source_id, str(exc))
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    proof_status = "not_due"
    if result["reports_ingested"] or service.cashflow_proof_due(source_id):
        try:
            proof_status = str(
                service.run_cashflow_proof(source_id=source_id)["status"]
            )
        except Exception as exc:
            proof_status = "failed"
            service.audit(
                "reconciliation.proof.failed", f"{source_id}: {str(exc)[:400]}"
            )
    service.audit("amazon.source.synced", source_id)
    return {
        **result,
        "report_status": report.status,
        "proof_status": proof_status,
    }


@router.post("/admin/reconciliation/bank-statements", response_model=dict[str, object])
async def import_bank_statement(
    file: UploadFile = File(...),
    bank_source: str = Form(...),
    column_mapping: str | None = Form(default=None),
    _: Principal = Depends(require_administrator_or_machine),
    service: Phase0Service = Depends(get_phase0_service),
) -> dict[str, object]:
    if file.content_type not in {
        "text/csv", "application/csv", "application/vnd.ms-excel",
        "application/octet-stream",
    }:
        raise HTTPException(status_code=422, detail="Bank statement must be a CSV file.")
    content = await file.read(service.config.max_upload_bytes + 1)
    try:
        mapping = json.loads(column_mapping) if column_mapping else None
        if mapping is not None and not isinstance(mapping, dict):
            raise ValueError("Column mapping must be a JSON object.")
        return service.import_bank_statement(
            file_name=file.filename or "",
            content=content,
            bank_source=bank_source,
            column_mapping=mapping,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/admin/reconciliation/proofs/run", response_model=dict[str, object])
def run_cashflow_proof(
    _: Principal = Depends(require_administrator_or_machine),
    service: Phase0Service = Depends(get_phase0_service),
) -> dict[str, object]:
    try:
        return service.run_cashflow_proof()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/admin/integrations/amazon/sources/{source_id}/syncs", response_model=list[dict[str, object]])
def list_amazon_source_syncs(
    source_id: str,
    _: None = Depends(require_super_admin),
    registry: AmazonSourceRegistry = Depends(get_amazon_source_registry),
) -> list[dict[str, object]]:
    try:
        return registry.list_syncs(source_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/admin/integrations/amazon/sources/{source_id}/transaction-collection",
    response_model=dict[str, object],
)
def amazon_transaction_collection_diagnostics(
    source_id: str,
    _: None = Depends(require_super_admin),
    registry: AmazonSourceRegistry = Depends(get_amazon_source_registry),
) -> dict[str, object]:
    try:
        return registry.transaction_collection_diagnostics(source_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/admin/integrations/amazon/sources/{source_id}/transaction-backfills",
    response_model=dict[str, object],
)
def create_amazon_transaction_backfill(
    source_id: str,
    payload: AmazonTransactionBackfillCreate,
    _: None = Depends(require_super_admin),
    registry: AmazonSourceRegistry = Depends(get_amazon_source_registry),
) -> dict[str, object]:
    try:
        return registry.create_transaction_backfill(
            source_id=source_id,
            **payload.model_dump(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get(
    "/admin/integrations/amazon/sources/{source_id}/transaction-backfills",
    response_model=list[dict[str, object]],
)
def list_amazon_transaction_backfills(
    source_id: str,
    _: None = Depends(require_super_admin),
    registry: AmazonSourceRegistry = Depends(get_amazon_source_registry),
) -> list[dict[str, object]]:
    return registry.list_transaction_backfills(source_id)


@router.post(
    "/admin/integrations/amazon/transaction-backfills/{job_id}/{action}",
    response_model=dict[str, object],
)
def control_amazon_transaction_backfill(
    job_id: str,
    action: str,
    _: None = Depends(require_super_admin),
    registry: AmazonSourceRegistry = Depends(get_amazon_source_registry),
) -> dict[str, object]:
    try:
        if action == "pause":
            return registry.pause_transaction_backfill(job_id)
        if action == "resume":
            return registry.resume_transaction_backfill(job_id)
        if action == "run":
            job = registry.get_transaction_backfill(job_id)
            return registry.run_transaction_backfill(
                job_id,
                AmazonSpApiClient(registry.credentials(job["source_id"])),
            )
        raise ValueError("Backfill action must be pause, resume, or run.")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except AmazonSpApiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.delete("/admin/integrations/amazon/sources/{source_id}/credentials", response_model=dict[str, object])
def delete_amazon_source_credentials(
    source_id: str,
    _: None = Depends(require_super_admin),
    registry: AmazonSourceRegistry = Depends(get_amazon_source_registry),
    service: Phase0Service = Depends(get_phase0_service),
) -> dict[str, object]:
    try:
        source = registry.delete_source_credentials(source_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    service.audit("amazon.source.credentials.deleted", source_id)
    return source


@router.put("/admin/integrations/amazon", response_model=dict[str, object])
def update_amazon_integration(
    payload: AmazonIntegrationUpdate,
    _: None = Depends(require_super_admin),
    manager: AmazonIntegrationManager = Depends(get_amazon_integration_manager),
    service: Phase0Service = Depends(get_phase0_service),
) -> dict[str, object]:
    try:
        result = manager.save(payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    service.audit("amazon.integration.updated", "Amazon integration settings updated by super admin")
    return result


@router.delete("/admin/integrations/amazon/credentials", response_model=dict[str, object])
def delete_amazon_credentials(
    _: None = Depends(require_super_admin),
    manager: AmazonIntegrationManager = Depends(get_amazon_integration_manager),
    service: Phase0Service = Depends(get_phase0_service),
) -> dict[str, object]:
    result = manager.delete_credentials()
    service.audit("amazon.credentials.deleted", "Amazon credentials removed by super admin")
    return result


@router.post("/admin/integrations/amazon/test", response_model=dict[str, object])
def test_amazon_integration(
    _: None = Depends(require_super_admin),
    manager: AmazonIntegrationManager = Depends(get_amazon_integration_manager),
    service: Phase0Service = Depends(get_phase0_service),
) -> dict[str, object]:
    try:
        reports = AmazonSpApiClient(manager.credentials()).list_settlement_reports()
    except ValueError as exc:
        manager.record_connection_test(success=False, error=str(exc))
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except AmazonSpApiError as exc:
        manager.record_connection_test(success=False, error=str(exc))
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    service.audit("amazon.connection.tested", f"Connection passed; {len(reports)} report(s) visible")
    return {
        "status": "connected",
        "reports_visible": len(reports),
        "integration": manager.record_connection_test(success=True),
    }


@router.post("/admin/integrations/amazon/sync", response_model=dict[str, object])
def sync_amazon_integration(
    _: Principal = Depends(require_administrator_or_machine),
    manager: AmazonIntegrationManager = Depends(get_amazon_integration_manager),
    service: Phase0Service = Depends(get_phase0_service),
) -> dict[str, object]:
    try:
        result = manager.sync(
            AmazonSpApiClient(manager.credentials()), service.config.samples_dir
        )
        report = service.run_report()
    except ValueError as exc:
        manager.record_sync_failure(str(exc))
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except AmazonSpApiError as exc:
        manager.record_sync_failure(str(exc))
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    service.audit("amazon.sync.completed", result["message"])
    return {**result, "report_status": report.status}


@router.get("/admin/integrations/amazon/syncs", response_model=list[dict[str, object]])
def list_amazon_syncs(
    _: None = Depends(require_super_admin),
    manager: AmazonIntegrationManager = Depends(get_amazon_integration_manager),
) -> list[dict[str, object]]:
    return manager.list_syncs()


@router.get("/phase0/summary", response_model=Phase0SummaryResponse)
def phase0_summary(
    _: None = Depends(require_api_key),
    service: Phase0Service = Depends(get_phase0_service),
) -> Phase0SummaryResponse:
    return Phase0SummaryResponse.model_validate(service.get_summary().__dict__)


@router.get("/phase0/reconciliation/proof", response_model=dict[str, object])
def cashflow_proof_summary(
    _: None = Depends(require_api_key),
    service: Phase0Service = Depends(get_phase0_service),
) -> dict[str, object]:
    return service.get_cashflow_proof_summary()


@router.get("/phase0/reconciliation/proof/items", response_model=dict[str, object])
def cashflow_proof_items(
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _: None = Depends(require_api_key),
    service: Phase0Service = Depends(get_phase0_service),
) -> dict[str, object]:
    return service.list_cashflow_proof_items(limit=limit, offset=offset)


@router.get("/reports/summary", response_model=Phase0SummaryResponse)
def report_summary(
    _: None = Depends(require_api_key),
    service: Phase0Service = Depends(get_phase0_service),
) -> Phase0SummaryResponse:
    return phase0_summary(_, service)


@router.get("/phase0/exceptions", response_model=dict[str, int])
def phase0_exceptions(
    _: None = Depends(require_api_key),
    service: Phase0Service = Depends(get_phase0_service),
) -> dict[str, int]:
    return service.get_exceptions()


@router.get("/reports/exceptions", response_model=dict[str, int])
def report_exceptions(
    _: None = Depends(require_api_key),
    service: Phase0Service = Depends(get_phase0_service),
) -> dict[str, int]:
    return phase0_exceptions(_, service)


@router.get("/phase0/data-quality", response_model=list[dict[str, str]])
def phase0_data_quality(
    _: None = Depends(require_api_key),
    service: Phase0Service = Depends(get_phase0_service),
) -> list[dict[str, str]]:
    return service.get_data_quality()


@router.get("/phase0/input-readiness", response_model=dict[str, object])
def phase0_input_readiness(
    _: None = Depends(require_api_key),
    service: Phase0Service = Depends(get_phase0_service),
) -> dict[str, object]:
    return service.get_input_readiness()


@router.get("/phase0/calculation-audit", response_model=dict[str, object])
def phase0_calculation_audit(
    _: None = Depends(require_api_key),
    service: Phase0Service = Depends(get_phase0_service),
) -> dict[str, object]:
    return service.get_calculation_audit()


@router.get("/phase0/uploads", response_model=list[dict[str, str]])
def phase0_uploads(
    _: None = Depends(require_api_key),
    service: Phase0Service = Depends(get_phase0_service),
) -> list[dict[str, str]]:
    return service.list_uploads()


@router.delete("/phase0/uploads/{file_name}", response_model=dict[str, object])
def phase0_delete_upload(
    file_name: str,
    _: Principal = Depends(require_administrator),
    service: Phase0Service = Depends(get_phase0_service),
) -> dict[str, object]:
    try:
        return service.delete_upload(file_name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/phase0/runs", response_model=list[dict[str, object]])
def phase0_runs(
    _: None = Depends(require_api_key),
    service: Phase0Service = Depends(get_phase0_service),
) -> list[dict[str, object]]:
    return service.list_runs()


@router.delete("/phase0/runs", response_model=dict[str, object])
def phase0_clear_runs(
    _: Principal = Depends(require_administrator),
    service: Phase0Service = Depends(get_phase0_service),
) -> dict[str, object]:
    deleted = service.clear_runs()
    return {"deleted": deleted}


@router.get("/phase0/audit-events", response_model=list[dict[str, str]])
def phase0_audit_events(
    _: Principal = Depends(require_administrator),
    service: Phase0Service = Depends(get_phase0_service),
) -> list[dict[str, str]]:
    return service.list_audit_events()


@router.get("/tenant/settings", response_model=dict[str, object])
def tenant_settings(
    _: None = Depends(require_api_key),
    service: Phase0Service = Depends(get_phase0_service),
) -> dict[str, object]:
    return service.get_tenant_settings()


@router.put("/tenant/settings", response_model=dict[str, object])
def update_tenant_settings(
    payload: dict[str, object],
    _: Principal = Depends(require_administrator),
    service: Phase0Service = Depends(get_phase0_service),
) -> dict[str, object]:
    try:
        return service.update_tenant_settings(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/phase0/matched-payouts", response_model=list[dict[str, str]])
def phase0_matched_payouts(
    _: None = Depends(require_api_key),
    service: Phase0Service = Depends(get_phase0_service),
) -> list[dict[str, str]]:
    return service.get_matched_payouts()


@router.get("/phase0/unmatched-expected", response_model=list[dict[str, str]])
def phase0_unmatched_expected(
    _: None = Depends(require_api_key),
    service: Phase0Service = Depends(get_phase0_service),
) -> list[dict[str, str]]:
    return service.get_unmatched_expected()


@router.get("/phase0/unmatched-receipts", response_model=list[dict[str, str]])
def phase0_unmatched_receipts(
    _: None = Depends(require_api_key),
    service: Phase0Service = Depends(get_phase0_service),
) -> list[dict[str, str]]:
    return service.get_unmatched_receipts()


@router.get("/phase0/marketplace-activity", response_model=dict[str, object])
def phase0_marketplace_activity(
    _: None = Depends(require_api_key),
    service: Phase0Service = Depends(get_phase0_service),
) -> dict[str, object]:
    return service.get_marketplace_activity_response()


@router.get("/phase0/marketplace-ar", response_model=dict[str, object])
def phase0_marketplace_ar(
    _: None = Depends(require_api_key),
    service: Phase0Service = Depends(get_phase0_service),
) -> dict[str, object]:
    return service.get_marketplace_ar()


@router.post("/phase0/api-ingest", response_model=dict[str, object])
def phase0_api_ingest(
    payload: dict[str, object],
    _: Principal = Depends(require_machine),
    service: Phase0Service = Depends(get_phase0_service),
) -> dict[str, object]:
    """Receive JSON rows from a live marketplace or bank API, persist as CSV,
    and immediately re-run reconciliation.

    Expected payload::

        {
          "source_name": "Amazon Seller Central – CA",
          "report_type": "marketplace_ar", // transactions | settlements | payouts | bank_receipts | marketplace_ar
          "rows": [
            {"date/time": "2026-05-26T05:21:09Z", "settlement id": "123", ...},
            ...
          ]
        }

    Field names are normalised using a loose alias table — see GET /phase0/api-status
    for the full list of accepted aliases per report type.
    """
    source_name = str(payload.get("source_name", "api_source"))
    rows = payload.get("rows", [])
    report_type = (
        str(payload.get("report_type", "transactions"))
        .lower()
        .replace(" ", "_")
        .replace("-", "_")
    )
    if not isinstance(rows, list):
        raise HTTPException(status_code=422, detail="'rows' must be a JSON array of objects.")
    if len(rows) > API_INGEST_ROW_LIMIT:
        raise HTTPException(status_code=422, detail=f"'rows' cannot exceed {API_INGEST_ROW_LIMIT} items.")
    if any(not isinstance(row, dict) for row in rows):
        raise HTTPException(status_code=422, detail="Each item in 'rows' must be a JSON object.")
    if report_type not in SUPPORTED_API_REPORT_TYPES:
        raise HTTPException(status_code=422, detail="Unsupported report type.")
    try:
        return service.ingest_api_data(source_name, rows, report_type)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/phase0/api-status", response_model=dict[str, object])
def phase0_api_status(
    _: Principal = Depends(require_administrator_or_machine),
    service: Phase0Service = Depends(get_phase0_service),
) -> dict[str, object]:
    """Return configured API sources, accepted field schemas, and polling metadata."""
    return service.get_api_status()


@router.get("/phase0/amazon/settlement-reports", response_model=list[dict[str, object]])
def phase0_amazon_settlement_reports(
    _: Principal = Depends(require_administrator_or_machine),
    service: Phase0Service = Depends(get_phase0_service),
) -> list[dict[str, object]]:
    try:
        return service.list_amazon_settlement_reports()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except AmazonSpApiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/phase0/amazon/financial-position", response_model=dict[str, object])
def phase0_amazon_financial_position(
    source_id: str,
    currency: str | None = None,
    snapshot_id: str | None = None,
    _: None = Depends(require_api_key),
    service: Phase0Service = Depends(get_phase0_service),
) -> dict[str, object]:
    try:
        response = (
            service.get_amazon_financial_position(source_id, currency, snapshot_id)
            if snapshot_id
            else service.get_amazon_financial_position(source_id, currency)
        )
        response.pop("financial_event_group_diagnostics", None)
        return response
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except AmazonSpApiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/admin/seller-central/statement-snapshots", response_model=dict[str, object])
def create_seller_central_statement_snapshot(
    payload: SellerCentralStatementSnapshotCreate,
    principal: Principal = Depends(require_administrator),
    registry: AmazonSourceRegistry = Depends(get_amazon_source_registry),
) -> dict[str, object]:
    try:
        return registry.create_seller_central_statement_snapshot(payload.model_dump(), principal.username)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/phase0/seller-central/statement-snapshots/latest", response_model=dict[str, object])
def latest_seller_central_statement_snapshot(
    source_id: str, marketplace_id: str, currency: str,
    _: None = Depends(require_api_key), registry: AmazonSourceRegistry = Depends(get_amazon_source_registry),
) -> dict[str, object]:
    return {"snapshot": registry.latest_seller_central_statement_snapshot(source_id, marketplace_id, currency)}


@router.get("/admin/seller-central/statement-snapshots", response_model=list[dict[str, object]])
def seller_central_statement_snapshot_history(
    source_id: str, marketplace_id: str, currency: str,
    _: Principal = Depends(require_administrator), registry: AmazonSourceRegistry = Depends(get_amazon_source_registry),
) -> list[dict[str, object]]:
    return registry.list_seller_central_statement_snapshots(source_id, marketplace_id, currency)


@router.get("/phase0/amazon/canonical-payout-state", response_model=dict[str, object])
def phase0_amazon_canonical_payout_state(
    source_id: str | None = None,
    currency: str | None = None,
    _: None = Depends(require_api_key),
    service: Phase0Service = Depends(get_phase0_service),
) -> dict[str, object]:
    try:
        return service.get_canonical_payout_state(source_id, currency)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/phase0/seller-central/statement", response_model=dict[str, object])
def phase0_seller_central_statement(
    _: Principal = Depends(require_administrator),
) -> dict[str, object]:
    """Return the latest safe Seller Central Statement View scrape, if present."""
    path = SCRAPED_DIR / "statement-view-latest.json"
    if not path.exists():
        return {"status": "missing", "message": "No Seller Central statement scrape found."}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail="Statement scrape JSON is invalid.") from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=422, detail="Statement scrape must be a JSON object.")
    return {"status": "loaded", **data}


@router.get("/phase0/source-evidence", response_model=list[dict[str, str]])
def phase0_source_evidence(
    _: None = Depends(require_api_key),
    service: Phase0Service = Depends(get_phase0_service),
) -> list[dict[str, str]]:
    return service.get_source_evidence()


@router.get("/reports/source-evidence", response_model=list[dict[str, str]])
def report_source_evidence(
    _: None = Depends(require_api_key),
    service: Phase0Service = Depends(get_phase0_service),
) -> list[dict[str, str]]:
    return phase0_source_evidence(_, service)


def _cash_position_dashboard(service: Phase0Service) -> HTMLResponse:
    dashboard_path = service.config.reports_dir / "cfo_dashboard.html"
    if not dashboard_path.exists():
        service.run_report()
    elif "Cash Position Report" not in dashboard_path.read_text(encoding="utf-8"):
        service.run_report()
    return HTMLResponse(dashboard_path.read_text(encoding="utf-8"))


@router.get("/phase0/dashboard", response_class=HTMLResponse)
def phase0_dashboard(
    _: None = Depends(require_api_key),
    service: Phase0Service = Depends(get_phase0_service),
) -> HTMLResponse:
    return _cash_position_dashboard(service)


@router.get("/reports/cash-position", response_class=HTMLResponse)
def cash_position_report(
    _: None = Depends(require_api_key),
    service: Phase0Service = Depends(get_phase0_service),
) -> HTMLResponse:
    return _cash_position_dashboard(service)


@router.post("/phase0/run-report", response_model=RunReportResponse)
def phase0_run_report(
    _: Principal = Depends(require_administrator_or_machine),
    service: Phase0Service = Depends(get_phase0_service),
) -> RunReportResponse:
    result = service.run_report()
    return RunReportResponse(status=result.status, reports_dir=str(result.reports_dir))
