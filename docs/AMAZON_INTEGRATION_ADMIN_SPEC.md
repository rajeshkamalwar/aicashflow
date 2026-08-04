# Spec: Super-Admin Amazon SP-API Integration Management

## Objective

Provide one authenticated super admin with a professional Settings workflow for configuring, testing, synchronizing, rotating, and auditing the Amazon SP-API integration without editing deployment files. Marketplace file uploads remain disabled.

## Architecture

- FastAPI and the existing SQLite operational database remain the application platform.
- Nginx Basic Authentication protects the single-admin application at the edge.
- Amazon configuration and encrypted credentials live in SQLite.
- A single Fernet master key remains deployment-managed as the root of trust.
- Admin APIs return configuration metadata and boolean secret-presence flags only.
- Amazon settlement reports use `GET_V2_SETTLEMENT_REPORT_DATA_FLAT_FILE_V2`.
- Synchronization downloads Amazon-generated report documents, records normalized rows and sync history in SQLite, and refreshes a server-managed reporting cache. Users never upload CSV/Excel files.

## Admin API Contract

- `GET /admin/integrations/amazon` returns masked configuration/status.
- `PUT /admin/integrations/amazon` validates and saves metadata plus any supplied secrets; blank secret fields preserve existing encrypted values.
- `POST /admin/integrations/amazon/test` exchanges the refresh token and lists completed reports.
- `POST /admin/integrations/amazon/sync` downloads and ingests new completed reports idempotently.
- `GET /admin/integrations/amazon/syncs` returns recent sync history without secrets.
- `DELETE /admin/integrations/amazon/credentials` removes encrypted credentials and disables the integration.

## Security Boundaries

- Always: TLS, edge authentication, endpoint allowlist, encrypted secrets, parameterized SQL, masked responses, audit events, bounded downloads, timeouts, idempotency.
- Never: return secrets, log secrets, store tokens in tenant JSON/browser storage/source code, accept arbitrary endpoints, or expose integration mutation endpoints without authentication.
- The application can read the master key but cannot display or rotate it through the UI.

## Testing Strategy

- Unit tests: encryption, masked reads, blank-secret preservation, endpoint validation, report parsing, idempotent ingestion.
- API tests: configuration CRUD, connection-test error handling, sync history, and no secret disclosure.
- Existing full suite remains green.
- Production verification checks protected access, masked Settings data, service health, and API status.

## Success Criteria

- Super admin can manage all Amazon operational settings from Settings.
- Client ID, client secret, and refresh token are encrypted at rest and never returned.
- Test Connection and Sync Now provide safe, actionable statuses.
- Sync history and audit records identify success/failure without secrets.
- Marketplace upload UI and POST upload endpoint remain absent.
- Existing server credentials are migrated into encrypted storage.
- The live service is active and the public application is authentication-protected.

## Known External Blocker

Amazon connection testing and data synchronization cannot succeed until a valid refresh token is entered by the super admin.
