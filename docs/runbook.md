# AI Cashflow Phase 0 Runbook

## Install Runtime Dependencies

Use Python 3.11 or newer.

```bash
python -m pip install -e .
```

## Run Tests

```bash
set PYTHONPATH=src
python -m unittest discover -s tests
```

## Generate Phase 0 Reports

```bash
python scripts/generate_phase0_report.py
```

Outputs are written to:

```text
reports/phase0/
```

Key outputs:

- `cfo_summary.md`
- `cfo_dashboard.html`
- `matched_payouts.csv`
- `unmatched_expected_payouts.csv`
- `unmatched_bank_receipts.csv`
- `exception_summary.csv`
- `data_quality_report.csv`

## Start Backend API

```bash
set PYTHONPATH=src
uvicorn ai_cashflow.api.app:app --reload
```

API endpoints:

- `GET /`
- `GET /health`
- `GET /ready`
- `GET /tenant/settings`
- `PUT /tenant/settings`
- `GET /phase0/summary`
- `GET /phase0/exceptions`
- `GET /phase0/data-quality`
- `GET /phase0/uploads`
- `POST /phase0/uploads`
- `GET /phase0/runs`
- `GET /phase0/audit-events`
- `GET /phase0/matched-payouts`
- `GET /phase0/unmatched-expected`
- `GET /phase0/unmatched-receipts`
- `GET /phase0/dashboard`
- `POST /phase0/run-report`

## Production Foundation Settings

The local app remains open by default for development. For a protected deployment,
set an API key before starting Uvicorn:

```bash
set AI_CASHFLOW_ENV=production
set AI_CASHFLOW_API_KEY=<strong-random-value>
set AI_CASHFLOW_DATABASE_PATH=D:\cashflow\data\operations.sqlite3
set AI_CASHFLOW_MAX_UPLOAD_BYTES=10485760
set PYTHONPATH=src
uvicorn ai_cashflow.api.app:app --host 0.0.0.0 --port 8000
```

When `AI_CASHFLOW_API_KEY` is set, protected API routes require:

```text
X-API-Key: <strong-random-value>
```

Operational history is stored in SQLite:

- uploaded files;
- report run history;
- audit events.

Uploads are limited to CSV files in approved categories and are rejected when
they exceed `AI_CASHFLOW_MAX_UPLOAD_BYTES`.

## White-Label Tenant Configuration

The app loads tenant branding and Phase 0 settings from:

```text
config/tenant.local.json
```

If that file is missing, it falls back to:

```text
config/tenant.example.json
```

Configure:

- `product_name`
- `tenant_name`
- `poc_entity`
- `brand.primary_color`
- `brand.logo_text`
- `currencies`
- `reconciliation.date_tolerance_days`
- `reconciliation.amount_tolerance`
- `sources.marketplaces`
- `sources.banks`

`tenant.local.json` is ignored by git so each deployment can have its own white-label settings.

The Settings page in `/app` can update the same tenant file through `PUT /tenant/settings`.

## Add Sample Files

Place sanitized files under:

```text
data/samples/
```

Use `phase0/mapping-templates.md` when a source file does not match the normalized CSV columns.

Do not place real credentials, MFA secrets, or unapproved sensitive banking data in the repository.
