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

## Production Security Configuration

Production is fail-closed. Copy `.env.example` to
`/etc/aicashflow/aicashflow.env`, replace every placeholder, make it readable
only by root and the `aicashflow` service account, and configure:

- `AI_CASHFLOW_ENV=production`
- `AI_CASHFLOW_DEV_AUTH_BYPASS=false`
- `AI_CASHFLOW_TRUSTED_PROXIES` with only the immediate Nginx peer addresses
- `AI_CASHFLOW_PROXY_ASSERTION_SECRET` with at least 32 random bytes
- `AI_CASHFLOW_ADMIN_USERS` with comma-separated, case-sensitive Basic Auth users
- `AI_CASHFLOW_API_KEY` with at least 32 random bytes
- `AI_CASHFLOW_API_KEY_PREVIOUS` only during key rotation
- `AI_CASHFLOW_MASTER_KEY` with a Fernet key
- `AI_CASHFLOW_DATABASE_PATH` with an absolute, service-writable path
- `AI_CASHFLOW_PUBLIC_ORIGIN` with the exact HTTPS browser origin
- `AI_CASHFLOW_MAX_UPLOAD_BYTES` with a positive byte limit

The assertion secret is also installed in the root-controlled file
`/etc/nginx/aicashflow/proxy-assertion.conf` (mode `0600`):

```nginx
proxy_set_header X-AI-Cashflow-Proxy-Assertion "REPLACE_WITH_THE_SAME_SECRET";
```

Never commit either protected file. Nginx overwrites the authenticated username,
proxy assertion, and legacy `X-Admin-User` headers. Uvicorn listens only on
`127.0.0.1`, uses one worker, and runs with `--no-proxy-headers`.

Authentication principals and route roles:

- Browser users require Nginx Basic Auth plus the trusted proxy assertion.
- Browser administrators additionally require an exact username match in
  `AI_CASHFLOW_ADMIN_USERS`.
- Machine clients use the active or previous `X-API-Key` only on approved
  synchronization, ingestion, report-generation, and readiness routes.
- `/health` is public and returns only `{"status":"ok"}`.
- Credential management and financial diagnostics remain administrator-only.
- Normal dashboard source discovery uses the sanitized `/phase0/sources` route.

Browser `POST`, `PUT`, `PATCH`, and `DELETE` requests must carry the exact
`AI_CASHFLOW_PUBLIC_ORIGIN` Origin and `X-AI-Cashflow-Request: browser`.
Machine-key requests are exempt from this browser CSRF check.

Development bypass is permitted only with both
`AI_CASHFLOW_ENV=development` and `AI_CASHFLOW_DEV_AUTH_BYPASS=true`, and only
for direct loopback requests. It is rejected in test and production.

### Safe Deployment Sequence

1. Back up the current application revision, environment file, Nginx vhost, and database.
2. Create the locked `aicashflow` service account and grant only the required database/report paths.
3. Install the protected environment, htpasswd, and assertion include files.
4. Upload the reviewed commit and run `bash deploy.sh`.
5. Run `nginx -t`, install `nginx_vhost.conf`, reload Nginx, and verify `/health`.
6. Verify browser login, `/auth/session`, administrator denial/allow behavior, and one machine readiness request.
7. Leave `AI_CASHFLOW_API_KEY_PREVIOUS` set only for the planned rotation window.

Rollback by restoring the prior revision and prior protected configuration,
restarting `aicashflow`, testing `nginx -t`, and reloading Nginx. Restore the
database backup only if a separate data migration was performed; Phase 1B has
no schema migration.

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
