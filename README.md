# AI Cashflow

Phase 0 workspace for the AI-Based Daily Cash Flow Consolidation & Forecasting System.

The approved BRD is the source document for the program. The working approach is:

1. Prove data access and reconciliation in Phase 0.
2. Build the data foundation and reconciliation engine.
3. Add dashboards and CFO reporting.
4. Add forecasting and alerts.
5. Add AI intelligence only after the source data is reliable.

Start with the Phase 0 package in [`phase0/README.md`](phase0/README.md).

## Amazon transaction collection and visibility

Both `AI_CASHFLOW_TRANSACTION_COLLECTION_ENABLED` and
`AI_CASHFLOW_TRANSACTION_VISIBILITY_ENABLED` default to `false`. Collection
runs only during source synchronization. Visibility only reads persisted
snapshots and never calls Amazon while rendering. Production visibility
requires collection to be enabled. Observations are append-only while one
canonical current row is retained per source, marketplace, and transaction ID.
Observation storage grows when Amazon returns changed records. Review database
growth before long-term enablement and add retention only after the required
audit-retention period is agreed.
