# Phase 0 Backend Implementation Plan

## Goal

Wrap the existing Phase 0 ingestion, data-quality, reconciliation, exception, and reporting engine with a thin FastAPI backend. Keep storage file-based until real source data and usage patterns justify PostgreSQL.

## Stack

- Python
- FastAPI
- Pydantic
- Uvicorn for local serving
- Existing `ai_cashflow` modules
- File-based data under `data/samples` and `reports/phase0`

## Files

- `src/ai_cashflow/config.py`: Phase 0 paths and reconciliation settings.
- `src/ai_cashflow/api/app.py`: FastAPI app factory.
- `src/ai_cashflow/api/routes.py`: HTTP routes.
- `src/ai_cashflow/api/schemas.py`: API response models.
- `src/ai_cashflow/api/services.py`: File/report service layer.
- `docs/runbook.md`: How to run reports, tests, and API.
- `tests/test_config.py`: Config behavior.
- `tests/test_api_services.py`: Service-layer behavior.
- `tests/test_api_app.py`: FastAPI route behavior.

## API

- `GET /health`
- `GET /phase0/summary`
- `GET /phase0/exceptions`
- `GET /phase0/data-quality`
- `POST /phase0/run-report`

## Verification

- `python -m unittest discover -s tests`
- `python scripts/generate_phase0_report.py`
- `uvicorn ai_cashflow.api.app:app --reload`
