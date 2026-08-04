# Spec: AI Cashflow Product UI Consolidation

Status: superseded on 2026-07-15 by the BRD-aligned source-aware redesign below. Source implementation and visual QA are in scope; production deployment remains a separate follow-up phase.

The product keeps four task-based pages: Overview (group treasury plus reports/audit), Payouts (reconciliation, exceptions, and data quality), Sources (one independently managed record per connector/account), and Admin Settings.

## Approved BRD Alignment Delta

The July 2026 correction explicitly approves carrying Amazon `source_id` from the managed report path into normalized payout and transaction rows. This removes the former blocker that prevented truthful account-level filtering.

- Every API-sourced payout and transaction retains its Amazon source ID through generated reconciliation outputs.
- Overview and Payouts expose real source, currency, and reporting-period filters. All three filters must change rows and totals together.
- “All sources” never adds different currencies into one figure. It renders one total per currency.
- The header never claims that a UI poll is a source sync. Freshness comes from the source registry's actual `last_sync_at` value.
- Automatic sync state, last successful source sync, and UI refresh state remain distinct.
- Bank cash, reserves/holds, committed outflows, and funding gap display “Unavailable” when the required BRD input is missing; zero is reserved for a real source-backed zero.
- Entity labels are not inferred from account names. Until a source-to-entity mapping exists, the scope is described as group/source scope rather than a selected legal entity.
- Phase 0 remains a single-super-admin product. AI predictions and recommendations stay hidden until the BRD data-foundation gate is met.
- Amazon settlement reports are scheduled/report-based, not streaming. “Current” means the latest successful API sync is within the configured source interval; otherwise the UI says delayed, stale, disabled, or never synced.

### BRD dashboard coverage in the four-page shell

- Group Treasury and entity/source cash: Overview scope and KPI row.
- Marketplace Payout: Payouts workspace plus recent activity on Overview.
- Bank Balance: Overview confirmed-cash card, explicitly unavailable without a bank feed.
- Reserve/Hold: Overview readiness card, explicitly unavailable without a reserve ledger.
- Funding Gap: Overview readiness card, calculated only when confirmed cash and committed outflows exist.
- Exception Dashboard: action queue within Payouts.
- Source reliability: Sources workspace with credentials, authorization, automatic sync, last data sync, freshness, and history shown separately.

## Objective

Redesign the application around one unambiguous cashflow dashboard and a small number of task-based workspaces. The primary user is the authenticated super admin acting as finance operator during the current rollout. The interface must make a clear distinction between financial results, operational exceptions, source connectivity, and system configuration.

The redesign must remove the impression that every navigation item is another dashboard, eliminate duplicated source management, and stop presenting selectors that do not actually filter financial data.

## Current-State Findings

- The shell exposes 10 views, 32 panels, and 37 render functions in one HTML/JavaScript surface.
- `Overview`, `Payout Review`, `Needs Attention`, `Forecast`, and `Deferred / Reserved` compete as dashboard-like financial summaries.
- `Marketplaces`, `Import Status`, `Import Checks`, and the Amazon section in `Settings` split one source-operations workflow across four locations.
- `Reports` repeats information already available in live views.
- The marketplace selector changes labels and context but does not filter the loaded financial records.
- The treasury horizon selector changes explanatory copy but does not recalculate a forecast.
- Source status terms mix credential presence, authorization testing, synchronization, and data freshness.
- The imported settlement cache is namespaced by Amazon source, but normalized report rows do not preserve `source_id`; truthful account-level financial filtering is therefore not yet supported.
- The frontend is concentrated in approximately 710 lines of HTML, 3,454 lines of CSS, and 3,127 lines of JavaScript, making style and behavior drift likely.

## Product Model

The application has four distinct concepts:

1. **Cash position** — financial totals and movement for a selected reporting period.
2. **Payout operations** — settlements, expected receipts, matches, and exceptions.
3. **Source operations** — Amazon authorizations, connection health, synchronization, and import quality.
4. **Administration** — workspace identity, currencies, bank sources, forecast assumptions, alerts, users, and governance.

