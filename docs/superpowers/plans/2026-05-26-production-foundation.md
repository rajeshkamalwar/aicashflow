# Production Foundation Plan

## Goal

Upgrade the Phase 0 FastAPI app from a local prototype into a deployable production foundation while preserving the current localhost workflow.

## Scope

- Add environment-driven operational configuration.
- Add optional API key enforcement for protected API routes.
- Add durable SQLite storage for uploads, report runs, and audit events.
- Harden uploads with category, extension, and size validation.
- Add readiness metadata for deployment checks.
- Update the runbook with production startup and security notes.

## Tasks

1. Add tests for auth behavior, upload validation, SQLite persistence, audit logging, and readiness.
2. Implement `AppConfig` production settings from environment variables.
3. Create a small SQLite operational store.
4. Route upload and run history through the operational store.
5. Add FastAPI auth dependency and readiness endpoint.
6. Update runbook and run the full test suite.
