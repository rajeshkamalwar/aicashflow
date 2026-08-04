# Phase 2A.1 transaction collection

Amazon `listTransactions` returns at most 500 rows per page, supplies a `nextToken`,
requires `postedBefore` to be later than `postedAfter` and at least two minutes old,
and returns no data for windows wider than 180 days. The collector follows that
contract and records Amazon's rate-limit header. See the official
[`listTransactions` reference](https://developer-docs.amazon.com/sp-api/lang-en_US/reference/listtransactions).

## Collection model

- Open FinancialEventGroup synchronization remains independent and keeps its
  five-minute cadence.
- Incremental collection starts with a 24-hour window. Later runs begin at the
  last successful coverage end minus a six-hour overlap and end two minutes
  before the request time.
- A page budget and wall-clock budget bound each scheduler invocation. An
  unfinished run retains its immutable window and `nextToken` for the next
  invocation.
- Historical backfill is created and advanced only through administrator
  endpoints. A job divides its range into configurable slices (one day by
  default), persists every page, and can pause, fail, resume, or survive a
  process restart.
- No page cap truncates a slice. Reaching the per-invocation page budget yields;
  the next run resumes from the saved token.

## Publication and status rules

Pages are written to `amazon_transaction_staging`. State, observation history,
the successful checkpoint, and run completion are committed together only after
Amazon returns no `nextToken`. Failed or yielded staging rows are never visible.

Status precedence is:

1. `DEFERRED`
2. `RELEASED`
3. `DEFERRED_RELEASED`

Progression advances current state. A later lower-ranked observation is retained
as audit evidence, marks the current row conflicted, and is excluded from visible
transaction totals until reviewed.

## Storage and retention

The normalized financial fingerprint excludes retrieval time. Repeating the same
transaction 100 times therefore leaves one observation and one current-state row.
The complete Amazon payload is compressed and stored once per unique fingerprint;
state and history contain only normalized financially relevant JSON.

Conservative SQLite capacity estimates (including state, history, payload, and
indexes) are:

| Unique transaction versions | Estimated storage |
| ---: | ---: |
| 10,000 | 30-70 MB |
| 100,000 | 300-700 MB |
| 1,000,000 | 3-7 GB |

Actual growth depends mainly on breakdown depth and how often a transaction's
financially relevant fingerprint changes. `AI_CASHFLOW_TRANSACTION_RETENTION_DAYS`
defaults to `0`, meaning unlimited audit retention. No automatic deletion is
enabled; a non-zero retention policy must not be activated until finance approves
the policy and maintenance procedure.

## Failed Phase 2A cleanup plan (not automatic)

1. Stop collection and create an online SQLite backup plus file checksums.
2. Identify the failed run by its deployment time and incomplete checkpoint;
   compare its `run_id`, retrieval timestamps, and source/marketplace/status scope
   with the pre-deployment backup.
3. Prove that candidate rows do not predate the failed deployment and are not
   referenced by a completed run or successful checkpoint.
4. In one transaction, delete only confirmed failed-run staging, state,
   observation, checkpoint, and payload rows. Preserve all pre-existing rows.
5. Run `PRAGMA foreign_key_check` and `PRAGMA integrity_check`, then verify row
   counts, `/health`, `/phase0/marketplace-ar`, and `/phase0/api-status`.
6. Do not expect row deletion to shrink the file. Schedule `VACUUM INTO` and an
   atomic database replacement only in an approved maintenance window.
7. Roll back by stopping the service, atomically restoring the verified database
   backup, restoring ownership and permissions, restarting, and repeating the
   integrity and endpoint checks.