These concepts must not be mixed in the same card or status label.

## Proposed Navigation

### 1. Overview

The only page called or presented as a dashboard.

- Cash position
- Expected versus received payouts
- Current exceptions requiring action
- Data freshness and coverage banner
- Compact 7/30/60-day outlook, shown only when calculated data exists

### 2. Payouts

One operational workspace replacing `Payout Review` and the detailed settlement portions of `Overview`.

- All settlements
- Matched payouts
- Expected/pending payouts
- Bank/payment receipts
- Transaction detail drawer

### 3. Exceptions

One action queue replacing overlapping `Needs Attention` and scattered error lists.

- Delayed or missing payouts
- Unmatched receipts
- Data-quality exceptions
- Severity, status, owner, and next action

### 4. Sources

One source workspace replacing the legacy marketplace editor, file-import status, and Amazon connection management inside general Settings.

- Amazon source registry
- Connection and authorization state
- Last successful data sync and report count
- Sync history
- Selected-account credentials, authorization, sync result, and data freshness

### 5. Reports

Purpose-built exports and audit history only. It must not duplicate live navigation pages.

- Cash position export
- Payout reconciliation export
- Exception export
- Calculation/audit evidence
- Refresh history

### Administration

Accessed from a clearly secondary `Admin Settings` entry, not presented as a finance workspace.

- Workspace identity and branding
- Currencies and reconciliation tolerances
- Forecast assumptions and horizons
- Alert rules
- Users and roles
- Governance
- Bank/payment configuration where credentials are not source-specific

## Filter Model

### Global filters

Shown once beneath the page title and persisted in the URL when supported:

- Reporting period: preset or start/end date
- Currency
- Amazon account/source

### Contextual filters

- Payouts: settlement status, transaction type, search/reference
- Exceptions: severity, workflow status, owner
- Sources: region, connection state, enabled state
- Reports: report type and period

### Filter rules

- A filter must change the dataset, totals, and empty state together.
- A selector that only changes a label is not a filter and must not look like one.
- Account filtering must be omitted until normalized rows preserve `source_id`; an unavailable control adds no operational value.
- Forecast horizons must not appear interactive until changing the horizon recalculates or selects real forecast data.
- Filter state must survive refresh through URL parameters once backend filtering exists.

## Status Vocabulary

Source state is represented by separate fields rather than one overloaded badge:

- **Credentials:** Missing / Stored
- **Authorization:** Not tested / Authorized / Denied
- **Automatic sync:** Off / On
- **Data sync:** Never / Successful / Failed
- **Freshness:** Current / Delayed / Stale

Financial records use:

- Expected
- Received
- Matched
- Delayed
- Exception

Severity uses:

- Critical
- High
- Medium
- Low

Colors support the text but never replace it.

## Layout and Design System

- Reuse the existing dark navigation and teal product accent.
- Use one page header, one filter bar, one KPI pattern, one table/list pattern, and one status-badge system.
- Reduce panel nesting and avoid equal-weight card grids for unrelated information.
- Prefer dense operational tables/lists for payouts, exceptions, and sources.
- Reserve large KPI cards for the Overview page.
- Use a consistent 4/8/12/16/24/32 spacing scale.
- Keep desktop navigation fixed and collapse it into an accessible mobile menu.
- Provide loading, empty, error, partial-data, and stale-data states for every workspace.

## Tech Stack

- FastAPI serves the application shell and JSON APIs.
- The frontend remains native HTML, CSS, and JavaScript for this consolidation.
- SQLite remains the operational store.
- No new frontend framework or dependency is introduced.

## Commands

