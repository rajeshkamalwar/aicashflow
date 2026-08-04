# Phase 0 Hybrid Start Implementation Plan

## Goal

Create the first working package for the AI-Based Daily Cash Flow Consolidation & Forecasting System. This package should let the team start Phase 0 immediately while keeping the technical work lightweight until sample marketplace and bank data is available.

## Files To Create

- `phase0/README.md`: Phase 0 operating overview and decision gate.
- `phase0/data-request-checklist.md`: CFO, finance, IT, and consultant data needs.
- `phase0/poc-scope.md`: recommended POC scope and sample selection rules.
- `phase0/success-criteria.md`: pass/fail criteria for approving Phase 1.
- `phase0/backlog.md`: first execution backlog.
- `README.md`: repository orientation.
- `pyproject.toml`: minimal Python project metadata.
- `src/ai_cashflow/__init__.py`: package exports.
- `src/ai_cashflow/models.py`: normalized data models for Phase 0.
- `src/ai_cashflow/reconciliation.py`: simple deterministic reconciliation helpers.
- `src/ai_cashflow/ingestion/README.md`: ingestion boundary notes.
- `data/samples/README.md`: where sanitized source samples belong.
- `tests/test_reconciliation.py`: unit coverage for the starter reconciliation logic.

## Tasks

1. Add Phase 0 planning documents.
2. Add the Python project scaffold.
3. Add deterministic reconciliation models and tests.
4. Verify all required files exist.
5. Run unit tests using the bundled Python runtime.

## Verification

- `python -m unittest discover -s tests`
- Confirm Phase 0 docs mention the POC decision gate and source sample needs.
- Confirm reconciliation tests cover exact matches, unmatched expected payouts, and unmatched bank receipts.
