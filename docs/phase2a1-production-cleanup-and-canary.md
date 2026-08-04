# Phase 2A.1 production cleanup and canary

## Selected cleanup: targeted Phase 2A removal

The verified pre-deployment backup has no transaction tables, but production has
continued to receive source-sync, audit, and report-run writes. Restoring the
whole backup would discard legitimate data. Remove only the failed Phase 2A
transaction tables' contents and preserve every unrelated table.

The following is a prepared maintenance procedure. Run it only after the
corrected schema has been initialized with both transaction flags still false.
It is intentionally not executed by the application or deployment workflow.

```bash
set -euo pipefail

maintenance_id="phase2a-cleanup-$(date -u +%Y%m%dT%H%M%SZ)"
database_path="/var/lib/aicashflow/operations.sqlite3"
backup_dir="/var/backups/aicashflow/${maintenance_id}"
working_database="${backup_dir}/operations.cleanup.sqlite3"
compact_database="${backup_dir}/operations.compact.sqlite3"

test "$(readlink -f "${database_path}")" = "/var/lib/aicashflow/operations.sqlite3"
install -d -m 0700 "${backup_dir}"
sqlite3 "${database_path}" ".backup '${backup_dir}/operations.before.sqlite3'"
sha256sum "${backup_dir}/operations.before.sqlite3" > "${backup_dir}/operations.before.sha256"
sha256sum -c "${backup_dir}/operations.before.sha256"
cp --reflink=auto --preserve=mode,ownership,timestamps \
  "${backup_dir}/operations.before.sqlite3" "${working_database}"

sqlite3 "${working_database}" <<'SQL'
PRAGMA foreign_keys=ON;
BEGIN IMMEDIATE;
DELETE FROM amazon_transaction_staging;
DELETE FROM amazon_transaction_backfill_jobs;
DELETE FROM amazon_transaction_checkpoints;
DELETE FROM amazon_transaction_state;
DELETE FROM amazon_transaction_observations;
DELETE FROM amazon_transaction_payloads;
DELETE FROM amazon_transaction_runs;
COMMIT;
PRAGMA foreign_key_check;
PRAGMA integrity_check;
SQL

sqlite3 "${working_database}" "VACUUM INTO '${compact_database}';"
sqlite3 "${compact_database}" "PRAGMA integrity_check;"
sqlite3 "${compact_database}" \
  "SELECT 'observations', COUNT(*) FROM amazon_transaction_observations
   UNION ALL SELECT 'state', COUNT(*) FROM amazon_transaction_state
   UNION ALL SELECT 'checkpoints', COUNT(*) FROM amazon_transaction_checkpoints
   UNION ALL SELECT 'runs', COUNT(*) FROM amazon_transaction_runs
   UNION ALL SELECT 'staging', COUNT(*) FROM amazon_transaction_staging
   UNION ALL SELECT 'payloads', COUNT(*) FROM amazon_transaction_payloads
   UNION ALL SELECT 'backfills', COUNT(*) FROM amazon_transaction_backfill_jobs;"
sha256sum "${compact_database}" > "${backup_dir}/operations.compact.sha256"
```

Before atomic replacement, compare unrelated table counts with the live database,
stop the application, take a final online backup, repeat the targeted transaction,
and verify the compact copy again. Preserve the original 441 MB database. After
replacement restore ownership/mode, start the service, and verify `/health`,
`/phase0/marketplace-ar`, and `/phase0/api-status`. Rollback is an atomic restore
of `operations.before.sqlite3` followed by the same integrity and endpoint checks.

## First canary

1. Deploy corrected code with collection and visibility both `false`.
2. Verify existing open balances, Marketplace AR, API Status, authentication, and
   the five-minute scheduler.
3. Create one explicit administrator backfill for one source, one marketplace,
   status `DEFERRED`, and a one-hour window. Keep visibility `false`.
4. Run only the bounded job step. Record pages, tokens, observation/state counts,
   conflicts, throttles, database bytes, free disk, and source-sync freshness.
5. Run the identical canary again; it must add zero observation rows.
6. Enable incremental collection only after the canary is reviewed and accepted.

Default automatic pause thresholds are 128 MiB database growth, 1 GiB minimum
free disk, 5% status conflicts, three consecutive throttled pages, and 15 minutes
maximum source-sync age. Historical `RELEASED` backfill starts later with small
slices and a separately approved daily page/time budget; it is never the first
production job.

A local 1,000-row synthetic `DEFERRED` canary grew SQLite by 1,912,832 bytes
(about 1.9 KiB per unique fingerprint); repeating the identical canary added zero
bytes. Actual production growth depends on payload breakdown depth, so the canary
must measure its own before/after database size rather than treating this estimate
as an acceptance guarantee.
