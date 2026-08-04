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

### Production Release Prerequisites

- Canonical origin: `https://www.aicashflow.pro`. HTTP on either hostname and
  HTTPS on the apex hostname redirect to it.
- Python 3.11, Git, Nginx, systemd, curl, and read-only access from the server
  to the approved private Git remote.
- The exact release commit must already exist on that remote. A commit that
  exists only in a developer worktree is not deployable.
- `/etc/aicashflow/aicashflow.env`: owner `root:aicashflow`, mode `0640`.
- `/etc/nginx/aicashflow/proxy-assertion.conf`: owner `root:root`, mode `0600`.
- `/etc/nginx/.htpasswd-aicashflow`: owner `root:www-data`, mode `0640`.
- `/var/lib/aicashflow/{samples,reports,config}` must be readable and writable
  by `aicashflow`; the database path must be absolute and its parent writable.
- The environment file must include the canonical public origin, the external
  data paths from `.env.example`, an approved smoke-test source ID, and a
  smoke-only Basic Auth credential. These values are never printed by scripts.
- Amazon transaction collection and visibility both default to disabled. For a
  collection-only validation rollout, set
  `AI_CASHFLOW_TRANSACTION_COLLECTION_ENABLED=true` and
  `AI_CASHFLOW_TRANSACTION_VISIBILITY_ENABLED=false`. Production preflight
  rejects visibility when collection is disabled.
- The certificate paths referenced by `nginx_vhost.conf` must contain a valid
  certificate for both `aicashflow.pro` and `www.aicashflow.pro`.
- Before the first Phase 1C deployment, a separately reviewed one-time
  migration must place the verified Phase 1B release under
  `/opt/aicashflow/releases` and point `/opt/aicashflow/current` to it. The
  deploy script fails closed when no valid current release exists, because an
  automatic rollback would otherwise be impossible.

Do not paste environment-file contents, assertion include contents, API keys,
master keys, smoke credentials, or full `nginx -T` output into tickets, logs,
or copied terminal output.

### Locked Dependencies

Production installs `requirements.lock` with exact versions and SHA-256 hashes.
Regenerate it only in a reviewed Python 3.11 environment:

```bash
python -m pip install "pip<26" pip-tools==7.5.1
python -m piptools compile --generate-hashes --strip-extras --output-file=requirements.lock requirements.txt
python -m pip install --require-hashes -r requirements.lock
```

The production server must use `requirements.lock`; `requirements.txt` remains
the human-maintained input and is not installed directly during a release.

### Commit-Pinned Deployment

The release layout is immutable application code plus external mutable data:

```text
/opt/aicashflow/releases/<full-commit>
/opt/aicashflow/current  -> active release
/opt/aicashflow/previous -> preserved prior release
/var/lib/aicashflow      -> database, reports, samples, tenant configuration
```

After reviewing and pushing the clean Phase 1C commit to the approved private
remote:

```bash
sudo bash deploy.sh \
  --commit <full-40-character-commit> \
  --repository <approved-private-git-url>
```

`AI_CASHFLOW_PYTHON_BIN` selects the deployment interpreter and defaults to
`/usr/bin/python3.12`. The deploy stops unless that exact configured executable
exists and reports Python 3.12. After restart it waits up to 30 seconds for the
service, loopback listener, and exact health response before verification; a
timeout restores the previous release.

The script clones the requested commit into a temporary clean checkout,
verifies the exact revision and clean tree, archives only committed files,
installs the hashed lock in a new virtual environment, validates production
configuration as `aicashflow`, verifies the candidate systemd unit, and only
then atomically changes `/opt/aicashflow/current`.

After restart it checks the exact health body, service state, Basic Auth,
authenticated readiness, the JavaScript release ID, and the financial-position
API. Any failure restores the previous symlink and restarts the old release.
No older release is deleted automatically.

HTML is served with `Cache-Control: no-store`. JavaScript and CSS URLs contain
the full release ID and versioned assets are immutable, so authentication-code
changes do not require a browser hard refresh. This application has no service
worker.

### Read-Only Production Verification

Run without changing production state:

```bash
sudo bash /opt/aicashflow/current/verify-production.sh
```

It emits safe PASS/FAIL labels for configuration shape, protected metadata,
systemd, loopback binding, Nginx, redirects, size limits, writable paths, and
the active release, plus safe yes/no transaction collection and visibility
status. It never prints protected values and intentionally does not run
`nginx -T`.

### Rollback

```bash
sudo bash /opt/aicashflow/current/rollback.sh
```

Rollback atomically restores `/opt/aicashflow/previous`, restarts the service,
and checks health. The failed release remains under `/opt/aicashflow/releases`
for investigation. Phase 2A transaction tables are additive and persisted
outside release directories; hiding visibility or rolling back code does not
delete observations, checkpoints, or canonical transaction state.

Leave `AI_CASHFLOW_API_KEY_PREVIOUS` set only for the planned rotation window.

Operational history is stored in SQLite:

- uploaded files;
- report run history;
- audit events.

Uploads and JSON requests are rejected with `413` when they exceed
`AI_CASHFLOW_MAX_UPLOAD_BYTES` (10 MiB in the production example). Nginx uses
`client_max_body_size 11m` for transport overhead. JSON API ingestion is also
limited to 10,000 object rows; unsupported report types and malformed rows are
rejected with `422` before any data is written.

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
