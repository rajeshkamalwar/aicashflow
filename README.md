# AI Cashflow

Phase 0 workspace for the AI-Based Daily Cash Flow Consolidation & Forecasting System.

The approved BRD is the source document for the program. The working approach is:

1. Prove data access and reconciliation in Phase 0.
2. Build the data foundation and reconciliation engine.
3. Add dashboards and CFO reporting.
4. Add forecasting and alerts.
5. Add AI intelligence only after the source data is reliable.

Start with the Phase 0 package in [`phase0/README.md`](phase0/README.md).

## Amazon transaction visibility

`AI_CASHFLOW_TRANSACTION_VISIBILITY_ENABLED` defaults to `false`. When enabled,
the dashboard reads transaction snapshots persisted by the normal source sync;
it does not call Amazon while rendering. Observations are append-only while one
canonical current row is retained per source, marketplace, and transaction ID.
Observation storage grows when Amazon returns changed records. Review database
growth before long-term enablement and add retention only after the required
audit-retention period is agreed.