- JavaScript syntax: `node --check src/ai_cashflow/web/app.js`
- Targeted UI/API test: `.venv\\Scripts\\python.exe -m unittest tests.test_api_app.ApiAppTests.test_app_shell_endpoint_returns_html`
- Full test suite when approved for release: `.venv\\Scripts\\python.exe -m unittest discover -s tests`

## Project Structure

- `src/ai_cashflow/web/app.html` — application shell and semantic view structure
- `src/ai_cashflow/web/app.css` — design tokens, layout, components, and responsive rules
- `src/ai_cashflow/web/app.js` — state loading, navigation, filtering, and rendering
- `src/ai_cashflow/api/` — API routes and response services
- `src/ai_cashflow/integrations.py` — encrypted source registry and sync history
- `src/ai_cashflow/reporting.py` — normalization, reconciliation, and report generation
- `tests/` — targeted unit and API regression coverage
- `docs/PRODUCT_UI_REDESIGN_SPEC.md` — approved product/UI contract

## Code Style

Reuse small rendering functions and semantic classes instead of view-specific inline styling:

```javascript
function renderStatus(label, value, tone = "neutral") {
  return `<div class="status-field status-${escapeAttribute(tone)}">
    <span>${escapeHtml(label)}</span>
    <strong>${escapeHtml(value)}</strong>
  </div>`;
}
```

- DOM IDs describe purpose, not visual position.
- Statuses are derived from server fields, not inferred from badge color.
- Shared layout and component classes are defined once.
- No secret values enter DOM state, logs, URLs, or browser storage.

## Testing Strategy

- Static shell tests verify the reduced navigation and removal of duplicate workspaces.
- Unit tests cover each real filter function with account, date, currency, and empty-data cases.
- API tests verify source status fields and masked credentials.
- Regression tests ensure imported source counts and synchronization history remain visible.
- Visual QA, when explicitly requested, covers approximately 1440px desktop and 390px mobile.

## Boundaries

### Always

- Preserve encrypted credential handling and authenticated admin routes.
- Keep financial and operational statuses explicit.
- Preserve existing API data and report history during the UI consolidation.
- Use source-backed totals and label incomplete data honestly.

### Ask first

- Database changes needed to add `source_id` to normalized financial records.
- New authentication roles or separate finance/admin accounts.
- Removing historical report files or tenant configuration data.
- Deployment and production data migration.

### Never

- Display or return Amazon credentials.
- Present a decorative selector as a working filter.
- Show sample/demo figures as live financial data.
- Reintroduce CSV/Excel upload controls for Amazon data.
- Create multiple pages that each claim to be the primary dashboard.

## Implementation Slices

1. Consolidate navigation and semantic page shells without changing APIs.
2. Move source operations into the Sources workspace and general configuration into Admin Settings.
3. Normalize shared headers, filters, KPI blocks, lists, tables, and status fields.
4. Merge payout views and exception views onto the reduced navigation.
5. Remove decorative filter behavior and label unsupported account filtering honestly.
6. Add source-aware backend tagging and real global filters only after separate approval.

## Success Criteria

- Exactly one primary dashboard exists: Overview.
- Primary navigation contains five finance/operations workspaces or fewer.
- Amazon management appears in one place only.
- SP-API connection health appears in Sources; financial data-quality issues appear in Exceptions.
- Payout review and related settlement lists appear in one Payouts workspace.
- All actionable issues appear in one Exceptions workspace.
- No visible filter claims to filter financial data unless it changes the underlying records and totals.
- Settings are clearly administrative and visually secondary.
- Source status distinguishes credentials, authorization, sync enablement, sync result, and freshness.
- Existing secrets, imported reports, and audit history remain unchanged.

## Open Questions

1. Should the application remain one super-admin experience, or should CFO/finance users receive a separate read-only login later?
2. Should `Forecast` remain a compact section inside Overview, or become a sixth workspace after real forecast data is available?
3. Which account dimension should drive future filtering: Amazon authorization source, Seller ID, merchant account, marketplace, or a client-defined business account?
4. What reporting timezone should date filters use across regions?
