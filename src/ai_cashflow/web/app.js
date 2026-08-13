const views = {
  overview: "Overview",
  "marketplace-ar": "Marketplace AR",
  payouts: "Payouts",
  sources: "Sources",
  admin: "Admin Settings",
};

const viewAliases = {
  settings: "admin",
  reconciliation: "payouts",
  "source-health": "sources",
  exceptions: "payouts",
  "data-quality": "payouts",
  reports: "overview",
  forecast: "overview",
  reserves: "overview",
};

const state = {
  session: null,
  tenant: null,
  summary: null,
  exceptions: {},
  matched: [],
  unmatchedExpected: [],
  unmatchedReceipts: [],
  dataQuality: [],
  uploads: [],
  runs: [],
  marketplaceActivity: [],
  marketplaceActivityStatus: null,
  marketplaceAr: null,
  sourceEvidence: [],
  inputReadiness: null,
  calculationAudit: null,
  cashflowProof: null,
  apiStatus: null,
  amazonSources: null,
  selectedAmazonSourceId: "",
  amazonSyncs: [],
  amazonFinancialPosition: null,
  amazonFinancialPositions: {},
  amazonFinancialSnapshotIds: {},
  amazonTransactionDiagnostics: null,
  sellerStatement: null,
  lastSyncAt: null,
  filters: {
    sourceId: new URLSearchParams(location.search).get("source") || "",
    currency: new URLSearchParams(location.search).get("currency") || "",
    period: new URLSearchParams(location.search).get("period") || "all",
  },
};

// ---------------------------------------------------------------------------
// API auto-polling
// ---------------------------------------------------------------------------
let _pollInterval = null;
const POLL_INTERVAL_MS = 30_000;
let _isBackgroundSyncing = false;
let _selectedSourceSyncing = false;
const _financialPositionRequests = new Map();

function configureStatementSnapshotForm() {
  const form = document.getElementById("seller-central-snapshot-form");
  if (!form) return;
  const source = document.getElementById("statement-source");
  const marketplace = document.getElementById("statement-marketplace");
  const currency = document.getElementById("statement-currency");
  const populate = () => {
    const selected = (state.amazonSources || []).find((row) => row.id === source.value);
    marketplace.innerHTML = (selected?.marketplaces || []).map((row) => `<option value="${escapeAttribute(row.id)}" data-currency="${escapeAttribute(row.currency || "")}">${escapeHtml(row.name || row.id)}</option>`).join("");
    currency.value = marketplace.selectedOptions[0]?.dataset.currency || "";
  };
  source.innerHTML = (state.amazonSources || []).map((row) => `<option value="${escapeAttribute(row.id)}">${escapeHtml(row.name)}</option>`).join("");
  source.onchange = populate;
  marketplace.onchange = () => { currency.value = marketplace.selectedOptions[0]?.dataset.currency || ""; };
  populate();
  form.onsubmit = async (event) => {
    event.preventDefault();
    const body = Object.fromEntries(new FormData(form));
    for (const key of ["standard_orders", "deferred_transactions", "all_accounts", "funds_available", "account_level_reserve", "recent_payout", "notes"]) if (!body[key]) body[key] = null;
    body.currency = String(body.currency).toUpperCase(); body.observed_at = new Date(body.observed_at).toISOString();
    const status = document.getElementById("seller-central-snapshot-status");
    try {
      const saved = await fetchJson("/admin/seller-central/statement-snapshots", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      status.textContent = `Snapshot saved · ${saved.currency} · observed ${String(saved.observed_at).slice(0, 19).replace("T", " ")} UTC`;
      await loadAmazonFinancialPosition(state.filters.sourceId); render();
    } catch (error) { status.textContent = error.message || "Snapshot was not saved."; }
  };
}

function hasApiSources() {
  return (state.amazonSources || []).some((source) => source.enabled);
}

function sourceSyncFingerprint(sources = state.amazonSources || []) {
  return sources.map((source) => [source.id, source.status, source.last_sync_at, source.last_error]).sort().join("|");
}

async function syncFromApi() {
  const status = document.getElementById("api-status");
  if (_isBackgroundSyncing) return;
  _isBackgroundSyncing = true;
  if (status) {
    status.textContent = "Syncing…";
    status.className = "status partial";
  }
  try {
    const previousSourceFingerprint = sourceSyncFingerprint();
    const sources = await fetchJson("/phase0/sources");
    const sourceChanged = previousSourceFingerprint !== sourceSyncFingerprint(sources);
    state.amazonSources = sources;
    if (sourceChanged) await loadData({ background: true });
    else {
      renderScopeControls();
      updateLiveIndicator();
    }
    if (sourceChanged) {
      showLiveToast("New data received — view updated.");
    }
  } catch {
    if (status) {
      status.textContent = "Sync failed";
      status.className = "status offline";
    }
  } finally {
    _isBackgroundSyncing = false;
  }
}

function startApiPolling() {
  if (_authenticationStopped || _pollInterval || !hasApiSources()) return;
  _pollInterval = setInterval(syncFromApi, POLL_INTERVAL_MS);
  updateLiveIndicator();
}

function stopApiPolling() {
  clearInterval(_pollInterval);
  _pollInterval = null;
  updateLiveIndicator();
}

function updateLiveIndicator() {
  const badge = document.getElementById("live-badge");
  if (!badge) return;
  const sources = filteredSources();
  const latest = sources.map((source) => source.last_sync_at).filter(Boolean).sort().at(-1);
  const enabled = sources.some((source) => source.enabled);
  badge.textContent = latest
    ? `${enabled ? "Source monitoring on" : "Snapshot"} · ${formatIntegrationTime(latest)}`
    : sources.length ? "No successful source sync" : "No source configured";
  badge.className = `live-badge ${enabled && latest ? "live" : "idle"}`;
}

function dashboardFingerprint() {
  if (!state.summary) return "";
  return JSON.stringify({
    summary: state.summary,
    exceptions: state.exceptions,
    matched: state.matched?.length || 0,
    unmatchedExpected: state.unmatchedExpected?.length || 0,
    unmatchedReceipts: state.unmatchedReceipts?.length || 0,
    uploads: state.uploads?.length || 0,
    runs: state.runs?.length || 0,
    marketplaceActivity: state.marketplaceActivity?.length || 0,
    sourceEvidence: state.sourceEvidence?.length || 0,
    sellerStatement: state.sellerStatement?.scraped_at || "",
    inputReadiness: state.inputReadiness?.status || "",
    calculationAudit: state.calculationAudit?.status || "",
  });
}

function showLiveToast(message) {
  let toast = document.getElementById("live-toast");
  if (!toast) {
    toast = document.createElement("div");
    toast.id = "live-toast";
    toast.className = "live-toast";
    document.body.appendChild(toast);
  }
  toast.textContent = message;
  toast.classList.add("visible");
  clearTimeout(toast._timeout);
  toast._timeout = setTimeout(() => toast.classList.remove("visible"), 4000);
}

document.querySelectorAll(".nav-item").forEach((button) => {
  button.addEventListener("click", () => {
    showView(button.dataset.view);
    history.replaceState(null, "", `#${button.dataset.view}`);
    closeMobileNavigation();
  });
});

document.getElementById("mobile-nav-toggle").addEventListener("click", () => {
  const sidebar = document.getElementById("app-sidebar") || document.querySelector(".sidebar");
  const toggle = document.getElementById("mobile-nav-toggle");
  const open = sidebar.classList.toggle("mobile-open");
  toggle.setAttribute("aria-expanded", String(open));
  toggle.setAttribute("aria-label", open ? "Close navigation" : "Open navigation");
});

for (const [id, key] of [
  ["global-source-filter", "sourceId"],
  ["global-currency-filter", "currency"],
  ["global-period-filter", "period"],
]) {
  document.getElementById(id).addEventListener("change", async (event) => {
    state.filters[key] = event.target.value;
    if (key === "sourceId") {
      state.filters.currency = "";
      state.amazonFinancialPosition = null;
      render();
      await loadAmazonFinancialPosition();
    } else if (key === "currency") {
      state.amazonFinancialPosition = null;
      render();
      await loadAmazonFinancialPosition();
    }
    persistScopeFilters();
    render();
  });
}

document.getElementById("sync-selected-source").addEventListener("click", syncSelectedSource);

document.getElementById("run-report").addEventListener("click", async () => {
  const btn = document.getElementById("run-report");
  const status = document.getElementById("api-status");
  btn.disabled = true;
  btn.textContent = "Refreshing...";
  status.textContent = "Refreshing cash view";
  try {
    await apiFetch("/phase0/run-report", { method: "POST" });
    await loadData();
  } finally {
    btn.disabled = false;
    btn.textContent = "Recalculate View";
  }
});

document.getElementById("tenant-settings-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  await saveTenantSettings();
});

document.getElementById("amazon-integration-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  await saveAmazonIntegration();
});
document.getElementById("amazon-test-connection").addEventListener("click", () => runAmazonAction("test"));
document.getElementById("amazon-sync-now").addEventListener("click", () => runAmazonAction("sync"));
document.getElementById("amazon-remove-credentials").addEventListener("click", removeAmazonCredentials);
document.getElementById("amazon-add-source").addEventListener("click", () => {
  state.selectedAmazonSourceId = "";
  state.amazonSyncs = [];
  renderAmazonIntegration();
  document.getElementById("amazon-source-name").focus();
});
document.getElementById("amazon-source-select").addEventListener("change", async (event) => {
  state.selectedAmazonSourceId = event.target.value;
  await loadSelectedAmazonSource();
  renderAmazonIntegration();
});

// "Review Actions" button → go to Needs Attention
document.getElementById("review-actions-btn").addEventListener("click", () => {
  navigateTo("payouts");
});

document.getElementById("clear-runs-btn").addEventListener("click", async () => {
  const count = state.runs.length;
  if (!count) return;
  if (!confirm(`Clear all ${count} run${count !== 1 ? "s" : ""} from the refresh history?\n\nThis only removes the history log — uploaded files and reports are not affected.`)) return;
  const btn = document.getElementById("clear-runs-btn");
  btn.disabled = true;
  btn.textContent = "Clearing…";
  try {
    const res = await apiFetch("/phase0/runs", { method: "DELETE" });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      alert(err.detail || "Clear failed");
      return;
    }
    state.runs = [];
    renderRuns();
    setText("runs-count", "0");
  } catch {
    alert("Clear failed — check connection.");
  } finally {
    btn.disabled = false;
    btn.textContent = "Clear history";
  }
});

document.getElementById("add-bank-source")?.addEventListener("click", () => {
  addSourceRow("bank-sources-editor", {
    name: "",
    bank_name: "",
    account_reference: "",
    currency: "",
    connection_type: "",
    status: "Configured",
    owner: "",
    frequency: "Daily",
    api_endpoint: "",
    credential_reference: "",
  });
});

document.getElementById("add-entity").addEventListener("click", () => {
  addSetupCard("entities-editor", "entity", { name: "", currency: "", status: "" });
});

document.getElementById("add-outflow").addEventListener("click", () => {
  addSetupCard("outflows-editor", "outflow", {
    name: "",
    category: "",
    amount: "",
    frequency: "",
  });
});

document.getElementById("add-horizon").addEventListener("click", () => {
  addSetupCard("horizons-editor", "horizon", { value: "" });
});

document.getElementById("add-alert").addEventListener("click", () => {
  addSetupCard("alerts-editor", "alert", { name: "", trigger: "", recipient: "" });
});

document.getElementById("add-role").addEventListener("click", () => {
  addSetupCard("roles-editor", "role", { name: "", role: "", scope: "" });
});

function showView(viewId) {
  const requestedViewId = viewAliases[viewId] || viewId;
  const resolvedViewId = views[requestedViewId] ? requestedViewId : "overview";
  document.querySelectorAll(".nav-item").forEach((button) => {
    button.classList.toggle("active", button.dataset.view === resolvedViewId);
  });
  document.querySelectorAll(".view").forEach((view) => {
    view.classList.toggle("active", view.id === resolvedViewId);
  });
  document.getElementById("view-title").textContent = views[resolvedViewId];
  document.getElementById("global-scope-bar").hidden = !["overview", "payouts"].includes(resolvedViewId);
}

function closeMobileNavigation() {
  const sidebar = document.getElementById("app-sidebar") || document.querySelector(".sidebar");
  const toggle = document.getElementById("mobile-nav-toggle");
  sidebar.classList.remove("mobile-open");
  toggle.setAttribute("aria-expanded", "false");
  toggle.setAttribute("aria-label", "Open navigation");
}

function persistScopeFilters() {
  const url = new URL(location.href);
  for (const [param, value] of [
    ["source", state.filters.sourceId],
    ["currency", state.filters.currency],
    ["period", state.filters.period === "all" ? "" : state.filters.period],
  ]) {
    if (value) url.searchParams.set(param, value);
    else url.searchParams.delete(param);
  }
  history.replaceState(null, "", `${url.pathname}${url.search}${location.hash}`);
}

async function syncSelectedSource() {
  const sourceId = state.filters.sourceId;
  if (!sourceId) return;
  const button = document.getElementById("sync-selected-source");
  _selectedSourceSyncing = true;
  button.disabled = true;
  button.textContent = "Syncing source…";
  try {
    await fetchJson(`/admin/integrations/amazon/sources/${encodeURIComponent(sourceId)}/sync`, { method: "POST" });
    await loadAmazonFinancialPosition(sourceId);
    render();
    showLiveToast("Source sync completed with current Amazon data.");
    loadData({ background: true }).catch((error) => showDashboardError("Background refresh", error));
  } catch (error) {
    showLiveToast(error.message || "Source sync failed.");
    showDashboardError("Source synchronization", error);
  } finally {
    _selectedSourceSyncing = false;
    const source = (state.amazonSources || []).find((candidate) => candidate.id === sourceId);
    button.disabled = Boolean(source && !source.credentials?.refresh_token_configured);
    button.textContent = "Sync selected source";
  }
}

async function loadAmazonFinancialPosition(sourceId = state.filters.sourceId) {
  if (!sourceId) return null;
  const requestId = (_financialPositionRequests.get(sourceId) || 0) + 1;
  _financialPositionRequests.set(sourceId, requestId);
  const currency = sourceId === state.filters.sourceId ? state.filters.currency : "";
  let position;
  try {
    const snapshotId = state.amazonFinancialSnapshotIds[sourceId] || "";
    position = await fetchJson(
      `/phase0/amazon/financial-position?source_id=${encodeURIComponent(sourceId)}${currency ? `&currency=${encodeURIComponent(currency)}` : ""}${snapshotId ? `&snapshot_id=${encodeURIComponent(snapshotId)}` : ""}`
    );
    if (currency && position?.marketplace_id) {
      const manual = await fetchJson(`/phase0/seller-central/statement-snapshots/latest?source_id=${encodeURIComponent(sourceId)}&marketplace_id=${encodeURIComponent(position.marketplace_id)}&currency=${encodeURIComponent(currency)}`);
      position.seller_central_statement_snapshot = manual.snapshot || null;
    }
  } catch (error) {
    position = {
      status: "unavailable",
      source_id: sourceId,
      message: error.message,
    };
  }
  const isLatestRequest = _financialPositionRequests.get(sourceId) === requestId;
  const isCurrentSelection = sourceId === state.filters.sourceId && currency === state.filters.currency;
  if (position?.snapshot_id) state.amazonFinancialSnapshotIds[sourceId] = position.snapshot_id;
  if (isLatestRequest) state.amazonFinancialPositions[sourceId] = position;
  if (isCurrentSelection) state.amazonFinancialPosition = position;
  return position;
}

async function loadAmazonFinancialPositions() {
  state.amazonFinancialPosition = null;
  state.amazonFinancialPositions = {};
  await Promise.all((state.amazonSources || [])
    .filter((source) => source.enabled && source.status === "Connected")
    .map((source) => loadAmazonFinancialPosition(source.id)));
}

window.addEventListener("hashchange", () => {
  showView(location.hash.slice(1));
});

async function loadData(options = {}) {
  const status = document.getElementById("api-status");
  const isBackground = Boolean(options.background);
  try {
    const session = await fetchJson("/auth/session");
    state.session = session;
    document.querySelector('[data-view="admin"]')?.toggleAttribute("hidden", !session.is_admin);
    const [health, tenant, summary, exceptions, quality, matched, expected, receipts, uploads, runs, marketplaceActivity, marketplaceAr, sourceEvidence, inputReadiness, calculationAudit, cashflowProof, apiStatus, sellerStatement, amazonSources] =
      await Promise.all([
        fetchJson("/health"),
        fetchJson("/tenant/settings"),
        fetchJson("/phase0/summary"),
        fetchJson("/phase0/exceptions"),
        fetchJson("/phase0/data-quality"),
        fetchJson("/phase0/matched-payouts"),
        fetchJson("/phase0/unmatched-expected"),
        fetchJson("/phase0/unmatched-receipts"),
        fetchJson("/phase0/uploads"),
        fetchJson("/phase0/runs"),
        fetchJson("/phase0/marketplace-activity").catch((error) => ({
          status: "unavailable", rows: [], diagnostics: { validation_reason: error.message },
        })),
        fetchJson("/phase0/marketplace-ar").catch((error) => ({ status: "unavailable", message: error.message })),
        fetchJson("/phase0/source-evidence"),
        fetchJson("/phase0/input-readiness"),
        fetchJson("/phase0/calculation-audit"),
        fetchJson("/phase0/reconciliation/proof").catch(() => null),
        session.is_admin ? fetchJson("/phase0/api-status").catch(() => null) : null,
        session.is_admin ? fetchJson("/phase0/seller-central/statement").catch(() => null) : null,
        fetchJson(session.is_admin ? "/admin/integrations/amazon/sources" : "/phase0/sources"),
      ]);

    state.tenant = tenant;
    state.summary = summary;
    state.exceptions = exceptions;
    state.dataQuality = Array.isArray(quality) ? quality : [];
    state.matched = Array.isArray(matched) ? matched : [];
    state.unmatchedExpected = Array.isArray(expected) ? expected : [];
    state.unmatchedReceipts = Array.isArray(receipts) ? receipts : [];
    state.uploads = Array.isArray(uploads) ? uploads : [];
    state.runs = Array.isArray(runs) ? runs : [];
    state.marketplaceActivity = Array.isArray(marketplaceActivity)
      ? marketplaceActivity
      : Array.isArray(marketplaceActivity?.rows) ? marketplaceActivity.rows : [];
    state.marketplaceActivityStatus = Array.isArray(marketplaceActivity)
      ? { status: "ready", diagnostics: {} }
      : marketplaceActivity;
    state.marketplaceAr = marketplaceAr;
    state.sourceEvidence = Array.isArray(sourceEvidence) ? sourceEvidence : [];
    state.inputReadiness = inputReadiness;
    state.calculationAudit = calculationAudit;
    state.cashflowProof = cashflowProof;
    state.apiStatus = apiStatus;
    state.amazonSources = Array.isArray(amazonSources) ? amazonSources : [];
    if (state.filters.sourceId && !state.amazonSources.some((source) => source.id === state.filters.sourceId)) {
      state.filters.sourceId = "";
      persistScopeFilters();
    }
    if (state.amazonSources.length) {
      if (!state.amazonSources.some((source) => source.id === state.selectedAmazonSourceId)) {
        state.selectedAmazonSourceId = state.amazonSources[0].id;
      }
      if (session.is_admin) await loadSelectedAmazonSource();
    } else {
      state.selectedAmazonSourceId = "";
      state.amazonSyncs = [];
    }
    await loadAmazonFinancialPositions();
    state.amazonTransactionDiagnostics = session.is_admin && state.filters.sourceId
      ? await fetchJson(`/admin/integrations/amazon/sources/${encodeURIComponent(state.filters.sourceId)}/transaction-collection`).catch(() => null)
      : null;
    state.sellerStatement = sellerStatement?.status === "loaded" ? sellerStatement : null;
    const latestSourceSync = state.amazonSources
      .map((source) => source.last_sync_at)
      .filter(Boolean)
      .sort()
      .at(-1);
    state.lastSyncAt = latestSourceSync ? new Date(latestSourceSync) : null;

    const isHealthy = health.status === "ok";
    const isFullyReady = isHealthy && inputReadiness?.status === "live_ready";
    const isPartial = isHealthy && !isFullyReady;
    if (status) {
      status.textContent = isHealthy ? readinessStatusLabel(inputReadiness) : "System check failed";
      status.classList.toggle("online", isFullyReady);
      status.classList.toggle("partial", isPartial);
      status.classList.toggle("offline", !isHealthy);
    }
    render();
    if (!isBackground && document.getElementById("admin")?.classList.contains("active")) {
      renderSettings();
    }
    if (!isBackground && document.getElementById("sources")?.classList.contains("active")) {
      renderAmazonIntegration();
    }
    startApiPolling();
    updateLiveIndicator();
  } catch (error) {
    if (status && !isBackground) {
      status.textContent = "System unavailable";
      status.classList.remove("online");
      status.classList.add("offline");
    }
    console.error(error);
    throw error;
  }
}

let _authenticationStopped = false;

function showAccessMessage(status) {
  const title = status === 401 ? "Authentication required" : "You do not have permission";
  let notice = document.getElementById("access-message");
  if (!notice) {
    notice = document.createElement("section");
    notice.id = "access-message";
    notice.className = "data-confidence-banner";
    document.querySelector("main")?.prepend(notice);
  }
  notice.innerHTML = status === 401
    ? `<strong>${title}</strong><span>Your session is unavailable.</span><button type="button" class="secondary-button" onclick="location.reload()">Reload</button>`
    : `<strong>${title}</strong><span>This action requires administrator access.</span>`;
}

async function apiFetch(url, options = {}) {
  const method = (options.method || "GET").toUpperCase();
  const headers = { ...(options.headers || {}) };
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
    headers["X-AI-Cashflow-Request"] = "browser";
  }
  const response = await fetch(url, { ...options, method, headers, credentials: "same-origin" });
  if (response.status === 401) {
    _authenticationStopped = true;
    stopApiPolling();
    showAccessMessage(401);
  } else if (response.status === 403) {
    showAccessMessage(403);
    document.querySelector('[data-view="admin"]')?.setAttribute("hidden", "");
    document.querySelectorAll("#admin button, #admin input, #admin select, #admin textarea")
      .forEach((control) => { control.disabled = true; });
  }
  return response;
}

async function fetchJson(url, options = {}) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  const response = await apiFetch(url, {
    method: options.method || "GET",
    headers,
    body: options.body ?? undefined,
  });
  if (!response.ok) {
    let detail = `${url} returned ${response.status}`;
    try {
      const payload = await response.json();
      if (payload.detail) detail = String(payload.detail);
    } catch {}
    const error = new Error(detail);
    error.status = response.status;
    throw error;
  }
  const text = await response.text();
  try { return JSON.parse(text); } catch { return text; }
}

function render() {
  const tenant = state.tenant;
  if (!state.summary || !tenant) return;

  document.title = tenant.product_name;
  document.documentElement.style.setProperty("--teal", tenant.brand?.primary_color || "#0f766e");
  setText("brand-mark", tenant.brand?.logo_text || "CC");
  setText("product-name", tenant.product_name);
  setText("tenant-name", tenant.tenant_name);

  document.getElementById("dashboard-error")?.remove();
  renderSafely("Dashboard filters", renderScopeControls);
  const expected = scopedRows(state.unmatchedExpected);
  const totalExceptions = expected.length;

  setText("settlement-pending-amount", formatCurrencyTotals(totalsByCurrency(expected)));
  setText("sources-marketplace-count", state.amazonSources?.length || 0);
  setText("sources-connected-count", (state.amazonSources || []).filter((source) => source.enabled).length);
  setText("review-count", expected.length);
  setText("action-total-count", totalExceptions);
  setText("action-total-items", totalExceptions);
  setText("action-pending-count", expected.length);
  setText("quality-count", state.dataQuality.length);
  setText("runs-count", state.runs.length);
  setText("settings-currency-count", Array.isArray(tenant.currencies) ? tenant.currencies.length : 0);
  setText("settings-source-count", state.amazonSources?.length || 0);

  renderSafely("Financial cards", renderTreasuryOverview);
  renderSafely("Overview panels", renderOverviewPanels);
  renderSafely("Marketplace AR", renderMarketplaceAr);
  renderSafely("Settlement activity", () => renderSettlementCards("unmatched-expected-table", expected, "pending"));
  renderSafely("Data quality", () => renderQualityList("quality-list", state.dataQuality));
  renderSafely("Exceptions", () => renderActionCards("exceptions-table", expected, []));
  renderSafely("Refresh history", renderRuns);
  // Keep background refreshes from wiping in-progress edits.
  if (!document.getElementById("admin")?.classList.contains("active")) {
    renderSafely("Settings", renderSettings);
  }
  if (!document.getElementById("sources")?.classList.contains("active")) {
    renderSafely("Amazon sources", renderAmazonIntegration);
  }
}

function showDashboardError(section, error) {
  console.error(`${section} failed`, error);
  let notice = document.getElementById("dashboard-error");
  if (!notice) {
    notice = document.createElement("section");
    notice.id = "dashboard-error";
    notice.className = "data-confidence-banner blocked";
    document.getElementById("global-scope-bar")?.after(notice);
  }
  notice.innerHTML = `<strong>${escapeHtml(section)} unavailable.</strong><span>The remaining dashboard is still available. Try again or contact support if the problem continues.</span>`;
}

function renderSafely(section, callback) {
  try {
    callback();
  } catch (error) {
    showDashboardError(section, error);
  }
}

function renderMarketplaceAr() {
  const summary = state.marketplaceAr;
  if (!summary) return;
  const coverage = document.getElementById("marketplace-ar-coverage");
  if (summary.status === "unavailable") {
    setText("marketplace-ar-total", "Withheld");
    setText("marketplace-ar-fx", "Withheld");
    setText("marketplace-ar-fx-note", "No verified API-fed AR snapshot is available.");
    coverage.className = "data-confidence-banner blocked";
    coverage.innerHTML = `<strong>No AR snapshot loaded.</strong><span>${escapeHtml(summary.message)}</span>`;
    return;
  }

  setText("marketplace-ar-date", summary.snapshot_date);
  setText("marketplace-ar-rate-date", summary.rate_date);
  setText("marketplace-ar-sources", summary.marketplace_count);
  setText(
    "marketplace-ar-source-note",
    `${summary.unique_marketplace_count} unique · ${summary.api_received_source_count} / ${summary.expected_source_count || "unverified"} received through API.`,
  );
  setText("marketplace-ar-duplicates", summary.duplicate_rows);
  setText(
    "marketplace-ar-duplicate-note",
    summary.duplicate_rows ? "Quarantined from live display." : "No duplicate rows detected.",
  );

  const issues = [];
  if (!summary.expected_source_count) issues.push("expected-source registry is missing");
  if (summary.missing_sources?.length) issues.push(`${summary.missing_sources.length} expected source(s) missing`);
  if (summary.unknown_sources?.length) issues.push(`${summary.unknown_sources.length} unregistered source(s) received`);
  if (summary.currency_mismatches?.length) issues.push(`${summary.currency_mismatches.length} source currency mismatch(es)`);
  if (!summary.coverage_verified && summary.pending_source_approvals?.length) {
    issues.push(`${summary.pending_source_approvals.length} source approval(s) pending`);
  }
  if (summary.pending_connector_count) issues.push(`${summary.pending_connector_count} connector(s) pending`);
  if (!summary.api_cutover_ready) {
    issues.push(
      `API-only cutover not ready (${summary.api_received_source_count} / ${summary.expected_source_count || "unverified"} sources)`,
    );
  }
  if (summary.freshness_status === "stale") issues.push(`snapshot is ${summary.snapshot_age_days} day(s) old`);
  if (summary.duplicate_rows) issues.push(`${summary.duplicate_rows} row(s) quarantined`);
  if (summary.conflicting_rows) issues.push(`${summary.conflicting_rows} conflicting row(s) block reporting`);

  if (!summary.display_ready) {
    setText("marketplace-ar-total", "Withheld");
    setText("marketplace-ar-fx", "Withheld");
    setText("marketplace-ar-fx-note", "Not displayed until API coverage, freshness, and data controls pass.");
    coverage.className = `data-confidence-banner ${summary.status === "blocked" ? "blocked" : "partial"}`;
    coverage.innerHTML = `<strong>Unverified AR values withheld</strong><span>${escapeHtml(issues.join(" · "))}. The snapshot remains available as audit evidence only.</span>`;
    document.getElementById("marketplace-ar-channels").innerHTML = '<div class="empty-state">Breakdown withheld until AR controls pass.</div>';
    document.getElementById("marketplace-ar-currencies").innerHTML = '<div class="empty-state">Breakdown withheld until AR controls pass.</div>';
    document.getElementById("marketplace-ar-top").innerHTML = '<div class="empty-state">Balances withheld until AR controls pass.</div>';
    return;
  }

  setText("marketplace-ar-total", formatUsd(summary.total_ar_usd));
  setText("marketplace-ar-fx", formatUsd(summary.fx_exposed_ar_usd));
  setText("marketplace-ar-fx-note", `${formatPercent(summary.fx_exposed_share)} of total AR originated outside USD.`);
  coverage.className = "data-confidence-banner ready";
  coverage.innerHTML = `<strong>AR controls passed</strong><span>All ${summary.received_source_count} approved sources are current, represented, and reconciled in USD.</span>`;

  renderArBreakdown("marketplace-ar-channels", summary.channel_breakdown);
  renderArBreakdown("marketplace-ar-currencies", summary.currency_breakdown);
  const top = document.getElementById("marketplace-ar-top");
  top.innerHTML = `
    <div class="ar-table-row ar-table-head"><span>Marketplace</span><span>Channel</span><span>Local value</span><span>Currency</span><span>AR in USD</span><span>Share</span></div>
    ${summary.top_balances.map((row) => `
      <div class="ar-table-row">
        <strong>${escapeHtml(row.venue)}</strong>
        <span>${escapeHtml(row.group)}</span>
        <span>${escapeHtml(row.local_amount)}</span>
        <span>${escapeHtml(row.currency)}</span>
        <strong>${formatUsd(row.amount_usd)}</strong>
        <span>${formatPercent(row.share)}</span>
      </div>`).join("")}`;
}

function renderArBreakdown(id, rows) {
  document.getElementById(id).innerHTML = (rows || []).map((row) => `
    <div class="ar-list-row">
      <div><strong>${escapeHtml(row.name)}</strong><small>${row.row_count} row${row.row_count === 1 ? "" : "s"}</small></div>
      <div><strong>${formatUsd(row.amount_usd)}</strong><small>${formatPercent(row.share)}</small></div>
    </div>`).join("");
}

function formatPercent(value) {
  return Number(value || 0).toLocaleString("en-US", {
    style: "percent",
    minimumFractionDigits: 1,
    maximumFractionDigits: 1,
  });
}

function setText(id, value) {
  const element = document.getElementById(id);
  if (element) {
    element.textContent = value;
  }
}

const COUNTRY_ICONS = {
  Canada: { img: "/assets/canada.png", alt: "Canada" },
};

const MARKETPLACE_PROFILES = [
  {
    test: (t) => t.includes("amazon.ca") || (t.includes("amazon") && t.includes("canada")),
    provider: "Amazon",
    providerClass: "amazon",
    img: "/assets/amazon.png",
    country: "Canada",
  },
  {
    test: (t) => t.includes("amazon"),
    provider: "Amazon",
    providerClass: "amazon",
    img: "/assets/amazon.png",
    country: "",
  },
];

function marketplaceProfile(value) {
  const text = String(value || "").toLowerCase();
  for (const profile of MARKETPLACE_PROFILES) {
    if (profile.test(text)) return profile;
  }
  return { provider: "Marketplace", providerClass: "generic", img: "", country: "" };
}

function marketplaceBadgesHtml(value) {
  const profile = marketplaceProfile(value);
  const iconHtml = profile.img
    ? `<img class="marketplace-glyph-img" src="${escapeAttribute(profile.img)}" alt="${escapeAttribute(profile.provider)}" aria-hidden="true">`
    : `<span class="marketplace-glyph-text" aria-hidden="true">${escapeHtml(profile.provider[0] || "M")}</span>`;

  const countryIcon = profile.country ? COUNTRY_ICONS[profile.country] : null;
  const countryBadge = profile.country
    ? `<span class="marketplace-badge country">
        ${countryIcon ? `<img class="marketplace-flag-img" src="${escapeAttribute(countryIcon.img)}" alt="${escapeAttribute(countryIcon.alt)}" aria-hidden="true">` : ""}
        ${escapeHtml(profile.country)}
       </span>`
    : "";

  return `
    <div class="marketplace-badges" aria-label="Marketplace and country">
      <span class="marketplace-badge provider ${escapeAttribute(profile.providerClass)}">
        ${iconHtml}
        ${escapeHtml(profile.provider)}
      </span>
      ${countryBadge}
    </div>
  `;
}

function setValue(id, value) {
  document.getElementById(id).value = value ?? "";
}

function value(id) {
  return document.getElementById(id).value.trim();
}

function renderKv(id, rows) {
  const element = document.getElementById(id);
  if (!element) {
    return;
  }
  element.innerHTML = rows
    .map(([label, value]) => `<div class="kv"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`)
    .join("");
}

const PAGE_SIZE = 50;

function renderTable(id, rows, emptyText = "No rows found.", pageSize = PAGE_SIZE) {
  const table = document.getElementById(id);
  if (!rows.length) {
    table.innerHTML = `<tbody><tr><td>${escapeHtml(emptyText)}</td></tr></tbody>`;
    return;
  }
  const keys = Object.keys(rows[0]);
  const visible = rows.slice(0, pageSize);
  const remaining = rows.length - visible.length;
  table.innerHTML = `
    <thead><tr>${keys.map((key) => `<th>${formatHeader(key)}</th>`).join("")}</tr></thead>
    <tbody>
      ${visible.map((row) => `<tr>${keys.map((key) => `<td>${escapeHtml(row[key])}</td>`).join("")}</tr>`).join("")}
    </tbody>
  `;
  renderLoadMore(table.parentElement || table, rows, keys, visible.length, remaining,
    (from, to) => rows.slice(from, to).map((row) =>
      `<tr>${keys.map((key) => `<td>${escapeHtml(row[key])}</td>`).join("")}</tr>`
    ).join(""),
    table.querySelector("tbody")
  );
  renderRowCount(table.parentElement || table, visible.length, rows.length, "row");
}

function renderQualityList(id, rows) {
  const container = document.getElementById(id);
  if (!container) return;
  if (!rows.length) {
    container.innerHTML = `<div class="empty-state">No import issues found — all uploaded files passed validation checks.</div>`;
    return;
  }
  const severityOrder = { error: 0, warning: 1, info: 2 };
  const sorted = [...rows].sort((a, b) => {
    const sa = severityOrder[(a.severity || a.level || "info").toLowerCase()] ?? 2;
    const sb = severityOrder[(b.severity || b.level || "info").toLowerCase()] ?? 2;
    return sa - sb;
  });
  container.innerHTML = sorted.map((row) => {
    const sev = (row.severity || row.level || "info").toLowerCase();
    const sevClass = sev === "error" ? "quality-error" : sev === "warning" ? "quality-warning" : "quality-info";
    const sevLabel = sev === "error" ? "Error" : sev === "warning" ? "Warning" : "Info";
    const file = row.file || row.source_file || row.filename || "";
    const field = row.field || row.column || "";
    const msg = row.message || row.check || row.description || Object.values(row).find((v) => typeof v === "string" && v.length > 10) || "-";
    return `
      <div class="quality-row ${sevClass}">
        <span class="quality-badge">${escapeHtml(sevLabel)}</span>
        <div class="quality-content">
          <strong>${escapeHtml(msg)}</strong>
          ${file ? `<span class="muted">${escapeHtml(fileBasename(file))}${field ? " · " + escapeHtml(field) : ""}</span>` : ""}
        </div>
      </div>`;
  }).join("");
  setText("quality-count", rows.length);
}

function renderRowCount(container, shown, total, unit) {
  let counter = container.querySelector(".row-count");
  if (!counter) {
    counter = document.createElement("div");
    counter.className = "row-count";
    container.insertBefore(counter, container.firstChild);
  }
  counter.textContent = shown >= total
    ? `${total.toLocaleString()} ${unit}${total !== 1 ? "s" : ""}`
    : `Showing ${shown.toLocaleString()} of ${total.toLocaleString()} ${unit}s`;
}

function renderLoadMore(container, allRows, keys, shownCount, remaining, rowHtml, tbody) {
  const existing = container.querySelector(".load-more-row");
  if (existing) existing.remove();
  if (remaining <= 0) return;
  const btn = document.createElement("div");
  btn.className = "load-more-row";
  btn.innerHTML = `<button type="button" class="secondary-button">Load ${Math.min(PAGE_SIZE, remaining).toLocaleString()} more <span class="muted">(${remaining.toLocaleString()} remaining)</span></button>`;
  btn.querySelector("button").addEventListener("click", () => {
    const newShown = shownCount + PAGE_SIZE;
    const batch = rowHtml(shownCount, newShown);
    tbody.insertAdjacentHTML("beforeend", batch);
    const newRemaining = allRows.length - newShown;
    renderLoadMore(container, allRows, keys, newShown, newRemaining, rowHtml, tbody);
    renderRowCount(container, Math.min(newShown, allRows.length), allRows.length, "row");
  });
  container.appendChild(btn);
}

function filteredSources() {
  const sources = state.amazonSources || [];
  return state.filters.sourceId
    ? sources.filter((source) => source.id === state.filters.sourceId)
    : sources;
}

function rowDate(row) {
  const raw = row.posted_at || row.expected_date || row.receipt_date || "";
  const match = String(raw).match(/^(\d{2})\.(\d{2})\.(\d{4})/);
  if (match) return new Date(`${match[3]}-${match[2]}-${match[1]}T00:00:00Z`);
  const parsed = new Date(raw);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

function scopedRows(rows, overrides = {}) {
  const sourceId = overrides.sourceId ?? state.filters.sourceId;
  const currency = overrides.currency ?? state.filters.currency;
  const period = overrides.period ?? state.filters.period;
  const cutoff = period === "all" ? null : new Date(Date.now() - Number(period) * 86_400_000);
  return (rows || []).filter((row) => {
    if (sourceId && row.source_id !== sourceId) return false;
    if (currency && row.currency !== currency) return false;
    if (cutoff) {
      const date = rowDate(row);
      if (!date || date < cutoff) return false;
    }
    return true;
  });
}

function totalsByCurrency(rows, field = "amount") {
  const totals = new Map();
  for (const row of rows || []) {
    const currency = String(row.currency || "Unknown").toUpperCase();
    totals.set(currency, (totals.get(currency) || 0) + moneyNumber(row[field]));
  }
  return totals;
}

function formatCurrencyTotals(totals, fallback = "—") {
  if (!totals?.size) return fallback;
  return [...totals.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([currency, amount]) => `${currency} ${formatMoney(amount)}`)
    .join(" · ");
}

function sourceFreshness(source) {
  if (!source?.last_sync_at) return { tone: "missing", label: "Never synced" };
  const syncedAt = new Date(source.last_sync_at);
  const ageMinutes = Math.max(0, (Date.now() - syncedAt.getTime()) / 60_000);
  const interval = Math.max(Number(source.sync_frequency_minutes) || 60, 5);
  if (!source.enabled) return { tone: "disabled", label: "Snapshot · automatic sync off" };
  if (ageMinutes <= Math.max(interval * 2, 30)) return { tone: "current", label: "Current" };
  if (ageMinutes <= Math.max(interval * 4, 120)) return { tone: "delayed", label: "Delayed" };
  return { tone: "stale", label: "Stale" };
}

function renderScopeControls() {
  configureStatementSnapshotForm();
  const sourceSelect = document.getElementById("global-source-filter");
  const currencySelect = document.getElementById("global-currency-filter");
  const periodSelect = document.getElementById("global-period-filter");
  const sources = state.amazonSources || [];
  const selected = sources.find((source) => source.id === state.filters.sourceId);

  sourceSelect.innerHTML = `<option value="">All sources</option>${sources.map((source) =>
    `<option value="${escapeAttribute(source.id)}">${escapeHtml(source.name)}</option>`
  ).join("")}`;
  sourceSelect.value = state.filters.sourceId;

  const currencyRows = [state.marketplaceActivity, state.unmatchedExpected, state.matched]
    .flat()
    .filter((row) => !state.filters.sourceId || row.source_id === state.filters.sourceId);
  const position = state.amazonFinancialPosition?.source_id === state.filters.sourceId
    ? state.amazonFinancialPosition
    : null;
  const marketplaces = Array.isArray(selected?.marketplaces) ? selected.marketplaces : [];
  const currencies = [...new Set([
    ...currencyRows.map((row) => row.currency),
    ...marketplaces.map((row) => row.currency),
    ...(Array.isArray(position?.available_currencies) ? position.available_currencies : []),
    position?.currency,
    position?.source_currency,
  ].filter(Boolean))].sort();
  if (state.filters.currency && !currencies.includes(state.filters.currency)) state.filters.currency = "";
  currencySelect.innerHTML = `<option value="">All currencies</option>${currencies.map((currency) =>
    `<option value="${escapeAttribute(currency)}">${escapeHtml(currency)}</option>`
  ).join("")}`;
  currencySelect.value = state.filters.currency;
  periodSelect.value = state.filters.period;

  setText("scope-label", selected ? `Source scope · ${selected.name}` : "Group treasury · all sources");
  const syncButton = document.getElementById("sync-selected-source");
  syncButton.hidden = !selected;
  syncButton.disabled = _selectedSourceSyncing || Boolean(selected && !selected.credentials?.refresh_token_configured);

  const scopedSources = filteredSources();
  const synced = scopedSources.filter((source) => source.last_sync_at);
  if (selected) {
    const freshness = sourceFreshness(selected);
    setText("scope-freshness-status", freshness.label);
    setText("scope-freshness-detail", selected.last_sync_at
      ? `Last successful Amazon sync ${formatIntegrationTime(selected.last_sync_at)}`
      : "This source has not produced settlement data yet.");
  } else if (synced.length) {
    const automatic = synced.filter((source) => source.enabled).length;
    setText("scope-freshness-status", `${synced.length} of ${scopedSources.length} sources have synchronized data`);
    setText("scope-freshness-detail", `${automatic} automatic sync source${automatic === 1 ? "" : "s"}; latest ${formatIntegrationTime(synced.map((source) => source.last_sync_at).sort().at(-1))}`);
  } else {
    setText("scope-freshness-status", "No synchronized source data");
    setText("scope-freshness-detail", "Authorize and sync an Amazon source before using financial totals.");
  }
  updateLiveIndicator();
}

function financialPositionDisplay(position) {
  return {
    values: position.source_amounts,
    currency: position.source_currency,
    comparisonValues: position.amounts,
    comparisonCurrency: position.currency,
  };
}

function formatPositionAmount(currency, value) {
  const formatted = formatMoney(moneyNumber(value));
  return currency === "AUD" ? `$${formatted}` : `${currency} ${formatted}`;
}

function financialValues(position, type) {
  const values = Array.isArray(position?.financial_values) ? position.financial_values : [];
  return values.filter((value) => value && value.type === type);
}

function unavailableFinancialValue(position, type, fallback) {
  const value = financialValues(position, type).find((candidate) => candidate.amount === null);
  return value?.availabilityReason || fallback;
}

function nativeOpenBalances(position) {
  if (position?.mode === "amazon_open_balances") {
    return Object.entries(position.totals_by_currency || {}).map(([currency, amount]) => ({ currency, amount }));
  }
  return financialValues(position, "OPEN_BALANCE")
    .filter((value) => value.amount !== null && value.currency)
    .map(({ currency, amount }) => ({ currency, amount }));
}

function sourceStatusHtml(position) {
  const labels = {
    passed: "Passed",
    healthy: "Healthy",
    not_verified: "Not Verified",
    unavailable: "Unavailable",
  };
  const status = position?.source_status && typeof position.source_status === "object" ? position.source_status : {};
  return Object.values(status).filter((item) => item && typeof item === "object").map((item) => `
    <div class="source-status-item">
      <span>${escapeHtml(item.label || "Status")}</span>
      <strong class="status-${escapeAttribute(item.status || "unknown")}">${escapeHtml(labels[item.status] || item.status || "Unknown")}</strong>
    </div>`).join("");
}

function renderAvailability(position, type, cashId, noteId, fallback) {
  const value = financialValues(position, type).find((candidate) => candidate.amount !== null);
  if (value) {
    setText(cashId, formatPositionAmount(value.currency, value.amount));
    setText(noteId, `${value.sourceField || "Amazon SP-API"} · ${value.processingStatus || "reported"}`);
    return;
  }
  setText(cashId, "Unavailable");
  setText(noteId, unavailableFinancialValue(position, type, fallback));
}

function nativeDecimalParts(value) {
  const match = String(value ?? "").trim().match(/^(-?)(\d+)(?:\.(\d+))?$/);
  if (!match) return null;
  return { negative: match[1] === "-", whole: match[2], fraction: match[3] || "" };
}

function addNativeAmounts(left, right) {
  const leftParts = nativeDecimalParts(left);
  const rightParts = nativeDecimalParts(right);
  if (!leftParts || !rightParts) return null;
  const scale = Math.max(leftParts.fraction.length, rightParts.fraction.length);
  const toMinorUnits = (parts) => {
    const digits = `${parts.whole}${parts.fraction.padEnd(scale, "0")}`;
    const amount = BigInt(digits);
    return parts.negative ? -amount : amount;
  };
  const total = toMinorUnits(leftParts) + toMinorUnits(rightParts);
  const negative = total < 0n ? "-" : "";
  const digits = (total < 0n ? -total : total).toString().padStart(scale + 1, "0");
  return scale
    ? `${negative}${digits.slice(0, -scale)}.${digits.slice(-scale)}`
    : `${negative}${digits}`;
}

function formatNativeAmount(currency, value) {
  const parts = nativeDecimalParts(value);
  if (!parts) return "Unavailable";
  const whole = parts.whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const fraction = parts.fraction.padEnd(2, "0");
  return `${currency} ${parts.negative ? "-" : ""}${whole}.${fraction}`;
}

function renderAccountBalanceSummary(position) {
  const selectedSourceId = state.filters.sourceId;
  const selectedCurrency = state.filters.currency;
  const isCurrentSnapshot = Boolean(
    position?.status === "ready"
    && position.source_id === selectedSourceId
    && position.snapshot_id
    && selectedCurrency
    && position.currency_scope === selectedCurrency
  );
  const standardEl = document.getElementById("account-balance-standard");
  const deferredEl = document.getElementById("account-balance-deferred");
  const allAccountsEl = document.getElementById("account-balance-all-accounts");
  const fundsEl = document.getElementById("account-balance-funds");
  const reserveEl = document.getElementById("account-balance-reserve");
  const deferredBadge = document.getElementById("account-balance-deferred-coverage");
  const allAccountsBadge = document.getElementById("account-balance-all-coverage");
  const snapshotEl = document.getElementById("account-balance-snapshot");
  const fundsNote = document.getElementById("account-balance-funds-note");
  const unavailableFundsNote = "Amazon has not supplied an authoritative funds-available value through the current API source.";

  deferredBadge.hidden = true;
  allAccountsBadge.hidden = true;
  fundsNote.textContent = unavailableFundsNote;
  if (!isCurrentSnapshot) {
    standardEl.textContent = "Unavailable";
    deferredEl.textContent = "Unavailable";
    allAccountsEl.textContent = "Unavailable";
    fundsEl.textContent = "Unavailable";
    reserveEl.textContent = "Unavailable";
    snapshotEl.textContent = selectedSourceId && !selectedCurrency
      ? "Select a native currency to view a financial snapshot."
      : "Select one source and native currency to view a financial snapshot.";
    return;
  }

  const standard = nativeOpenBalances(position).find((entry) => entry.currency === selectedCurrency);
  const hasStandard = standard?.amount !== null && standard?.amount !== undefined;
  standardEl.textContent = hasStandard ? formatNativeAmount(selectedCurrency, standard.amount) : "Unavailable";
  deferredEl.textContent = "Unavailable";
  allAccountsEl.textContent = "Unavailable";
  reserveEl.textContent = "Unavailable";
  const manual = position.seller_central_statement_snapshot;
  const observed = manual?.observed_at ? new Date(manual.observed_at) : null;
  const age = observed && !Number.isNaN(observed.getTime()) ? Date.now() - observed.getTime() : Infinity;
  const freshness = age <= 30 * 60 * 1000 ? "FRESH" : age <= 24 * 60 * 60 * 1000 ? "STALE" : "EXPIRED";
  const manualValue = (field, target) => {
    if (!manual || freshness === "EXPIRED" || manual[field] === null || manual[field] === undefined) return false;
    target.textContent = formatNativeAmount(selectedCurrency, manual[field]);
    return true;
  };
  const hasManualDeferred = manualValue("deferred_transactions", deferredEl);
  const hasManualAll = manualValue("all_accounts", allAccountsEl);
  manualValue("account_level_reserve", reserveEl);

  const fundsAvailable = financialValues(position, "FUNDS_AVAILABLE").find((value) => (
    value.currency === selectedCurrency && value.amount !== null && value.isAuthoritative === true
  ));
  fundsEl.textContent = fundsAvailable
    ? formatNativeAmount(selectedCurrency, fundsAvailable.amount)
    : "Unavailable";
  if (fundsAvailable) {
    fundsNote.textContent = `${fundsAvailable.sourceField || "Amazon SP-API"} · authoritative Amazon-reported value`;
  }
  if (!fundsAvailable && manualValue("funds_available", fundsEl)) fundsNote.textContent = `Seller Central reported · ${freshness} · observed ${String(manual.observed_at).slice(0, 19).replace("T", " ")} UTC`;
  if (manual && freshness !== "EXPIRED") {
    if (hasManualDeferred) deferredBadge.hidden = false;
    if (hasManualAll) allAccountsBadge.hidden = false;
    snapshotEl.textContent = `Seller Central reported · ${freshness} · observed ${String(manual.observed_at).slice(0, 19).replace("T", " ")} UTC · statement snapshot ${manual.id}`;
  } else if (manual) {
    snapshotEl.textContent = "Seller Central statement snapshot expired · refresh required.";
  } else snapshotEl.textContent = `Snapshot ${position.snapshot_id} · ${selectedCurrency} · updated ${String(position.snapshot_created_at || position.as_of || "Unavailable").slice(0, 19).replace("T", " ")} UTC`;
}

function renderTransactionVisibility(position) {
  const enabled = Boolean(position?.transaction_visibility_enabled);
  const cards = ["overview-deferred-card", "overview-released-card", "overview-completed-payout-card"];
  cards.forEach((id) => { document.getElementById(id).hidden = !enabled; });
  const breakdown = document.getElementById("overview-transaction-breakdown");
  breakdown.hidden = !enabled;
  if (!enabled) return;

  const visibility = position?.transaction_visibility || {};
  const entries = visibility.by_currency && typeof visibility.by_currency === "object"
    ? Object.entries(visibility.by_currency)
    : (visibility.currency ? [[visibility.currency, visibility]] : []);
  const moneyRows = (selector) => entries.map(([currency, row]) => ({
    currency,
    amount: row?.[selector],
  }));
  const renderRows = (target, rows) => {
    const element = document.getElementById(target);
    element.className = "open-balance-list";
    element.innerHTML = rows.length
      ? rows.map(({ currency, amount }) => `<span><b>${escapeHtml(currency)}</b><span>${escapeHtml(formatMoney(moneyNumber(amount)))}</span></span>`).join("")
      : "Unavailable";
  };
  renderRows("overview-deferred-transactions", moneyRows("deferred_amount"));

  const releasedRows = [];
  entries.forEach(([currency, row]) => {
    releasedRows.push({ currency: `${currency} Released`, amount: row?.released_amount });
    releasedRows.push({ currency: `${currency} Previously Deferred, Released During Observed Period`, amount: row?.deferred_released_amount });
  });
  renderRows("overview-released-transactions", releasedRows);

  const completed = position?.recent_completed_payouts && typeof position.recent_completed_payouts === "object"
    ? Object.entries(position.recent_completed_payouts)
    : (position?.recent_completed_payout ? [[position.recent_completed_payout.currency, position.recent_completed_payout]] : []);
  renderRows("overview-completed-payout", completed.map(([currency, row]) => ({ currency, amount: row?.amount })));
  setText("overview-completed-payout-note", completed.length
    ? `Only successful, positive Closed transfers. ${completed.map(([, row]) => `${row.transfer_date || "Date unavailable"} · ${row.financial_event_group_id || "ID unavailable"}`).join(" · ")}`
    : "No successful, positive Closed transfer is available in this scope.");

  const coverageRows = entries.map(([, row]) => row?.coverage || {}).filter(Boolean);
  const coverageStarts = coverageRows.map((row) => row.observed_start || row.completed_start).filter(Boolean).sort();
  const coverageEnds = coverageRows.map((row) => row.observed_end || row.completed_end).filter(Boolean).sort();
  const retrieved = coverageRows.map((row) => row.last_successful_collection_at).filter(Boolean).sort();
  const isComplete = coverageRows.length > 0 && coverageRows.every((row) => row.is_historically_complete ?? row.is_complete);
  const transactionCount = entries.reduce((sum, [, row]) => sum + Number(row?.count || 0), 0);
  const observedStart = formatUtcCoverage(coverageStarts[0]);
  const observedEnd = formatUtcCoverage(coverageEnds.at(-1));
  const coverageLabel = `${observedStart} to ${observedEnd}`;
  const historicalStatuses = coverageRows.map((row) => row.historical_backfill_status || "NOT_STARTED");
  const historicalStatus = historicalStatuses.every((status) => status === "COMPLETE")
    ? "Complete"
    : historicalStatuses.some((status) => status !== "NOT_STARTED")
      ? historicalStatuses.join(", ")
      : "Not started";
  const canaries = state.amazonTransactionDiagnostics?.validation_canaries || [];
  const canaryStatus = canaries.length && canaries.every((row) => row.status === "completed")
    ? "Completed successfully"
    : canaries.length ? "Requires review" : "Not run";
  setText("overview-coverage-badge", isComplete ? "Complete coverage" : "Partial coverage");
  setText("overview-coverage-warning", isComplete ? "Historical coverage is complete." : "Historical coverage is incomplete.");
  setText("overview-released-note", `Persisted released transactions collected from ${coverageLabel}.`);
  document.getElementById("overview-deferred-meta").innerHTML = `
    <span><b>Data classification</b> AMAZON_TRANSACTION_DERIVED</span>
    <span><b>Collected from</b> ${escapeHtml(observedStart)}</span>
    <span><b>Collected through</b> ${escapeHtml(observedEnd)}</span>
    <span><b>Classification</b> ${isComplete ? "COMPLETE_COVERAGE" : "PARTIAL_COVERAGE"}</span>
    <span><b>Transactions</b> ${transactionCount.toLocaleString()}</span>
    <span><b>Last collection</b> ${escapeHtml(formatUtcCoverage(retrieved.at(-1)))}</span>
    <span><b>Reconciliation</b> ${escapeHtml(entries.map(([, row]) => row?.reconciliation_state || "not_verified").join(", ") || "not_verified")}</span>
    <span><b>Incremental collection</b> ${coverageRows.some((row) => row.has_observed_data) ? "Active" : "Not started"}</span>
    <span><b>Historical backfill</b> ${escapeHtml(historicalStatus)}</span>
    <span><b>Validation canaries</b> ${escapeHtml(canaryStatus)}</span>`;
  setText("overview-transaction-composition-title", isComplete ? "Transaction Composition" : "Transaction Composition — Partial Coverage");
  setText("overview-transaction-composition-note", isComplete
    ? "This breakdown reflects persisted Amazon transactions for the completed coverage period."
    : `This breakdown reflects persisted Amazon transactions within the displayed observed coverage. Historical backfill ${historicalStatus.toLowerCase()}.`);
  document.getElementById("overview-transaction-composition-meta").innerHTML = `
    <span><b>Coverage</b> ${escapeHtml(coverageLabel)}</span>
    <span><b>Completeness</b> ${isComplete ? "Complete" : "Partial coverage"}</span>
    <span><b>Transactions</b> ${transactionCount.toLocaleString()}</span>`;

  const composition = [];
  entries.forEach(([currency, row]) => {
    Object.entries(row?.composition || {}).forEach(([bucket, amount]) => {
      composition.push(`<div class="payout-forecast-row"><b>${escapeHtml(currency)}</b><span>${escapeHtml(bucket)}</span><strong>${escapeHtml(formatMoney(moneyNumber(amount)))}</strong></div>`);
    });
  });
  document.getElementById("overview-transaction-composition").innerHTML = composition.join("") || '<div class="empty-state">No component-level amounts were returned for this scope.</div>';
}

function formatUtcCoverage(value) {
  if (!value) return "Unavailable";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : `${date.toLocaleString("en-GB", {
    day: "numeric", month: "long", year: "numeric", hour: "2-digit", minute: "2-digit",
    hour12: false, timeZone: "UTC",
  })} UTC`;
}

function renderTreasuryOverview() {
  const sources = filteredSources();
  const position = state.amazonFinancialPosition;
  const missing = [];
  setText("overview-confirmed-label", "Amazon-Reported Open Balances");
  setText("overview-reserve-label", "Current reserve");
  setText("overview-funds-label", "Funds available after reserve");
  setText("overview-upcoming-label", "Upcoming payout");
  renderTransactionVisibility(
    position?.source_id === state.filters.sourceId ? position : null
  );
  renderAccountBalanceSummary(
    position?.source_id === state.filters.sourceId ? position : null
  );

  if (position?.status === "ready" && position.source_id === state.filters.sourceId) {
    const balances = nativeOpenBalances(position);
    const balanceEl = document.getElementById("overview-confirmed-cash");
    balanceEl.innerHTML = balances.length
      ? balances.map(({ currency, amount }) => `<span role="listitem"><b>${escapeHtml(currency)}</b><span>${escapeHtml(formatMoney(moneyNumber(amount)))}</span></span>`).join("")
      : "Unavailable";
    setText("overview-confirmed-note", `${Number(position.group_count ?? position.openSettlementGroupCount ?? 0)} unique open financial event group(s), deduplicated by FinancialEventGroupId.`);
    document.getElementById("overview-open-balance-meta").innerHTML = `
      <span><b>Source</b> Amazon SP-API</span>
      <span><b>ProcessingStatus</b> Open</span>
      <span><b>Retrieved</b> ${escapeHtml(String(position.as_of || "Unavailable").slice(0, 19).replace("T", " "))} UTC</span>
      <span><b>Freshness</b> ${escapeHtml(position.data_freshness?.status || "unknown")}</span>`;

    const fx = position.fx_conversion || {};
    const fxEl = document.getElementById("overview-fx-estimate");
    fxEl.className = `fx-estimate ${fx.status === "stale" ? "stale" : ""}`;
    fxEl.innerHTML = `
      <span>Estimated USD Equivalent</span>
      <strong>${fx.amount === null || fx.amount === undefined ? "Unavailable" : `USD ${escapeHtml(formatMoney(moneyNumber(fx.amount)))}`}</strong>
      <small>FX source: ${escapeHtml(fx.fx_source || "Unavailable")} · Rate date: ${escapeHtml(fx.fx_rate_as_of || "Unavailable")} · Retrieved: ${escapeHtml(fx.fx_retrieved_at || "Unavailable")} · Currencies: ${escapeHtml((Array.isArray(fx.currencies_included) ? fx.currencies_included : []).join(", ") || "Unavailable")} · Status: ${escapeHtml(fx.status || "unknown")}</small>
      ${fx.availability_reason ? `<em>${escapeHtml(fx.availability_reason)}</em>` : ""}
      <small>${escapeHtml(fx.disclaimer || "Converted values are estimates for reporting purposes. Native-currency Amazon amounts remain authoritative.")}</small>`;

    renderAvailability(position, "CURRENT_RESERVE", "overview-reserve-cash", "overview-reserve-note", "Amazon SP-API has not provided an authoritative current reserve value.");
    renderAvailability(position, "RESERVE_ADJUSTED_FUNDS", "overview-funds-cash", "overview-funds-note", "Funds available cannot be calculated without an authoritative current reserve.");
    renderAvailability(position, "UPCOMING_PAYOUT", "overview-upcoming-cash", "overview-upcoming-note", "Amazon SP-API has not supplied an authoritative upcoming payout amount and transfer date.");
    document.getElementById("overview-source-status").innerHTML = sourceStatusHtml(position);

    const readiness = document.getElementById("overview-readiness");
    readiness.className = "data-confidence-banner partial";
    readiness.innerHTML = "<strong>Amazon open balances loaded successfully.</strong><span>Reserve-adjusted funds and upcoming payouts remain unavailable where Amazon has not supplied authoritative values.</span>";
    return;
  }

  setText("overview-confirmed-cash", "Unavailable");
  setText("overview-confirmed-note", position?.message || "Select one Amazon source to load its current balance.");
  document.getElementById("overview-open-balance-meta").innerHTML = "";
  document.getElementById("overview-fx-estimate").innerHTML = "";
  document.getElementById("overview-source-status").innerHTML = "";
  if (state.filters.sourceId) missing.push("current Amazon balance");

  setText("overview-reserve-cash", "Unavailable");
  setText("overview-reserve-note", "Amazon SP-API has not provided an authoritative current reserve value.");
  setText("overview-funds-cash", "Unavailable");
  setText("overview-funds-note", "Funds available cannot be calculated without an authoritative current reserve.");
  setText("overview-upcoming-cash", "Unavailable");
  setText("overview-upcoming-note", "Amazon SP-API has not supplied an authoritative upcoming payout amount and transfer date.");
  missing.push("current reserve and upcoming payout");

  const neverSynced = sources.filter((source) => !source.last_sync_at).length;
  const automatic = sources.filter((source) => source.enabled).length;
  if (neverSynced) missing.push(`${neverSynced} source${neverSynced === 1 ? " has" : "s have"} never synced`);
  if (sources.length && !automatic) missing.push("automatic source sync");
  const readiness = document.getElementById("overview-readiness");
  if (!sources.length) {
    readiness.className = "data-confidence-banner blocked";
    readiness.innerHTML = `<strong>No marketplace sources configured.</strong><span>Add and authorize a source before relying on this dashboard.</span>`;
  } else if (missing.length) {
    readiness.className = "data-confidence-banner partial";
    readiness.innerHTML = `<strong>Partial treasury view</strong><span>${escapeHtml(missing.join(" · "))}. Missing inputs are shown as unavailable, never as zero.</span>`;
  } else {
    readiness.className = "data-confidence-banner ready";
    readiness.innerHTML = `<strong>Source-backed view ready</strong><span>All required inputs for the selected scope are available.</span>`;
  }
}

function detectStatementCurrency(statement) {
  return statement ? "USD" : "";
}

function statementUsdNumber(statement, value) {
  const sourceCurrency = String(statement?.currency || "USD").toUpperCase();
  const rate = Number(state.tenant?.reconciliation?.usd_exchange_rates?.[sourceCurrency]);
  if (!Number.isFinite(rate) || rate <= 0) {
    throw new Error(`Missing positive USD exchange rate for ${sourceCurrency}.`);
  }
  return moneyNumber(value) * rate;
}

function renderSourceBasis() {
  const readiness = state.inputReadiness;
  if (readiness) {
    const blocked = readiness.checks.filter((c) =>
      c.status === "blocked" && !/bank|cash confirmation/i.test(`${c.name} ${c.detail}`)
    );
    const passed = readiness.checks.find((c) => c.status === "passed");
    const audit = state.calculationAudit;
    const auditText = audit?.status === "passed" ? "Calculation audit passed." : "Calculation audit needs review.";

    // Detect source mode from tenant config
    const marketplaces = state.tenant?.sources?.marketplaces || [];
    const hasApiSource = marketplaces.some((s) => (s.type || "").toLowerCase().includes("api"));
    const hasApiCredential = marketplaces.some((s) =>
      (s.type || "").toLowerCase().includes("api") && s.api_endpoint && s.credential_reference
    );
    const hasTransactions = (state.summary?.marketplace_transaction_records || 0) > 0;

    // Headline reflects actual connection method
    let headline;
    if (hasApiSource && hasApiCredential && hasTransactions) {
      headline = "Amazon API connected. Marketplace and current balance data flowing.";
    } else if (hasApiSource && !hasApiCredential) {
      headline = "API source configured — set the credential reference to start receiving data.";
    } else if (hasApiSource) {
      headline = "API source configured. Complete credential setup to enable live data.";
    } else if (hasTransactions) {
      headline = "Amazon marketplace data loaded.";
    } else {
      headline = hasApiSource
        ? "Configure API credentials to start receiving marketplace data."
        : "No data loaded yet. Configure a marketplace source and connect data.";
    }
    setText("source-basis-headline", headline);
    setText("source-basis-note", blocked.length ? "Some Amazon source inputs still need attention." : "Amazon source inputs are ready.");
    setText("source-basis-file", blocked.length ? "Action required" : "Amazon inputs ready");
    setText("source-basis-limit",
      `${auditText} ${passed ? passed.detail : "No source-backed inputs are available yet."}`);
    setText("source-gap-count", `${blocked.length} required input${blocked.length !== 1 ? "s" : ""}`);
    renderGapChips("source-gap-list", blocked);
    return;
  }

  const evidence = state.sourceEvidence || [];
  const loaded = evidence.find((row) => row.status === "Loaded");
  const gaps = evidence.filter((row) => row.status !== "Loaded");
  setText("source-basis-headline", loaded
    ? "Amazon marketplace data loaded."
    : "No source files loaded. Connect an Amazon marketplace source before relying on this view.");
  setText("source-basis-note",
    loaded
      ? "Figures are based on synchronized Amazon marketplace reports."
      : "Upload marketplace files before relying on the dashboard.");
  setText("source-basis-file", loaded ? `${loaded.source}: ${loaded.file}` : "No loaded source");
  setText("source-basis-limit", loaded ? loaded.limitation : "No figures should be treated as source-backed.");
  setText("source-gap-count", `${gaps.length} required inputs`);
  renderGapChips("source-gap-list", gaps.map((row) => ({ name: row.source, detail: "" })));
}

// Map check name patterns → { page, label, icon, sub } for quick-action buttons
const GAP_NAV_MAP = [
  { test: (n) => n.startsWith("API credential"),               page: "sources",    label: "Connect API",     icon: "🔌", sub: "Add API credentials in Sources" },
  { test: (n) => n.includes("Marketplace report files"),       page: "sources",    label: "Review Sources",  icon: "🔌", sub: "Connect the marketplace API source" },
  { test: (n) => n.includes("Portal report"),                  page: "sources",    label: "Review Sources",  icon: "🔌", sub: "Replace the portal report with an API connection" },
  { test: (n) => n.includes("Transaction activity"),           page: "sources",    label: "Review Sources",  icon: "🔌", sub: "Check synchronized transaction activity" },
  { test: (n) => n.includes("Expected payout"),                page: "sources",    label: "Review Sources",  icon: "🔌", sub: "Check synchronized payout records" },
  { test: (n) => n.includes("Entity and currency"),            page: "admin",      label: "Open Admin",      icon: "⚙️",  sub: "Set entity name and currency" },
  { test: (n) => n.includes("Marketplace source setup"),       page: "sources",    label: "Open Sources",    icon: "⚙️",  sub: "Configure a marketplace source" },
  { test: (n) => n.includes("Planned outflow"),                page: "admin",      label: "Open Admin",      icon: "⚙️",  sub: "Enter planned outflow amounts" },
  { test: (n) => n.includes("Calculation audit"),              page: "payouts",    label: "View Issues",      icon: "🔍", sub: "Review data quality issues" },
  { test: (n) => n.includes("Source manifest"),                page: "sources",    label: "Open Sources",    icon: "📄", sub: "Review source readiness" },
];

function navigateTo(page) {
  location.hash = page;
  showView(page);
}

function _gapNav(name) {
  return GAP_NAV_MAP.find((m) => m.test(name)) || { page: "admin", label: "Open Admin", icon: "⚙️", sub: name };
}

function renderGapChips(id, checks) {
  const el = document.getElementById(id);
  if (!el) return;
  if (!checks.length) {
    el.innerHTML = `<span class="gap-chip all-clear">✓ All inputs ready</span>`;
    return;
  }
  el.innerHTML = checks.slice(0, 6).map((c) => {
    const nav = _gapNav(c.name);
    return `<button type="button" class="gap-chip" data-page="${escapeAttribute(nav.page)}">
      <span class="gap-chip-icon">${nav.icon}</span>
      <span class="gap-chip-body">
        <span class="gap-chip-label">${escapeHtml(c.name)}</span>
        <span class="gap-chip-sub">${escapeHtml(nav.sub)}</span>
      </span>
      <span class="gap-chip-cta">${escapeHtml(nav.label)}</span>
    </button>`;
  }).join("");
  if (checks.length > 6) {
    el.innerHTML += `<span class="gap-chip muted">+${checks.length - 6} more required inputs</span>`;
  }
  el.querySelectorAll(".gap-chip[data-page]").forEach((btn) => {
    btn.addEventListener("click", () => navigateTo(btn.dataset.page));
  });
}

function readinessStatusLabel(readiness) {
  if (!readiness) return "Checking readiness";
  if (readiness.status === "live_ready") return "Live data ready";
  if (readiness.status === "partially_configured") return "Partially configured";
  return "Input setup required";
}

function renderRecentPayouts() {
  const container = document.getElementById("treasury-recent-payouts");
  const position = state.amazonFinancialPosition;
  if (position?.mode === "amazon_open_balances") {
    container.innerHTML = `<div class="empty-state compact">Open balances are current Amazon SP-API records. Completed payouts remain separate and are not inferred from open groups.</div>`;
    return;
  }
  if (
    position?.status === "ready"
    && position.source_id === state.filters.sourceId
    && position.recent_payout_date
  ) {
    const display = financialPositionDisplay(position);
    const payoutDate = String(position.recent_payout_date).slice(0, 10);
    const payoutAmount = formatPositionAmount(display.currency, display.values?.recent_payout);
    const comparisonPayoutAmount = `${display.comparisonCurrency} ${formatMoney(moneyNumber(display.comparisonValues?.recent_payout))}`;
    container.innerHTML = `
      <div class="recent-payout-row">
        <div class="recent-payout-date">
          <span>${escapeHtml(payoutDate)}</span>
          <strong>Recent Payout</strong>
        </div>
        <div class="recent-payout-main">
          <strong>${escapeHtml(position.source_name || "Amazon")}</strong>
          <div class="marketplace-badges">
            <span class="marketplace-badge provider amazon">Amazon</span>
            <span class="marketplace-badge country">${escapeHtml(position.marketplace_name || "Marketplace")}</span>
          </div>
        </div>
        <div class="recent-payout-amount">
          <strong class="positive">${escapeHtml(payoutAmount)}</strong>
          <span>${escapeHtml(comparisonPayoutAmount)} · ${escapeHtml(position.recent_payout_status || "Closed settlement")}</span>
        </div>
      </div>`;
    return;
  }
  const statement = state.sellerStatement;
  if (statement?.recent_payout_amount) {
    const currency = detectStatementCurrency(statement) || "USD";
    const payoutDate = statement.recent_payout_date || "Latest payout";
    const payoutAmount = `${currency} ${formatMoney(statementUsdNumber(statement, statement.recent_payout_amount))}`;
    container.innerHTML = `
      <div class="recent-payout-row">
        <div class="recent-payout-date">
          <span>${escapeHtml(payoutDate)}</span>
          <strong>Recent Payout</strong>
        </div>
        <div class="recent-payout-main">
          <strong>Seller Central Statement View</strong>
          <div class="marketplace-badges">
            <span class="marketplace-badge provider amazon">Amazon</span>
            <span class="marketplace-badge country">Statement</span>
          </div>
        </div>
        <div class="recent-payout-amount">
          <strong class="positive">${escapeHtml(payoutAmount)}</strong>
          <span>${statement.request_payment_enabled ? "Request payment available" : "Snapshot balance"}</span>
        </div>
      </div>`;
    return;
  }
  const toSortable = (s) => {
    if (!s) return "";
    // DD.MM.YYYY HH:MM:SS UTC → YYYY-MM-DD HH:MM:SS for correct sort
    const m = s.match(/^(\d{2})\.(\d{2})\.(\d{4})\s+(\d{2}:\d{2}:\d{2})/);
    return m ? `${m[3]}-${m[2]}-${m[1]} ${m[4]}` : s;
  };
  const rows = [...state.marketplaceActivity]
    .sort((a, b) => toSortable(b.posted_at).localeCompare(toSortable(a.posted_at)))
    .slice(0, 3);
  if (!rows.length) {
    container.innerHTML = `<div class="empty-state compact">Upload a marketplace transaction report to show activity.</div>`;
    return;
  }
  container.innerHTML = rows
    .map(
      (row) => {
        const shortDate = (row.posted_at || "-").replace(/\s+\d{2}:\d{2}:\d{2}\s*UTC/i, "").trim();
        const amtNum = moneyNumber(row.total);
        const amtFormatted = amtNum !== 0
          ? `${row.currency || "USD"} ${Math.abs(amtNum).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
          : (row.total || "-");
        const amtClass = amtNum < 0 ? "negative" : amtNum > 0 ? "positive" : "";
        return `
        <div class="recent-payout-row">
          <div class="recent-payout-date">
            <span>${escapeHtml(shortDate)}</span>
            <strong>${escapeHtml(row.type || "Transaction")}</strong>
          </div>
          <div class="recent-payout-main">
            <strong>${escapeHtml(row.marketplace || "Marketplace activity")}</strong>
            ${marketplaceBadgesHtml(row.marketplace)}
          </div>
          <div class="recent-payout-amount">
            <strong class="${amtClass}">${escapeHtml(amtFormatted)}</strong>
            <span>${escapeHtml(row.status || "-")}</span>
          </div>
        </div>`;
      }
    )
    .join("");
}

function renderWeeklyPayable() {
  const outstandingEl  = document.getElementById("weekly-outstanding-amount");
  const outCountEl     = document.getElementById("weekly-outstanding-count");
  const thisWeekEl     = document.getElementById("weekly-thisweek-amount");
  const thisWeekRange  = document.getElementById("weekly-thisweek-range");
  const overdueEl      = document.getElementById("weekly-overdue-amount");
  const overdueCountEl = document.getElementById("weekly-overdue-count");
  const rowsEl         = document.getElementById("weekly-payable-rows");
  if (!outstandingEl || !rowsEl) return;

  const payouts = state.unmatchedExpected || [];
  if (!payouts.length) {
    rowsEl.innerHTML = `<div class="empty-state compact">No outstanding Amazon settlements.</div>`;
    [outstandingEl, thisWeekEl, overdueEl].forEach(el => { if (el) el.textContent = "USD 0.00"; });
    return;
  }

  // Compute Mon–Sun window for current week
  const today = new Date();
  const dayOfWeek = today.getDay(); // 0=Sun,1=Mon,...
  const diffToMon = (dayOfWeek === 0) ? -6 : 1 - dayOfWeek;
  const monday = new Date(today); monday.setDate(today.getDate() + diffToMon); monday.setHours(0,0,0,0);
  const sunday = new Date(monday); sunday.setDate(monday.getDate() + 6); sunday.setHours(23,59,59,999);

  const fmtDate = d => d.toLocaleDateString("en-CA", { month: "short", day: "numeric" });
  const weekLabel = `${fmtDate(monday)} – ${fmtDate(sunday)}`;
  if (thisWeekRange) thisWeekRange.textContent = weekLabel;

  let totalOutstanding = 0, totalThisWeek = 0, totalOverdue = 0;
  let overdueCount = 0, thisWeekCount = 0;

  const sorted = [...payouts].sort((a, b) => (a.expected_date || "").localeCompare(b.expected_date || ""));

  sorted.forEach(p => {
    const amt = parseFloat(p.amount) || 0;
    const dt = p.expected_date ? new Date(p.expected_date + "T00:00:00") : null;
    totalOutstanding += amt;
    if (dt) {
      if (dt >= monday && dt <= sunday) { totalThisWeek += amt; thisWeekCount++; }
      else if (dt < monday) { totalOverdue += amt; overdueCount++; }
    }
  });

  const fmt = v => `USD ${Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  outstandingEl.textContent  = fmt(totalOutstanding);
  thisWeekEl.textContent     = thisWeekCount ? fmt(totalThisWeek) : "USD 0.00";
  overdueEl.textContent      = overdueCount  ? fmt(totalOverdue)  : "USD 0.00";
  if (outCountEl)     outCountEl.textContent     = `${payouts.length} settlement${payouts.length !== 1 ? "s" : ""} pending`;
  if (overdueCountEl) overdueCountEl.textContent = overdueCount ? `${overdueCount} past deposit date` : "All on schedule";

  // Row list
  rowsEl.innerHTML = sorted.map(p => {
    const dt = p.expected_date ? new Date(p.expected_date + "T00:00:00") : null;
    const isThisWeek = dt && dt >= monday && dt <= sunday;
    const isOverdue  = dt && dt < monday;
    const tag = isThisWeek ? "this-week" : isOverdue ? "overdue" : "upcoming";
    const tagLabel = isThisWeek ? "This Week" : isOverdue ? "Overdue" : "Upcoming";
    const amt = parseFloat(p.amount) || 0;
    return `
      <div class="weekly-row">
        <span class="weekly-row-date">${escapeHtml(p.expected_date || "—")}</span>
        <span class="weekly-row-ref">${escapeHtml(p.reference || p.payout_id || "—")}</span>
        <span class="weekly-row-tag ${tag}">${tagLabel}</span>
        <span class="weekly-row-amount">${escapeHtml(fmt(amt))}</span>
      </div>`;
  }).join("");
}

function renderNetProceedsBreakdown() {
  const s = state.summary;
  const totalEl = document.getElementById("net-proceeds-total");
  const barsEl  = document.getElementById("net-proceeds-bars");
  const periodEl = document.getElementById("net-proceeds-period");
  if (!totalEl || !barsEl) return;

  const position = state.amazonFinancialPosition;
  if (position?.mode === "amazon_open_balances") {
    totalEl.textContent = Object.entries(position.totals_by_currency || {})
      .map(([currency, amount]) => `${currency} ${formatMoney(moneyNumber(amount))}`)
      .join(" · ") || "Unavailable";
    if (periodEl) periodEl.textContent = `${position.group_count || 0} unique Amazon-reported open balance group(s)`;
    const balances = position.balances || [];
    const max = Math.max(...balances.map((row) => Math.abs(moneyNumber(row.usd_amount))), 1);
    barsEl.innerHTML = balances.map((row) => `
      <div class="proceeds-row">
        <span class="proceeds-label">${escapeHtml(row.currency)} · ${escapeHtml(row.financial_event_group_id)}</span>
        <div class="proceeds-bar-track"><div class="proceeds-bar proceeds-bar-positive" style="width:${Math.round(Math.abs(moneyNumber(row.usd_amount)) / max * 100)}%"></div></div>
        <span class="proceeds-value positive">${escapeHtml(row.currency)} ${escapeHtml(row.amount)} · USD ${escapeHtml(row.usd_amount)}</span>
      </div>`).join("");
    return;
  }
  if (
    position?.status === "ready"
    && position.source_id === state.filters.sourceId
  ) {
    const display = financialPositionDisplay(position);
    const formatPosition = (value) => formatPositionAmount(display.currency, Math.abs(moneyNumber(value)));
    const formatComparisonPosition = (value) => `${display.comparisonCurrency} ${Math.abs(moneyNumber(value)).toLocaleString("en-US", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    })}`;
    const fundsAvailableKnown = display.values.funds_available !== null
      && display.values.funds_available !== undefined;
    const totalKey = fundsAvailableKnown ? "funds_available" : "standard_balance";
    totalEl.textContent = `${formatPosition(display.values[totalKey])} · ${formatComparisonPosition(display.comparisonValues[totalKey])}`;
    if (periodEl) {
      periodEl.textContent = `Open settlement from ${String(position.settlement_period_start || "").slice(0, 10)} · Current Amazon SP-API statement`;
    }
    const rows = [
      { label: "Beginning Balance", key: "beginning_balance", value: moneyNumber(display.values.beginning_balance), positive: true },
      { label: "Standard Orders Balance", key: "standard_balance", value: moneyNumber(display.values.standard_balance), positive: true },
      ...(fundsAvailableKnown ? [
        { label: "Account Level Reserve", key: "reserve_balance", value: moneyNumber(display.values.reserve_balance), positive: false },
        { label: "Funds Available", key: "funds_available", value: moneyNumber(display.values.funds_available), positive: true },
      ] : []),
      { label: "Deferred Transactions", key: "deferred_balance", value: moneyNumber(display.values.deferred_balance), positive: true },
      { label: "Total Balance", key: "total_balance", value: moneyNumber(display.values.total_balance), positive: true },
    ];
    const maxAbs = Math.max(...rows.map((row) => Math.abs(row.value)), 1);
    barsEl.innerHTML = `<div class="empty-state compact"><strong>Amazon balance components</strong><br>Reserve-adjusted funds are not calculated because the current reserve is unavailable.</div>` + rows.map((row) => {
      const pct = Math.round((Math.abs(row.value) / maxAbs) * 100);
      const cls = row.positive ? "proceeds-bar-positive" : "proceeds-bar-negative";
      const valCls = row.positive ? "positive" : "negative";
      const sign = row.value > 0 ? "+" : row.value < 0 ? "−" : "";
      return `
        <div class="proceeds-row">
          <span class="proceeds-label">${escapeHtml(row.label)}</span>
          <div class="proceeds-bar-track">
            <div class="proceeds-bar ${cls}" style="width:${pct}%"></div>
          </div>
          <span class="proceeds-value ${valCls}">${sign}${escapeHtml(formatPosition(row.value))} · ${sign}${escapeHtml(formatComparisonPosition(display.comparisonValues[row.key]))}</span>
        </div>`;
    }).join("");
    return;
  }

  const statement = state.sellerStatement;
  if (statement?.net_proceeds || statement?.scheduled_transfer_amount) {
    const cur = detectStatementCurrency(statement) || "USD";
    const fmtStatement = v => `${cur} ${Math.abs(statementUsdNumber(statement, v)).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
    const netValue = statement.net_proceeds || statement.scheduled_transfer_amount;
    const net = statementUsdNumber(statement, netValue);
    totalEl.textContent = fmtStatement(netValue);
    if (periodEl) {
      const transfer = statement.scheduled_transfer_initiation_date
        ? `Transfer ${fmtStatement(statement.scheduled_transfer_amount || netValue)} scheduled ${statement.scheduled_transfer_initiation_date}`
        : "Seller Central Statement View";
      periodEl.textContent = `${statement.settlement_period || "Open settlement period"} · ${transfer}`;
    }

    const rows = [
      { label: "Beginning Balance", value: statementUsdNumber(statement, statement.beginning_balance), positive: true },
      { label: "Sales", value: statementUsdNumber(statement, statement.sales), positive: true },
      { label: "Refunds", value: statementUsdNumber(statement, statement.refunds), positive: false },
      { label: "Expenses", value: statementUsdNumber(statement, statement.expenses), positive: false },
      { label: "Account Level Reserve", value: statementUsdNumber(statement, statement.account_level_reserve), positive: false },
      { label: "Product Charges", value: statementUsdNumber(statement, statement.product_charges), positive: true },
      { label: "Tax", value: statementUsdNumber(statement, statement.tax), positive: true },
      { label: "Refunded Expenses", value: statementUsdNumber(statement, statement.refunded_expenses), positive: true },
      { label: "Refunded Sales", value: statementUsdNumber(statement, statement.refunded_sales), positive: false },
      { label: "FBA Inventory Fees", value: statementUsdNumber(statement, statement.fba_inventory_fees), positive: false },
      { label: "Cost of Advertising", value: statementUsdNumber(statement, statement.cost_of_advertising), positive: false },
      { label: "Amazon Fees", value: statementUsdNumber(statement, statement.amazon_fees), positive: false },
      { label: "Other", value: statementUsdNumber(statement, statement.negative_other), positive: false },
    ].filter((row) => row.value !== 0);

    const maxAbs = Math.max(...rows.map((row) => Math.abs(row.value)), 1);
    const transferNotice = statement.scheduled_transfer_amount
      ? `<div class="empty-state compact"><strong>Scheduled Transfer</strong><br>${escapeHtml(fmtStatement(statement.scheduled_transfer_amount))} initiates ${escapeHtml(statement.scheduled_transfer_initiation_date || "when Amazon releases it")}. Transfers can take 3-5 business days and the amount can change.</div>`
      : "";

    barsEl.innerHTML = transferNotice + rows.map((row) => {
      const pct = Math.round((Math.abs(row.value) / maxAbs) * 100);
      const cls = row.positive ? "proceeds-bar-positive" : "proceeds-bar-negative";
      const valCls = row.positive ? "positive" : "negative";
      const sign = row.value > 0 ? "+" : "";
      return `
        <div class="proceeds-row">
          <span class="proceeds-label">${escapeHtml(row.label)}</span>
          <div class="proceeds-bar-track">
            <div class="proceeds-bar ${cls}" style="width:${pct}%"></div>
          </div>
          <span class="proceeds-value ${valCls}">${sign}${escapeHtml(fmtStatement(row.value))}</span>
        </div>`;
    }).join("");
    return;
  }

  const net    = moneyNumber(s.total_expected_payouts);
  const sales  = moneyNumber(s.marketplace_order_amount);
  const tax    = moneyNumber(s.marketplace_tax_amount);
  const refund = moneyNumber(s.marketplace_refund_amount);
  const fees   = moneyNumber(s.marketplace_selling_fees);
  const fba    = moneyNumber(s.marketplace_fba_fees);
  const other  = moneyNumber(s.marketplace_other_fees);

  if (!net) {
    barsEl.innerHTML = `<div class="empty-state compact">Upload settlement files to see the Net Proceeds breakdown.</div>`;
    return;
  }

  totalEl.textContent = `USD ${Math.abs(net).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  if (periodEl) periodEl.textContent = `${s.expected_payout_records || 0} settlement(s) · All loaded periods`;

  const reimb      = moneyNumber(s.marketplace_reimbursements);
  const withheld   = moneyNumber(s.marketplace_withheld_tax);
  const failedTxfr = moneyNumber(s.marketplace_failed_transfers);
  const reserves   = moneyNumber(s.marketplace_reserve_adjustments);
  const uncat      = moneyNumber(s.marketplace_uncategorised);

  // Warn if there are failed transfers
  const barsEl2 = document.getElementById("net-proceeds-bars");
  if (failedTxfr !== 0 && barsEl2) {
    const existing = barsEl2.querySelector(".failed-transfer-warning");
    if (!existing) {
      const warn = document.createElement("div");
      warn.className = "failed-transfer-warning";
      warn.innerHTML = `⚠️ <strong>Failed Transfer: USD ${Math.abs(failedTxfr).toLocaleString("en-US",{minimumFractionDigits:2})}</strong> — Amazon attempted a bank transfer that was unsuccessful. Verify with your bank.`;
      barsEl2.parentElement.insertBefore(warn, barsEl2);
    }
  }

  const rows = [
    { label: "Gross Sales (Principal)",      value: sales,      positive: true  },
    { label: "Tax Collected",                value: tax,        positive: true  },
    { label: "Reserve Releases & Adjustments", value: reserves, positive: reserves > 0 },
    { label: "FBA Reimbursements",           value: reimb,      positive: true  },
    { label: "Refunds",                      value: refund,     positive: false },
    { label: "Amazon Commissions",           value: fees,       positive: false },
    { label: "FBA Fees",                     value: fba,        positive: false },
    { label: "Other Fees",                   value: other,      positive: false },
    { label: "Withheld Tax",                 value: withheld,   positive: false },
    { label: "Failed Transfers",             value: failedTxfr, positive: true, warn: true },
    { label: "Other Adjustments",            value: uncat,      positive: uncat > 0 },
  ].filter(r => r.value !== 0);

  const maxAbs = Math.max(...rows.map(r => Math.abs(r.value)), 1);

  barsEl.innerHTML = rows.map(row => {
    const pct = Math.round((Math.abs(row.value) / maxAbs) * 100);
    const sign = row.value > 0 ? "+" : "";
    const cls  = row.warn ? "proceeds-bar-warn" : (row.positive ? "proceeds-bar-positive" : "proceeds-bar-negative");
    const valCls = row.warn ? "warn" : (row.positive ? "positive" : "negative");
    const formatted = `USD ${Math.abs(row.value).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
    return `
      <div class="proceeds-row${row.warn ? " proceeds-row-warn" : ""}">
        <span class="proceeds-label">${escapeHtml(row.label)}</span>
        <div class="proceeds-bar-track">
          <div class="proceeds-bar ${cls}" style="width:${pct}%"></div>
        </div>
        <span class="proceeds-value ${valCls}">${sign}${formatted}</span>
      </div>`;
  }).join("");
}

function renderOverviewPanels() {
  renderSourceOverview();
  renderOverviewExceptionSnapshot();
  renderOverviewActivity();
  renderBackfillProgress();
}

function renderBackfillProgress() {
  const panel = document.getElementById("overview-backfill-progress");
  const container = document.getElementById("overview-backfill-progress-list");
  const diagnostics = state.amazonTransactionDiagnostics;
  panel.hidden = !state.session?.is_admin || !diagnostics;
  if (panel.hidden) return;
  const incremental = diagnostics.incremental_collection || {};
  const canaries = Array.isArray(diagnostics.validation_canaries) ? diagnostics.validation_canaries : [];
  const historical = Array.isArray(diagnostics.historical_backfills) ? diagnostics.historical_backfills : [];
  const latestIncremental = Array.isArray(incremental.runs) ? incremental.runs[0] : null;
  const incrementalHtml = `<h3>Incremental collection</h3><article class="source-overview-row">
    <div class="source-overview-name"><strong>${escapeHtml(incremental.status || "NOT_STARTED")}</strong><span>Recent/current observed coverage only</span></div>
    <div><span>Last successful run</span><strong>${escapeHtml(formatUtcCoverage(incremental.last_successful_run))}</strong></div>
    <div><span>Recent checkpoint</span><strong>${escapeHtml(formatUtcCoverage(diagnostics.checkpoints?.map((row) => row.coverage_end).filter(Boolean).sort().at(-1)))}</strong></div>
    <div><span>Processed</span><strong>${Number(latestIncremental?.pages_completed || 0).toLocaleString()} pages · ${Number(latestIncremental?.transactions_received || 0).toLocaleString()} transactions</strong></div>
    <div><span>Errors / freshness</span><strong>${escapeHtml(latestIncremental?.last_error || "None")} · ${escapeHtml(formatUtcCoverage(latestIncremental?.updated_at))}</strong></div>
  </article>`;
  const canaryHtml = `<h3>Validation canaries</h3>${canaries.length ? canaries.map((row) => `<article class="source-overview-row">
    <div class="source-overview-name"><strong>Canary ${escapeHtml(row.status || "unknown")}</strong><span>${escapeHtml(row.marketplace_name)} · ${escapeHtml(row.currency)} · ${escapeHtml(row.transaction_status)}</span></div>
    <div><span>Coverage window</span><strong>${escapeHtml(formatUtcCoverage(row.overall_start))} — ${escapeHtml(formatUtcCoverage(row.overall_end))}</strong></div>
    <div><span>Result</span><strong>${row.status === "completed" ? "Completed successfully" : escapeHtml(row.status)}</strong></div>
    <div><span>Deduplication</span><strong>${Number(row.status_conflicts || 0).toLocaleString()} conflicts</strong></div>
    <div><span>Storage growth</span><strong>${Number(row.database_growth_bytes || 0).toLocaleString()} bytes</strong></div>
  </article>`).join("") : '<div class="empty-state">No validation canary has been recorded.</div>'}
  <p class="muted">Canary runs validate collection and deduplication. They do not represent historical coverage.</p>`;
  const historicalHtml = `<h3>Historical backfill</h3>${historical.length ? historical.map((row) => `<article class="source-overview-row">
    <div class="source-overview-name"><strong>${escapeHtml(row.marketplace_name || row.marketplace_id || "Marketplace")}</strong><span>${escapeHtml(row.transaction_status || "Status unavailable")}</span></div>
    <div><span>Status</span><strong>${escapeHtml(row.status || "not started")}</strong></div>
    <div><span>Requested range</span><strong>${escapeHtml(formatUtcCoverage(row.overall_start))} — ${escapeHtml(formatUtcCoverage(row.overall_end))}</strong></div>
    <div><span>Completed range</span><strong>${escapeHtml(formatUtcCoverage(row.completed_start))} — ${escapeHtml(formatUtcCoverage(row.completed_end))}</strong></div>
    <div><span>Current slice</span><strong>${escapeHtml(formatUtcCoverage(row.current_slice_start))} — ${escapeHtml(formatUtcCoverage(row.current_slice_end))}</strong></div>
    <div><span>Slices</span><strong>${Number(row.completed_slice_count || 0)} complete · ${Number(row.pending_slice_count || 0)} pending · ${Number(row.failed_slice_count || 0)} failed</strong></div>
    <div><span>Processed</span><strong>${Number(row.pages_completed || 0).toLocaleString()} pages · ${Number(row.transactions_received || 0).toLocaleString()} transactions</strong></div>
    <div><span>Conflicts / DB growth</span><strong>${Number(row.status_conflicts || 0).toLocaleString()} · ${Number(row.database_growth_bytes || 0).toLocaleString()} bytes</strong></div>
    <div><span>Last update</span><strong>${escapeHtml(formatUtcCoverage(row.updated_at))}</strong></div>
    <div><span>Pause / error</span><strong>${escapeHtml(row.pause_reason || row.last_error || "None")}</strong></div>
  </article>`).join("") : '<div class="empty-state">Historical backfill has not started. Current transaction visibility remains available with partial coverage.</div>'}`;
  container.innerHTML = incrementalHtml + canaryHtml + historicalHtml;
}

function renderCashflowProofSummary() {
  const container = document.getElementById("cashflow-proof-summary");
  const proof = state.cashflowProof;
  if (!container) return;
  if (!proof?.latest_run) {
    container.innerHTML = `<div class="empty-state">No proof run yet. Run the 90-day proof after Amazon settlement data is synced.</div>`;
    return;
  }
  const amounts = (values) => Object.entries(values || {})
    .map(([currency, amount]) => `${currency} ${formatMoney(moneyNumber(amount))}`)
    .join(", ") || "None";
  container.innerHTML = `<article class="source-overview-row">
    <div class="source-overview-name"><strong>${escapeHtml(proof.source_coverage)}</strong><span>Source coverage</span></div>
    <div><span>Verified</span><strong>${escapeHtml(amounts(proof.verified_amounts))}</strong></div>
    <div><span>Pending</span><strong>${escapeHtml(amounts(proof.pending_amounts))}</strong></div>
    <div><span>Unexplained variance</span><strong>${escapeHtml(amounts(proof.unexplained_variances))}</strong></div>
    <div><span>Evidence checked</span><strong>${escapeHtml(proof.last_checked_at ? new Date(proof.last_checked_at).toLocaleString() : "Never")}</strong></div>
  </article>`;
}

function sourceName(sourceId) {
  return (state.amazonSources || []).find((source) => source.id === sourceId)?.name || "Unassigned source";
}

function renderSourceOverview() {
  const container = document.getElementById("source-overview-list");
  const sources = filteredSources();
  if (!sources.length) {
    container.innerHTML = `<div class="empty-state">No source exists in this scope. Add an Amazon source from the Sources page.</div>`;
    return;
  }
  container.innerHTML = sources.map((source) => {
    const activity = scopedRows(state.marketplaceActivity, { sourceId: source.id });
    const freshness = sourceFreshness(source);
    const position = state.amazonFinancialPositions[source.id]?.status === "ready"
      ? state.amazonFinancialPositions[source.id]
      : null;
    const aggregate = position?.mode === "amazon_open_balances";
    const payoutLabel = position ? "Amazon-reported open balance" : "Amazon-reported open balance";
    const payoutValue = aggregate
      ? Object.entries(position.totals_by_currency || {}).map(([currency, amount]) => `${currency} ${formatMoney(moneyNumber(amount))}`).join(" · ")
      : position
        ? formatPositionAmount(position.source_currency, position.source_amounts?.standard_balance)
        : "Unavailable";
    return `<article class="source-overview-row">
      <div class="source-overview-name"><strong>${escapeHtml(source.name)}</strong><span>${escapeHtml(source.status || "Setup incomplete")}</span></div>
      <div><span>Freshness</span><strong class="freshness-${escapeAttribute(freshness.tone)}">${escapeHtml(freshness.label)}</strong></div>
      <div><span>Activity</span><strong>${activity.length.toLocaleString()} rows</strong></div>
      <div><span>${escapeHtml(payoutLabel)}</span><strong>${escapeHtml(payoutValue)}</strong></div>
      <button class="source-scope-button secondary-button" type="button" data-source-id="${escapeAttribute(source.id)}">View source</button>
    </article>`;
  }).join("");
  container.querySelectorAll(".source-scope-button").forEach((button) => button.addEventListener("click", async () => {
    state.filters.sourceId = button.dataset.sourceId;
    state.filters.currency = "";
    await loadAmazonFinancialPosition();
    persistScopeFilters();
    render();
  }));
}

function renderOverviewExceptionSnapshot() {
  const position = state.amazonFinancialPosition?.source_id === state.filters.sourceId
    ? state.amazonFinancialPosition
    : null;
  const quality = state.dataQuality || [];
  const items = [
    ["Open settlement groups", Number(position?.openSettlementGroupCount ?? position?.group_count ?? 0), "Amazon financial event groups currently being processed."],
    ["Data-quality checks", quality.length, "Validation items from the latest report generation"],
  ];
  document.getElementById("overview-exception-snapshot").innerHTML = items.map(([label, count, note]) =>
    `<div class="exception-snapshot-row"><strong>${Number(count).toLocaleString()}</strong><div><span>${escapeHtml(label)}</span><small>${escapeHtml(note)}</small></div></div>`
  ).join("");
}

function renderOverviewActivity() {
  const container = document.getElementById("overview-activity-list");
  if (state.marketplaceActivityStatus?.status === "unavailable") {
    setText("overview-activity-count", "Unavailable");
    container.innerHTML = `<div class="empty-state">Marketplace activity is temporarily unavailable. Other financial data remains available.</div>`;
    return;
  }
  const rows = scopedRows(state.marketplaceActivity)
    .slice()
    .sort((a, b) => (rowDate(b)?.getTime() || 0) - (rowDate(a)?.getTime() || 0));
  setText("overview-activity-count", `${rows.length.toLocaleString()} rows`);
  if (!rows.length) {
    container.innerHTML = `<div class="empty-state">No source-backed settlement activity matches the selected filters.</div>`;
    return;
  }
  const visible = rows.slice(0, 20);
  container.innerHTML = `<div class="activity-row activity-head"><span>Source</span><span>Marketplace</span><span>Date</span><span>Type</span><span>Amount</span></div>${visible.map((row) =>
    `<div class="activity-row">
      <strong data-label="Source">${escapeHtml(sourceName(row.source_id))}</strong>
      <span data-label="Marketplace">${escapeHtml(row.marketplace || "—")}</span>
      <span data-label="Date">${escapeHtml(row.posted_at || "—")}</span>
      <span data-label="Type">${escapeHtml(row.type || row.status || "Reported")}</span>
      <strong data-label="Amount">${escapeHtml(`${row.currency || ""} ${formatMoney(moneyNumber(row.total))}`.trim())}</strong>
    </div>`
  ).join("")}`;
}

function renderOverviewTransactions() {
  const activityRows = state.marketplaceActivity.map((row) => ({
    ...row,
    _group_key: row.settlement_id || fileBasename(row.source_file) || row.posted_at?.slice(0, 10) || "Unsettled",
    _group_type: "marketplace",
    status: `${row.type || "Transaction"} · ${row.status || "Reported"}`,
    title: row.marketplace || "Marketplace activity",
    amount: row.total,
    currency: row.currency || "USD",
    date: row.posted_at,
    reference: row.settlement_id || row.order_id,
    source_file: row.source_file,
  }));

  const matchedRows = state.matched.map((row) => ({
    ...row,
    _group_key: row.reference || row.receipt_id || "Matched",
    _group_type: "matched",
    status: "Matched",
    title: row.marketplace || row.bank_account,
    date: row.receipt_date || row.expected_date,
  }));

  const pendingRows = state.unmatchedExpected.map((row) => ({
    ...row,
    _group_key: "Pending payouts",
    _group_type: "pending",
    status: "Pending",
    title: row.marketplace || row.entity,
    date: row.expected_date,
  }));

  const allRows = [...activityRows, ...matchedRows, ...pendingRows];
  const container = document.getElementById("overview-transactions");

  if (!allRows.length) {
    container.innerHTML = `<div class="empty-state">No transaction records available. Upload client source data to populate this tab.</div>`;
    return;
  }

  const groups = new Map();
  allRows.forEach((row) => {
    const key = row._group_key || "Other";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(row);
  });

  const groupCount = groups.size;
  let html = `<div class="row-count">${allRows.length.toLocaleString()} transactions across ${groupCount} settlement run${groupCount !== 1 ? "s" : ""}</div>`;
  let isFirst = true;
  groups.forEach((rows, key) => {
    const total = rows.reduce((sum, r) => sum + (moneyNumber(r.amount) || 0), 0);
    const totalLabel = total !== 0 ? `USD ${Math.abs(total).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}` : `${rows.length} record${rows.length !== 1 ? "s" : ""}`;
    const type = rows[0]?._group_type || "marketplace";
    const typeLabel = type === "matched" ? "Confirmed" : type === "pending" ? "Pending" : "Settlement";
    const expanded = false;
    const groupId = `txn-group-${CSS.escape(key)}`;
    html += `
      <div class="accordion-group" data-group="${escapeAttribute(key)}">
        <button class="accordion-header${expanded ? " open" : ""}" type="button" aria-expanded="${expanded}" aria-controls="${escapeAttribute(groupId)}">
          <div class="accordion-header-left">
            <span class="accordion-type-tag">${escapeHtml(typeLabel)}</span>
            <strong>${escapeHtml(key)}</strong>
          </div>
          <div class="accordion-header-right">
            <span class="accordion-count">${rows.length.toLocaleString()} rows</span>
            <span class="accordion-total">${escapeHtml(totalLabel)}</span>
            <span class="accordion-chevron" aria-hidden="true"></span>
          </div>
        </button>
        <div id="${escapeAttribute(groupId)}" class="accordion-body${expanded ? " open" : ""}" aria-hidden="${!expanded}">
          <div class="transaction-list">
            ${rows.slice(0, PAGE_SIZE).map(transactionListItem).join("")}
          </div>
          ${rows.length > PAGE_SIZE ? `<div class="accordion-load-more" data-shown="${PAGE_SIZE}" data-group="${escapeAttribute(key)}"><button type="button" class="secondary-button">Load ${Math.min(PAGE_SIZE, rows.length - PAGE_SIZE).toLocaleString()} more <span class="muted">(${(rows.length - PAGE_SIZE).toLocaleString()} remaining)</span></button></div>` : ""}
        </div>
      </div>`;
    isFirst = false;
  });

  container.innerHTML = html;

  container.querySelectorAll(".accordion-header").forEach((btn) => {
    btn.addEventListener("click", () => {
      const isOpen = btn.classList.contains("open");
      const body = document.getElementById(btn.getAttribute("aria-controls"));
      btn.classList.toggle("open", !isOpen);
      btn.setAttribute("aria-expanded", String(!isOpen));
      body.classList.toggle("open", !isOpen);
      body.setAttribute("aria-hidden", String(isOpen));
    });
  });

  container.querySelectorAll(".accordion-load-more button").forEach((btn) => {
    btn.addEventListener("click", () => {
      const wrapper = btn.closest(".accordion-load-more");
      const groupKey = wrapper.dataset.group;
      const shown = Number(wrapper.dataset.shown);
      const groupRows = groups.get(groupKey) || [];
      const batch = groupRows.slice(shown, shown + PAGE_SIZE);
      const listEl = wrapper.previousElementSibling;
      listEl.insertAdjacentHTML("beforeend", batch.map(transactionListItem).join(""));
      const newShown = shown + batch.length;
      const newRemaining = groupRows.length - newShown;
      if (newRemaining > 0) {
        wrapper.dataset.shown = newShown;
        btn.innerHTML = `Load ${Math.min(PAGE_SIZE, newRemaining).toLocaleString()} more <span class="muted">(${newRemaining.toLocaleString()} remaining)</span>`;
      } else {
        wrapper.remove();
      }
    });
  });
}

function renderTransactionList(id, rows, emptyText) {
  const container = document.getElementById(id);
  container.classList.add("transaction-list");
  if (!rows.length) {
    container.innerHTML = `<div class="empty-state">${escapeHtml(emptyText)}</div>`;
    return;
  }
  renderCardList(container, rows, transactionListItem, "transaction");
}

function formatTransactionDate(raw) {
  if (!raw) return "-";
  const MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];

  // Amazon Flat File V2 format: "DD.MM.YYYY HH:MM:SS UTC"
  const ddmm = raw.match(/^(\d{2})\.(\d{2})\.(\d{4})(?:\s+(\d{2}):(\d{2}))?/);
  if (ddmm) {
    const [, dd, mm, yyyy, hh, min] = ddmm;
    const mon = MONTHS[parseInt(mm, 10) - 1] || mm;
    return hh ? `${mon} ${parseInt(dd)} · ${hh}:${min}` : `${mon} ${parseInt(dd)}, ${yyyy}`;
  }

  // ISO format: 2026-05-26T05:21:09Z or 2026-05-26
  if (/^\d{4}-\d{2}-\d{2}/.test(raw)) {
    try {
      const d = new Date(raw);
      if (!isNaN(d)) {
        return d.toLocaleDateString("en-CA", { month: "short", day: "numeric", year: "numeric" })
          + " · " + d.toLocaleTimeString("en-CA", { hour: "numeric", minute: "2-digit", hour12: true });
      }
    } catch { /* fall through */ }
  }

  // Amazon old format: "MAY 26, 2026 5:21:09 A.M. PDT"
  const m = raw.match(/([A-Z]{3})\s+(\d{1,2}),\s*(\d{4})\s+(\d{1,2}):(\d{2})(?::\d{2})?\s*(A\.?M\.?|P\.?M\.?)/i);
  if (m) {
    const [, mon, day, year, hr, min, ampm] = m;
    const cleanAmpm = ampm.replace(/\./g, "").toUpperCase();
    return `${mon[0]}${mon.slice(1).toLowerCase()} ${parseInt(day)} · ${hr}:${min} ${cleanAmpm}`;
  }
  return raw.slice(0, 16);
}

function fileBasename(filePath) {
  if (!filePath) return "";
  return filePath.split(/[\\/]/).pop() || filePath;
}

function transactionListItem(row) {
  const amountText = [row.currency, row.amount].filter(Boolean).join(" ") || "-";
  const badgeSource = row.marketplace || row.title || row.source_file || "";
  const statusText = row.status || "Record";
  return `
    <article class="transaction-list-item">
      <div class="transaction-date">
        <span>${escapeHtml(formatTransactionDate(row.date))}</span>
        <strong>${escapeHtml(row.order_id || row.reference || "—")}</strong>
      </div>
      <div class="transaction-main">
        <div>
          <strong>${escapeHtml(row.title || "Marketplace activity")}</strong>
          <span>${escapeHtml(statusText)}</span>
        </div>
        ${marketplaceBadgesHtml(badgeSource)}
      </div>
      <div class="transaction-amount">
        <strong>${escapeHtml(amountText)}</strong>
      </div>
    </article>
  `;
}

function renderOverviewReceivables() {
  const deferredAmount = moneyNumber(state.summary.marketplace_deferred_amount);
  const rows = [];
  if (deferredAmount) {
    const sourceName = state.tenant.sources?.marketplaces?.[0]?.name || "Marketplace";
    rows.push({
      status: "Deferred — not yet paid out",
      title: `${sourceName} deferred balance`,
      amount: formatMoney(deferredAmount),
      currency: state.tenant.currencies?.[0] || "USD",
      date: "Current transaction report",
      reference: `${state.summary.marketplace_transaction_records} transaction records`,
      note: "Deferred balance is Amazon evidence of cash expected to be paid out.",
    });
  }
  rows.push(...state.unmatchedExpected.map((row) => ({
    ...row,
    status: formatHeader(row.exception_type || "Receivable"),
    title: row.marketplace || row.entity,
    date: row.expected_date,
    note: "Expected customer or marketplace inflow awaiting confirmation.",
  })));
  renderOverviewRecordCards(
    "overview-receivables",
    rows,
    "No receivables are available yet. Upload marketplace, invoice, collections, or expected inflow data."
  );
}

function renderOverviewPayables() {
  const rows = (state.tenant.client_setup?.planned_outflows || []).map((outflow) => ({
    status: outflow.frequency || "Planned",
    title: outflow.name,
    amount: !outflow.amount || outflow.amount.trim() === "" ? "To be confirmed" : outflow.amount,
    currency: (!outflow.amount || outflow.amount.trim() === "") ? "" : (state.tenant.currencies?.[0] || ""),
    date: outflow.frequency ? `${outflow.frequency} payment` : "Scheduled outflow",
    reference: outflow.category || "Operating outflow",
    source: "Settings → Planned Outflows",
    note: "Client-configured committed cash requirement. Update amounts in Settings when confirmed.",
  }));
  renderOverviewRecordCards(
    "overview-payables",
    rows,
    "No payables are configured yet. Add vendor bills, payroll, rent, tax, or planned expense data."
  );
}

function renderOverviewForecast() {
  const horizons = state.tenant.client_setup?.forecast_horizons || [];
  const outflows = state.tenant.client_setup?.planned_outflows || [];
  const expectedTotal = moneyNumber(state.summary.marketplace_deferred_amount);
  const outflowTotal = outflows.reduce((total, outflow) => total + moneyNumber(outflow.amount), 0);
  const hasConfirmedOutflows = outflowTotal > 0;
  const rows = horizons.map((horizon, index) => {
    const ratio = (index + 1) / Math.max(horizons.length, 1);
    const expected = expectedTotal * ratio;
    const outflow = outflowTotal * ratio;
    const net = expected - outflow;
    const scenarioOnly = !hasConfirmedOutflows;
    return {
      status: scenarioOnly ? "Marketplace only" : net >= 0 ? "Covered" : "Funding Gap",
      title: horizon,
      amount: formatMoney(net),
      currency: state.tenant.currencies?.[0] || "",
      date: "Forecast horizon",
      reference: `Marketplace cash ${formatUsd(expected)} / Outflows ${formatUsd(outflow)}`,
      source: scenarioOnly ? "Marketplace data + configured outflows" : "Reconciled data",
      note: scenarioOnly
        ? "Scenario uses marketplace receipts and placeholder outflow assumptions, so this is not a final funding-gap forecast."
        : net >= 0 ? "Expected inflows cover configured outflows." : "Planned outflows exceed expected inflows.",
    };
  });
  renderOverviewRecordCards(
    "overview-forecast",
    rows,
    "No forecast horizons are configured yet. Add the client forecast periods in Configuration."
  );
}

function renderOverviewReports() {
  const hasRuns = state.runs.length > 0;
  const REPORTS = [
    {
      title: "Cash Position Report",
      href: "/reports/cash-position",
      reference: "Cashflow control output",
      note: "Generated from current source data and reconciliation output.",
      needsRuns: true,
    },
    {
      title: "Input Log",
      href: "/reports/source-evidence",
      reference: "Loaded files and pending inputs",
      note: "Shows which marketplace, settlement, reserve, and outflow inputs are present.",
      needsRuns: false,
    },
    {
      title: "Exception Register",
      href: "/reports/exceptions",
      reference: "Finance review items",
      note: "Counts pending receipts, delayed payouts, and validation findings.",
      needsRuns: true,
    },
  ];

  const container = document.getElementById("overview-reports");
  if (!container) return;

  container.innerHTML = REPORTS.map((r) => {
    const ready = r.needsRuns ? hasRuns : true;
    if (ready) {
      return `
        <article class="overview-record-card report-card-link" role="link" tabindex="0" data-href="${escapeAttribute(r.href)}">
          <div class="overview-record-head">
            <div>
              <span class="status-badge ok">Available</span>
              <h3>${escapeHtml(r.title)}</h3>
            </div>
            <a href="${escapeAttribute(r.href)}" target="_blank" class="report-open-btn" onclick="event.stopPropagation()">Open ↗</a>
          </div>
          <div class="source-facts">
            <div><span>Date / Period</span><strong>Latest run</strong></div>
            <div><span>Reference</span><strong>${escapeHtml(r.reference)}</strong></div>
            <div><span>Source</span><strong>${escapeHtml(r.href)}</strong></div>
            <div><span>Note</span><strong>${escapeHtml(r.note)}</strong></div>
          </div>
        </article>`;
    }
    return `
      <article class="overview-record-card report-card-disabled">
        <div class="overview-record-head">
          <div>
            <span class="status-badge pending">No data yet</span>
            <h3>${escapeHtml(r.title)}</h3>
          </div>
          <span class="report-open-btn disabled">Awaiting data</span>
        </div>
        <div class="source-facts">
          <div><span>Date / Period</span><strong>—</strong></div>
          <div><span>Reference</span><strong>${escapeHtml(r.reference)}</strong></div>
          <div><span>Source</span><strong>${escapeHtml(r.href)}</strong></div>
          <div><span>Note</span><strong>Run a reconciliation refresh first to generate this report.</strong></div>
        </div>
      </article>`;
  }).join("");

  // Make entire card clickable for ready reports
  container.querySelectorAll(".report-card-link").forEach((card) => {
    card.addEventListener("click", () => window.open(card.dataset.href, "_blank"));
    card.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") window.open(card.dataset.href, "_blank");
    });
  });
}

function renderOverviewAiInsights() {
  const insights = [
    {
      status: "Locked",
      title: "AI review is disabled until controls are ready",
      amount: "Human review",
      date: "Control gated",
      reference: "Approval required",
      note: "AI stays off until source inputs, reconciliation, and forecast controls are complete.",
    },
  ];
  const pendingAmount = moneyNumber(state.summary.marketplace_deferred_amount);
  const qualityIssues = Number(state.summary.data_quality_issues || 0);
  const outflowCount = state.tenant.client_setup?.planned_outflows?.length || 0;
  const sourceFileCount = Number(state.summary.marketplace_sample_files || 0);

  if (pendingAmount > 0) {
    insights.push({
      status: "Input Review",
      title: "Marketplace deferred cash is visible from Amazon",
      amount: formatUsd(pendingAmount),
      currency: "",
      date: "Current cash view",
      reference: `${state.summary.marketplace_transaction_records} transaction records`,
      note: "This is not an AI recommendation; it is a deterministic read of the uploaded marketplace report.",
    });
  }
  if (qualityIssues > 0) {
    insights.push({
      status: "Data Quality",
      title: "Source validation issues found",
      amount: String(qualityIssues),
      date: "Current cash view",
      reference: "Import Checks",
      note: "Resolve validation findings before using the data for automated recommendations.",
    });
  }
  if (!outflowCount) {
    insights.push({
      status: "Missing Input",
      title: "Planned outflow data is required",
      amount: "0 outflows",
      date: "Settings",
      reference: "Payables / planned obligations",
      note: "Forecast accuracy depends on vendor bills, payroll, rent, tax, and other committed payments.",
    });
  }
  if (!sourceFileCount) {
    insights.push({
      status: "Data Needed",
      title: "Historical source files are not uploaded",
      amount: "0 files",
      date: "Upload Reports",
      reference: "Marketplace payment exports",
      note: "AI forecasting should wait until enough clean marketplace history is uploaded.",
    });
  }

  renderOverviewRecordCards(
    "overview-ai-insights",
    insights,
    "AI is disabled until the source-data foundation is stable."
  );
}

function renderOverviewRecordCards(id, rows, emptyText) {
  const container = document.getElementById(id);
  if (!rows.length) {
    container.innerHTML = `<div class="empty-state">${escapeHtml(emptyText)}</div>`;
    return;
  }
  container.innerHTML = rows.map(overviewRecordCard).join("");
}

function overviewRecordCard(row) {
  const amountText = [row.currency, row.amount].filter(Boolean).join(" ") || "-";
  // Only show marketplace badge when a real marketplace source is explicitly provided
  const badgeSource = row.marketplace || "";
  const profile = badgeSource ? marketplaceProfile(badgeSource) : null;
  const showBadge = profile && profile.providerClass !== "generic";
  return `
    <article class="overview-record-card">
      <div class="overview-record-head">
        <div>
          <span>${escapeHtml(row.status || "Record")}</span>
          <h3>${escapeHtml(row.title || "-")}</h3>
          ${showBadge ? marketplaceBadgesHtml(badgeSource) : ""}
        </div>
        <strong>${escapeHtml(amountText)}</strong>
      </div>
      <div class="source-facts">
        <div><span>Date / Period</span><strong>${escapeHtml(row.date || "-")}</strong></div>
        <div><span>Reference</span><strong>${escapeHtml(row.reference || "-")}</strong></div>
        <div><span>Source</span><strong>${escapeHtml(row.source || row.source_file || row.category || "Current backend data")}</strong></div>
        <div><span>Note</span><strong>${escapeHtml(row.note || "-")}</strong></div>
      </div>
    </article>
  `;
}

const SETTLEMENT_EMPTY = {
  pending: "No pending expected payouts. Upload a marketplace payout or settlement file to show expected receipts.",
};

function renderSettlementCards(id, rows, type) {
  const container = document.getElementById(id);
  if (!rows.length) {
    container.innerHTML = `<div class="empty-state">${escapeHtml(SETTLEMENT_EMPTY[type] || "No records found.")}</div>`;
    return;
  }
  renderCardList(container, rows, (row) => settlementCard(row, type), "settlement record");
}

function renderCardList(container, rows, renderFn, unit = "record", pageSize = PAGE_SIZE) {
  const visible = rows.slice(0, pageSize);
  const remaining = rows.length - visible.length;
  container.innerHTML = visible.map(renderFn).join("");

  let counter = container.querySelector(".row-count");
  if (!counter) {
    counter = document.createElement("div");
    counter.className = "row-count";
    container.insertBefore(counter, container.firstChild);
  }
  counter.textContent = visible.length >= rows.length
    ? `${rows.length.toLocaleString()} ${unit}${rows.length !== 1 ? "s" : ""}`
    : `Showing ${visible.length.toLocaleString()} of ${rows.length.toLocaleString()} ${unit}s`;

  renderCardLoadMore(container, rows, renderFn, visible.length, remaining, unit, pageSize);
}

function renderCardLoadMore(container, allRows, renderFn, shownCount, remaining, unit, pageSize) {
  const existing = container.querySelector(".load-more-row");
  if (existing) existing.remove();
  if (remaining <= 0) return;
  const btn = document.createElement("div");
  btn.className = "load-more-row";
  btn.innerHTML = `<button type="button" class="secondary-button">Load ${Math.min(pageSize, remaining).toLocaleString()} more <span class="muted">(${remaining.toLocaleString()} remaining)</span></button>`;
  btn.querySelector("button").addEventListener("click", () => {
    const batch = allRows.slice(shownCount, shownCount + pageSize);
    btn.remove();
    batch.forEach((row) => container.insertAdjacentHTML("beforeend", renderFn(row)));
    const newShown = shownCount + batch.length;
    const newRemaining = allRows.length - newShown;
    const counter = container.querySelector(".row-count");
    if (counter) counter.textContent = newShown >= allRows.length
      ? `${allRows.length.toLocaleString()} ${unit}${allRows.length !== 1 ? "s" : ""}`
      : `Showing ${newShown.toLocaleString()} of ${allRows.length.toLocaleString()} ${unit}s`;
    renderCardLoadMore(container, allRows, renderFn, newShown, newRemaining, unit, pageSize);
  });
  container.appendChild(btn);
}

function renderDisabledState(id, message) {
  const container = document.getElementById(id);
  container.innerHTML = `<div class="empty-state">${escapeHtml(message)}</div>`;
}

function _settlementDateDisplay(expected, receipt) {
  const e = (expected || "").trim();
  const r = (receipt || "").trim();
  if (e && r && e !== "-" && r !== "-") return { label: "Expected → Received", value: `${e} → ${r}` };
  if (e && e !== "-") return { label: "Expected date", value: e };
  if (r && r !== "-") return { label: "Receipt date", value: r };
  return { label: "Date", value: "Not recorded" };
}

function settlementCard(row, type) {
  const isMatched = type === "matched";
  const isPending = type === "pending";

  // date display
  const datePart = isMatched
    ? _settlementDateDisplay(row.expected_date, row.receipt_date)
    : isPending
    ? { label: "Expected date", value: row.expected_date || "Not recorded" }
    : { label: "Receipt date", value: row.receipt_date || "Not recorded" };

  // label badge
  const label = isMatched
    ? "Confirmed"
    : formatHeader(row.exception_type || (isPending ? "Pending" : "Unexplained"));

  // card title
  const title = isMatched || isPending ? (row.marketplace || row.entity || "-") : (row.bank_account || "-");

  // counterparty
  const counterpart = isMatched
    ? (row.bank_account || "-")
    : isPending
    ? (row.entity || "-")
    : (row.receipt_id || "-");

  // fourth tile: for matched show settlement/match source; for others show next action
  const fourthTile = isMatched
    ? `<div><span>Settlement ID</span><strong>${escapeHtml(row.settlement_id || row.reference || row.receipt_id || "—")}</strong></div>`
    : isPending
    ? `<div class="action-tile"><span>Next action</span><strong>Confirm receipt timing or follow up with marketplace</strong></div>`
    : `<div class="action-tile"><span>Next action</span><strong>Classify receipt or link to a payout reference</strong></div>`;

  // amount — strip duplicate currency prefix if amount already contains it
  const currency = (row.currency || "").trim();
  const amount = (row.amount || "-").trim();
  const amountDisplay = amount.startsWith(currency) ? amount : `${currency} ${amount}`.trim();

  return `
    <article class="settlement-card ${type}">
      <div class="settlement-card-head">
        <div>
          <span class="settlement-label ${type}">${escapeHtml(label)}</span>
          <h3>${escapeHtml(title)}</h3>
        </div>
        <strong class="settlement-amount">${escapeHtml(amountDisplay)}</strong>
      </div>
      <div class="settlement-facts">
        <div><span>Counterparty</span><strong>${escapeHtml(counterpart)}</strong></div>
        <div><span>${escapeHtml(datePart.label)}</span><strong>${escapeHtml(datePart.value)}</strong></div>
        <div><span>Reference</span><strong>${escapeHtml(row.reference || "-")}</strong></div>
        ${fourthTile}
      </div>
    </article>
  `;
}

function renderActionCards(id, expectedRows = state.unmatchedExpected, receiptRows = state.unmatchedReceipts) {
  const container = document.getElementById(id);
  const actions = [
    ...expectedRows.map((row) => ({ ...row, action_type: "expected" })),
    ...receiptRows.map((row) => ({ ...row, action_type: "receipt" })),
  ];
  if (!actions.length) {
    container.innerHTML = `<div class="empty-state">No finance actions required. All payouts and receipts are matched.</div>`;
    return;
  }
  renderCardList(container, actions, actionCard, "action item");
}

function actionCard(row) {
  const isReceipt = row.action_type === "receipt";
  const title = isReceipt ? row.bank_account : row.marketplace;
  const amount = `${row.currency || ""} ${row.amount || "-"}`;
  const date = isReceipt ? row.receipt_date : row.expected_date;
  const impact = isReceipt
    ? "Upload bank export files to match this receipt to a marketplace payout."
    : "Expected marketplace cash is not yet confirmed in the bank feed.";
  const nextAction = isReceipt
    ? "Classify this receipt or connect it to the correct payout reference."
    : "Confirm expected receipt timing with the marketplace source owner.";

  return `
    <article class="action-card ${isReceipt ? "receipt" : "expected"}">
      <div class="action-card-head">
        <div>
          <span>${escapeHtml(formatHeader(row.exception_type || "Review"))}</span>
          <h3>${escapeHtml(title || "-")}</h3>
        </div>
        <strong>${escapeHtml(amount)}</strong>
      </div>
      <div class="action-card-body">
        <div><span>Business impact</span><strong>${escapeHtml(impact)}</strong></div>
        <div><span>Date</span><strong>${escapeHtml(date || "-")}</strong></div>
        <div><span>Reference</span><strong>${escapeHtml(row.reference || "-")}</strong></div>
        <div><span>Recommended next action</span><strong>${escapeHtml(nextAction)}</strong></div>
      </div>
    </article>
  `;
}

// ── Source card helpers ─────────────────────────────────────────────────────

function _sourceSelectOptions(opts, selected) {
  return opts.map((o) => `<option value="${escapeAttribute(o)}"${o === selected ? " selected" : ""}>${escapeHtml(o)}</option>`).join("");
}

function _sourceStatusClass(status) {
  if (!status) return "";
  const s = status.toLowerCase();
  if (s.includes("active") || s.includes("uploaded") || s.includes("configured")) return "ok";
  if (s.includes("await") || s.includes("pending") || s.includes("review")) return "partial";
  return "";
}

function _sourceViewHtml(source, idx) {
  return `
    <article class="data-source-card" data-src-idx="${idx}">
      <div class="src-card-top">
        <span class="strip-label">${escapeHtml(source.group || "Marketplace")}</span>
        <div class="src-card-btns">
          <button type="button" class="secondary-button edit-source-btn" data-src-idx="${idx}">Edit</button>
          <button type="button" class="secondary-button danger-btn remove-source-btn" data-src-idx="${idx}">Delete</button>
        </div>
      </div>
      <div class="src-card-name">
        <h3>${escapeHtml(source.name)}</h3>
        ${marketplaceBadgesHtml(source.name)}
      </div>
      <span class="status ${_sourceStatusClass(source.status)}">${escapeHtml(source.status || "Configured")}</span>
      <div class="source-facts">
        <div><span>Access method</span><strong>${escapeHtml(source.type || "-")}</strong></div>
        <div><span>Owner</span><strong>${escapeHtml(source.owner || "-")}</strong></div>
        <div><span>Frequency</span><strong>${escapeHtml(source.frequency || "-")}</strong></div>
        <div><span>Evidence</span><strong>${escapeHtml(source.evidence_type || "-")}</strong></div>
        <div><span>API Endpoint</span><strong class="${source.api_endpoint ? "" : "muted"}">${escapeHtml(source.api_endpoint || "Not configured")}</strong></div>
        <div><span>Credential</span><strong class="${source.credential_reference ? "" : "muted"}">${escapeHtml(source.credential_reference || "Not configured")}</strong></div>
      </div>
    </article>`;
}

function _sourceEditHtml(source, idx) {
  return `
    <article class="data-source-card editing" data-src-idx="${idx}">
      <div class="src-card-top">
        <span class="strip-label">Editing</span>
        <div class="src-card-btns">
          <button type="button" class="run-button inline save-source-btn" data-src-idx="${idx}">Save</button>
          <button type="button" class="secondary-button cancel-source-btn" data-src-idx="${idx}">Cancel</button>
          <button type="button" class="secondary-button danger-btn remove-source-btn" data-src-idx="${idx}">Delete</button>
        </div>
      </div>
      <input class="src-edit-name edit-title-input" value="${escapeAttribute(source.name)}" placeholder="Source name (required)">
      <div class="form-grid compact source-edit-grid">
        <label>Source Type
          <select class="src-edit-type">
            <option value="">— select —</option>
            ${_sourceSelectOptions(SOURCE_TYPES, source.type)}
          </select>
        </label>
        <label>Status
          <select class="src-edit-status">
            ${_sourceSelectOptions(SOURCE_STATUSES, source.status || "Configured")}
          </select>
        </label>
        <label>Owner
          <input class="src-edit-owner" value="${escapeAttribute(source.owner || "")}" placeholder="e.g. Finance analyst">
        </label>
        <label>Frequency
          <select class="src-edit-frequency">
            <option value="">— select —</option>
            ${_sourceSelectOptions(SOURCE_FREQUENCIES, source.frequency)}
          </select>
        </label>
        <label>Evidence / Report Name
          <input class="src-edit-evidence" value="${escapeAttribute(source.evidence_type || "")}" placeholder="e.g. Amazon Payments report">
        </label>
        <label>API Endpoint
          <input class="src-edit-api-endpoint" value="${escapeAttribute(source.api_endpoint || "")}" placeholder="https://…">
        </label>
        <label>Credential Reference
          <input class="src-edit-credential" value="${escapeAttribute(source.credential_reference || "")}" placeholder="e.g. vault/marketplace-key">
        </label>
      </div>
      <p class="src-edit-error muted" style="display:none;color:var(--red)"></p>
    </article>`;
}

function renderSourceCards(id, sources) {
  const container = document.getElementById(id);
  if (!sources.length) {
    container.innerHTML = `<div class="empty-state">No marketplace sources configured yet. Use <strong>+ Add Marketplace</strong> above to add one.</div>`;
    return;
  }
  container.innerHTML = sources.map((source, idx) => _sourceViewHtml(source, idx)).join("");
  _bindSourceCardEvents(container);
}

function _bindSourceCardEvents(container) {
  container.querySelectorAll(".edit-source-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const idx = parseInt(btn.dataset.srcIdx, 10);
      const source = (state.tenant.sources?.marketplaces || [])[idx];
      const card = container.querySelector(`[data-src-idx="${idx}"]`);
      card.outerHTML = _sourceEditHtml(source, idx);
      _bindSourceCardEvents(container);
    });
  });

  container.querySelectorAll(".cancel-source-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const idx = parseInt(btn.dataset.srcIdx, 10);
      const source = (state.tenant.sources?.marketplaces || [])[idx];
      const card = container.querySelector(`[data-src-idx="${idx}"]`);
      card.outerHTML = _sourceViewHtml(source, idx);
      _bindSourceCardEvents(container);
    });
  });

  container.querySelectorAll(".save-source-btn").forEach((btn) => {
    btn.addEventListener("click", () => saveSourceCard(container, parseInt(btn.dataset.srcIdx, 10)));
  });

  container.querySelectorAll(".remove-source-btn").forEach((btn) => {
    btn.addEventListener("click", () => deleteSourceCard(container, parseInt(btn.dataset.srcIdx, 10), btn));
  });
}

async function saveSourceCard(container, idx) {
  const card = container.querySelector(`[data-src-idx="${idx}"]`);
  const nameVal = card.querySelector(".src-edit-name").value.trim();
  const errEl = card.querySelector(".src-edit-error");
  if (!nameVal) {
    errEl.textContent = "Source name is required.";
    errEl.style.display = "block";
    card.querySelector(".src-edit-name").focus();
    return;
  }
  errEl.style.display = "none";

  const updated_source = {
    name: nameVal,
    type: card.querySelector(".src-edit-type").value,
    status: card.querySelector(".src-edit-status").value,
    owner: card.querySelector(".src-edit-owner").value.trim(),
    frequency: card.querySelector(".src-edit-frequency").value,
    evidence_type: card.querySelector(".src-edit-evidence").value.trim(),
    api_endpoint: card.querySelector(".src-edit-api-endpoint").value.trim(),
    credential_reference: card.querySelector(".src-edit-credential").value.trim(),
  };

  const saveBtn = card.querySelector(".save-source-btn");
  saveBtn.disabled = true;
  saveBtn.textContent = "Saving…";

  try {
    const current = await fetchJson("/tenant/settings");
    const list = [...(current.sources?.marketplaces || [])];
    list[idx] = updated_source;
    const res = await apiFetch("/tenant/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...current, sources: { ...current.sources, marketplaces: list } }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      errEl.textContent = err.detail || "Save failed";
      errEl.style.display = "block";
      saveBtn.disabled = false;
      saveBtn.textContent = "Save";
      return;
    }
    state.tenant = await fetchJson("/tenant/settings");
    render();
  } catch {
    errEl.textContent = "Save failed — check connection.";
    errEl.style.display = "block";
    saveBtn.disabled = false;
    saveBtn.textContent = "Save";
  }
}

async function deleteSourceCard(container, idx, btn) {
  const source = (state.tenant.sources?.marketplaces || [])[idx];
  if (!confirm(`Delete "${source?.name}"?\n\nThis removes the configuration only — uploaded files are not affected.`)) return;
  btn.disabled = true;
  btn.textContent = "Deleting…";
  try {
    const current = await fetchJson("/tenant/settings");
    const list = (current.sources?.marketplaces || []).filter((_, i) => i !== idx);
    const res = await apiFetch("/tenant/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...current, sources: { ...current.sources, marketplaces: list } }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      alert(err.detail || "Delete failed");
      btn.disabled = false;
      btn.textContent = "Delete";
      return;
    }
    state.tenant = await fetchJson("/tenant/settings");
    render();
  } catch {
    alert("Delete failed — check connection.");
    btn.disabled = false;
    btn.textContent = "Delete";
  }
}

function renderSourceHealth() {
  const sources = [
    ...(state.tenant.sources.marketplaces || []).map((source) => ({
      ...source,
      group: "Marketplace",
    })),
  ];
  const attentionCount = sources.filter((source) =>
    `${source.status}`.toLowerCase().includes("await")
  ).length;

  const apiSourceCount = sources.filter((s) => s.type?.toLowerCase().includes("api")).length;
  const fileSourceCount = sources.length - apiSourceCount;
  setText("health-source-count", sources.length);
  setText("health-connected-count", apiSourceCount ? `${apiSourceCount} API source(s)` : "No API connector");
  setText("health-file-count", fileSourceCount ? `${fileSourceCount} file-based source(s)` : "No file sources");
  setText("health-attention-count", attentionCount);
  const failedCount = (state.uploads || []).filter((u) => u.status === "error" || u.status === "failed").length;
  setText("health-failed-count", failedCount > 0 ? `${failedCount} failed` : "None");

  const container = document.getElementById("source-health-cards");
  if (!sources.length) {
    container.innerHTML = `<div class="empty-state">No sources configured for monitoring yet.</div>`;
    return;
  }
  container.innerHTML = sources.map(sourceHealthCard).join("");

  container.querySelectorAll(".sync-now-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      btn.textContent = "Syncing…";
      await syncFromApi();
      btn.disabled = false;
      btn.textContent = "Sync Now";
    });
  });

  container.querySelectorAll(".health-card-remove-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const name = btn.dataset.sourceName;
      const group = btn.dataset.sourceGroup;
      _confirmHealthCardRemove(name, group);
    });
  });
}

function _sourceHasData(name) {
  const nameLower = name.toLowerCase();
  const inUploads = (state.uploads || []).some(
    (u) => String(u.source || u.marketplace || "").toLowerCase().includes(nameLower)
  );
  const inActivity = (state.marketplaceActivity || []).some(
    (a) => String(a.marketplace || "").toLowerCase().includes(nameLower)
  );
  const inRuns = state.runs.length > 0;
  return { inUploads, inActivity, inRuns, any: inUploads || inActivity };
}

function _confirmHealthCardRemove(name, group) {
  const data = _sourceHasData(name);
  const warningLines = [];
  if (data.inUploads) warningLines.push("• Uploaded files linked to this source");
  if (data.inActivity) warningLines.push("• Transaction activity records from this source");
  if (data.inRuns && data.any) warningLines.push("• Reconciliation runs that used this source");

  const hasData = warningLines.length > 0;
  const message = hasData
    ? `Remove "${name}" from ${group} sources?\n\nWarning — associated data found:\n${warningLines.join("\n")}\n\nThe source configuration will be removed. Historical data in the system will remain but will no longer be attributed to a configured source.`
    : `Remove "${name}" from ${group} sources?\n\nNo associated data was found. The source will be removed from configuration.`;

  if (!confirm(message)) return;

  // Remove from tenant config
  const tenant = state.tenant;
  if (group === "Marketplace") {
    tenant.sources.marketplaces = (tenant.sources.marketplaces || []).filter(
      (s) => s.name !== name
    );
  } else {
    tenant.sources.banks = (tenant.sources.banks || []).filter(
      (s) => s.name !== name
    );
  }

  apiFetch("/tenant/settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(tenant),
  })
    .then(async (r) => {
      if (!r.ok) throw new Error(await r.text());
      state.tenant = await r.json();
      render();
      showLiveToast(`"${name}" removed from ${group} sources.`);
    })
    .catch(() => showLiveToast("Failed to remove source — please try again."));
}

function sourceHealthCard(source) {
  const sourceText = `${source.type} ${source.evidence_type}`.toLowerCase();
  const isConnected = sourceText.includes("api") && source.api_endpoint;
  const isApiConfigured = sourceText.includes("api");
  const isAttention = `${source.status}`.toLowerCase().includes("await");
  const mode = isConnected ? "Live API connection" : isApiConfigured ? "API configured — awaiting credential" : "File-based source";
  const healthLabel = isConnected ? "API live" : isApiConfigured ? "API pending credential" : "Manual reports active";
  const nextCheck = isConnected ? source.frequency || "Daily" : "On file receipt";

  return `
    <article class="health-card ${isAttention ? "attention" : "ready"}">
      <div class="health-card-head">
        <div>
          <span>${escapeHtml(source.group)}</span>
          <h3>${escapeHtml(source.name)}</h3>
          ${marketplaceBadgesHtml(source.name)}
        </div>
        <div class="health-card-actions">
          <strong class="health-status-label">${escapeHtml(healthLabel)}</strong>
          ${isApiConfigured ? `<button type="button" class="secondary-button sync-now-btn">Sync Now</button>` : ""}
          <button type="button" class="health-card-remove-btn" data-source-name="${escapeAttribute(source.name)}" data-source-group="${escapeAttribute(source.group)}" title="Remove source">
            <svg viewBox="0 0 16 16" fill="none" width="14" height="14"><path d="M2 4h12M5 4V3a1 1 0 011-1h4a1 1 0 011 1v1M6 7v5M10 7v5M3 4l1 9a1 1 0 001 1h6a1 1 0 001-1l1-9" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>
          </button>
        </div>
      </div>
      <div class="source-facts">
        <div><span>Mode</span><strong>${escapeHtml(mode)}</strong></div>
        <div><span>Access method</span><strong>${escapeHtml(source.type || "-")}</strong></div>
        <div><span>Owner</span><strong>${escapeHtml(source.owner || "Finance owner")}</strong></div>
        <div><span>Next check</span><strong>${escapeHtml(nextCheck)}</strong></div>
        <div><span>Evidence</span><strong>${escapeHtml(source.evidence_type || "Source data")}</strong></div>
        <div><span>API Endpoint</span><strong class="${source.api_endpoint ? "" : "muted"}">${escapeHtml(source.api_endpoint || "Not configured — add in Settings")}</strong></div>
        <div><span>Credential</span><strong class="${source.credential_reference ? "" : "muted"}">${escapeHtml(source.credential_reference || "Not configured — add in Settings")}</strong></div>
      </div>
    </article>
  `;
}

function renderUploads() {
  const container = document.getElementById("uploads-list");
  if (!container) return;
  if (!state.uploads.length) {
    container.innerHTML = `<div class="empty-state">No source files imported yet. Use the form above to upload a marketplace CSV.</div>`;
    return;
  }
  container.innerHTML = state.uploads.map((upload) => {
    const name = escapeHtml(upload.file_name || "-");
    const cat = escapeHtml(formatHeader(upload.category || ""));
    const date = escapeHtml(formatTransactionDate(upload.uploaded_at || ""));
    const path = escapeHtml(upload.path || "");
    return `
      <div class="upload-file-row" data-file="${escapeAttribute(upload.file_name)}">
        <div class="upload-file-info">
          <strong>${name}</strong>
          <span>${cat}</span>
        </div>
        <div class="upload-file-meta">
          <span title="${path}">${date}</span>
          <button type="button" class="delete-upload-btn secondary-button danger-btn" data-file="${escapeAttribute(upload.file_name)}">Delete</button>
        </div>
      </div>`;
  }).join("");

  container.querySelectorAll(".delete-upload-btn").forEach((btn) => {
    btn.addEventListener("click", () => deleteUpload(btn.dataset.file, btn));
  });
}

async function deleteUpload(fileName, btn) {
  if (!confirm(`Delete "${fileName}"?\n\nThis will remove the file and re-run reconciliation.`)) return;
  btn.disabled = true;
  btn.textContent = "Deleting…";
  try {
    const res = await apiFetch(`/phase0/uploads/${encodeURIComponent(fileName)}`, { method: "DELETE" });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      alert(err.detail || "Delete failed");
      btn.disabled = false;
      btn.textContent = "Delete";
      return;
    }
    state.uploads = await fetchJson("/phase0/uploads");
    state.summary = await fetchJson("/phase0/summary");
    const activity = await fetchJson("/phase0/marketplace-activity");
    state.marketplaceActivity = Array.isArray(activity) ? activity : (activity.rows || []);
    state.marketplaceActivityStatus = Array.isArray(activity) ? { status: "ready" } : activity;
    renderUploads();
    setText("uploads-count", state.uploads.length);
    render();
  } catch {
    alert("Delete failed — server error");
    btn.disabled = false;
    btn.textContent = "Delete";
  }
}

const RUNS_PAGE = 20;

const REPORT_DEFINITIONS = [
  {
    tag: "Cash",
    title: "Cash Position Report",
    desc: "Marketplace evidence, source readiness, and full cash view",
    href: "/reports/cash-position",
    needsRuns: true,
    isHtml: true,
  },
  {
    tag: "Summary",
    title: "Treasury Summary",
    desc: "Current marketplace metrics and source status",
    href: "/reports/summary",
    needsRuns: true,
    isHtml: false,
  },
  {
    tag: "Inputs",
    title: "Input Log",
    desc: "Loaded files and pending inputs",
    href: "/reports/source-evidence",
    needsRuns: false,
    isHtml: false,
  },
  {
    tag: "Exceptions",
    title: "Exception Register",
    desc: "Items needing finance review",
    href: "/reports/exceptions",
    needsRuns: true,
    isHtml: false,
  },
];

function renderReportLinks() {
  const container = document.getElementById("report-links");
  if (!container) return;
  const hasRuns = state.runs.length > 0;
  const hasUploads = (state.uploads || []).length > 0;
  setText("reports-output-count", REPORT_DEFINITIONS.length);

  container.innerHTML = `<div class="report-links">${REPORT_DEFINITIONS.map((r) => {
    const dataReady = r.needsRuns ? hasRuns : hasUploads;
    if (dataReady && r.isHtml) {
      return `
        <a href="${r.href}" target="_blank" class="report-link-item">
          <div class="report-link-tag">${escapeHtml(r.tag)}</div>
          <div class="report-link-body">
            <strong>${escapeHtml(r.title)}</strong>
            <small>${escapeHtml(r.desc)}</small>
          </div>
          <span class="report-link-status ok">Open ↗</span>
        </a>`;
    }
    if (dataReady && !r.isHtml) {
      return `
        <div class="report-link-item" title="Returns structured data — use via API or export">
          <div class="report-link-tag">${escapeHtml(r.tag)}</div>
          <div class="report-link-body">
            <strong>${escapeHtml(r.title)}</strong>
            <small>${escapeHtml(r.desc)}</small>
          </div>
          <span class="report-link-status api-only">API / Export</span>
        </div>`;
    }
    const why = r.needsRuns
      ? "Run a reconciliation refresh to generate this report"
      : "Upload at least one file to enable this report";
    return `
      <div class="report-link-item disabled" title="${escapeAttribute(why)}">
        <div class="report-link-tag muted">${escapeHtml(r.tag)}</div>
        <div class="report-link-body">
          <strong>${escapeHtml(r.title)}</strong>
          <small class="muted">${escapeHtml(why)}</small>
        </div>
        <span class="report-link-status muted">No data yet</span>
      </div>`;
  }).join("")}</div>`;
}

function renderRuns() {
  renderReportLinks();
  const container = document.getElementById("run-history-list");
  if (!container) return;
  setText("runs-count", state.runs.length || "—");
  if (!state.runs.length) {
    container.innerHTML = `<div class="empty-state">No cash view refreshes yet. Upload a file to trigger the first run.</div>`;
    return;
  }
  _renderRunsPage(container, state.runs, 0);
}

function _renderRunsPage(container, runs, offset) {
  const page = runs.slice(offset, offset + RUNS_PAGE);
  const isFirst = offset === 0;

  if (isFirst) {
    container.innerHTML = `
      <div class="run-list-header">
        <span>Run ID</span>
        <span>Date</span>
        <span>Status</span>
        <span class="num">Matched</span>
        <span class="num">Pending</span>
        <span class="num">Unmatched receipts</span>
      </div>
      <div class="run-list-body"></div>
      <div class="run-list-footer"></div>`;
  }

  const body = container.querySelector(".run-list-body");
  page.forEach((run) => {
    const status = (run.status || "generated").toLowerCase();
    const statusClass = status === "generated" ? "ok" : "partial";
    const row = document.createElement("div");
    row.className = "run-list-row";
    row.innerHTML = `
      <span class="run-id">${escapeHtml(String(run.run_id || "—"))}</span>
      <span>${escapeHtml(formatTransactionDate(String(run.created_at || "")))}</span>
      <span><span class="run-status-pill ${statusClass}">${escapeHtml(String(run.status || "Generated"))}</span></span>
      <span class="num">${run.matched_payouts ?? "—"}</span>
      <span class="num">${run.unmatched_expected_payouts ?? "—"}</span>
      <span class="num">${run.unmatched_bank_receipts ?? "—"}</span>`;
    body.appendChild(row);
  });

  const footer = container.querySelector(".run-list-footer");
  const shown = offset + page.length;
  const remaining = runs.length - shown;
  footer.innerHTML = remaining > 0
    ? `<button type="button" class="secondary-button run-load-more">
         Load ${Math.min(RUNS_PAGE, remaining)} more
         <span class="muted">(${remaining} remaining)</span>
       </button>`
    : `<span class="muted run-list-count">${runs.length.toLocaleString()} run${runs.length !== 1 ? "s" : ""} total</span>`;

  if (remaining > 0) {
    footer.querySelector(".run-load-more").addEventListener("click", () => {
      _renderRunsPage(container, runs, shown);
    });
  }
}

function renderForecast() {
  const horizons = state.tenant.client_setup?.forecast_horizons || [];
  const outflows = state.tenant.client_setup?.planned_outflows || [];
  // Use confirmed matched + unmatched expected as the realistic "expected inflows"
  const matchedAmt   = moneyNumber(state.summary.matched_amount);
  const pendingAmt   = moneyNumber(state.summary.unmatched_expected_amount);
  const expectedTotal = (Number.isFinite(matchedAmt) ? matchedAmt : 0)
                      + (Number.isFinite(pendingAmt)  ? pendingAmt  : 0);
  const outflowTotal = outflows.reduce((total, outflow) => {
    const parsed = moneyNumber(outflow.amount);
    return total + (Number.isFinite(parsed) ? parsed : 0);
  }, 0);
  const netPosition = expectedTotal - outflowTotal;

  setText("forecast-horizon-count", horizons.length || "—");
  setText("forecast-outflow-count", outflows.length || "—");
  setText("forecast-expected-total", expectedTotal ? formatMoney(expectedTotal) : "No data yet");
  setText("forecast-outflow-total",  outflowTotal  ? formatMoney(outflowTotal)  : "Awaiting inputs");
  setText("forecast-net-position",   expectedTotal ? formatMoney(netPosition)   : "No data yet");

  const container = document.getElementById("forecast-cards");
  if (!horizons.length) {
    container.innerHTML = `<div class="empty-state">
      No forecast horizons configured yet.<br>
      <span class="muted">Add forecast periods in <strong>Settings → Client Setup → Forecast Horizons</strong>.</span>
    </div>`;
    return;
  }
  if (!expectedTotal) {
    container.innerHTML = `<div class="empty-state">
      Forecast horizons are configured but no reconciled cash data is available yet.<br>
      <span class="muted">Upload marketplace and bank files to populate expected receipt amounts.</span>
    </div>`;
    return;
  }

  const cards = horizons.map((horizon, index) => {
    const ratio = (index + 1) / Math.max(horizons.length, 1);
    const expected = expectedTotal * ratio;
    const outflow  = outflowTotal  * ratio;
    const net      = expected - outflow;
    return { horizon, expected, outflow, net };
  });
  container.innerHTML = cards.map(forecastCard).join("");
}

function forecastCard(card) {
  const gap = card.net < 0;
  const noOutflows = card.outflow === 0;
  return `
    <article class="forecast-card ${gap ? "gap" : "surplus"}">
      <div class="forecast-card-head">
        <div>
          <span class="strip-label">Horizon</span>
          <h3>${escapeHtml(card.horizon)}</h3>
        </div>
        <span class="status ${gap ? "warn" : "ok"}">${gap ? "Funding gap" : "Surplus"}</span>
      </div>
      <div class="forecast-facts">
        <div>
          <span>Expected inflows</span>
          <strong>${escapeHtml(formatMoney(card.expected))}</strong>
          <small class="muted">Matched + pending payouts</small>
        </div>
        <div>
          <span>Planned outflows</span>
          <strong class="${noOutflows ? "muted" : ""}">${noOutflows ? "Awaiting inputs" : escapeHtml(formatMoney(card.outflow))}</strong>
          ${noOutflows ? `<small class="muted">Add outflows in Settings</small>` : ""}
        </div>
        <div class="forecast-net ${gap ? "gap" : "surplus"}">
          <span>Net position</span>
          <strong>${escapeHtml(formatMoney(card.net))}</strong>
          <small class="muted">${gap ? "Outflows exceed inflows — plan funding" : "Inflows cover outflows"}</small>
        </div>
      </div>
    </article>
  `;
}

function renderReserves() {
  const marketplaces = state.tenant.sources.marketplaces || [];
  setText("reserve-count", marketplaces.length || "—");
  setText("reserve-total", "No hold data loaded");
  const container = document.getElementById("reserve-cards");
  if (!marketplaces.length) {
    container.innerHTML = `<div class="empty-state">No marketplace sources configured yet.</div>`;
    return;
  }
  container.innerHTML = marketplaces.map(reserveCard).join("");
}

function reserveCard(source) {
  const reason = source.evidence_type || source.type || "Marketplace settlement policy";
  return `
    <article class="reserve-card">
      <div class="reserve-card-head">
        <div>
          <span class="strip-label">Marketplace hold</span>
          <h3>${escapeHtml(source.name)}</h3>
          ${marketplaceBadgesHtml(source.name)}
        </div>
        <span class="status partial">Awaiting data</span>
      </div>
      <div class="reserve-facts">
        <div>
          <span>Held amount</span>
          <strong class="muted">No hold data from source</strong>
          <small class="muted">Upload a reserve/hold report to populate</small>
        </div>
        <div>
          <span>Release timing</span>
          <strong class="muted">To be confirmed</strong>
        </div>
        <div>
          <span>Reason</span>
          <strong>${escapeHtml(reason)}</strong>
        </div>
        <div>
          <span>Cash impact</span>
          <strong>Reduces available liquidity</strong>
        </div>
      </div>
    </article>
  `;
}

function renderSettings() {
  const tenant = state.tenant;
  setValue("settings-product-name", tenant.product_name);
  setValue("settings-tenant-name", tenant.tenant_name);
  setValue("settings-poc-entity", tenant.poc_entity);
  setValue("settings-logo-text", tenant.brand.logo_text);
  setValue("settings-primary-color", tenant.brand.primary_color);
  setValue("settings-currencies", tenant.currencies.join(", "));
  setValue("settings-date-tolerance", tenant.reconciliation.date_tolerance_days);
  setValue("settings-amount-tolerance", tenant.reconciliation.amount_tolerance);
  setValue(
    "settings-data-retention",
    tenant.client_setup?.governance?.data_retention || ""
  );
  setValue(
    "settings-hosting-region",
    tenant.client_setup?.governance?.hosting_region || ""
  );
  document.getElementById("settings-ai-enabled").checked =
    Boolean(tenant.client_setup?.governance?.ai_enabled);
  document.getElementById("settings-human-approval").checked =
    tenant.client_setup?.governance?.human_approval_required !== false;
  renderSetupCards(
    "entities-editor",
    "entity",
    tenant.client_setup?.entities || []
  );
  renderSetupCards(
    "outflows-editor",
    "outflow",
    tenant.client_setup?.planned_outflows || []
  );
  renderSetupCards(
    "horizons-editor",
    "horizon",
    (tenant.client_setup?.forecast_horizons || []).map((value) => ({ value }))
  );
  renderSetupCards(
    "alerts-editor",
    "alert",
    tenant.client_setup?.alert_rules || []
  );
  renderSetupCards(
    "roles-editor",
    "role",
    tenant.client_setup?.user_roles || []
  );
}

function amazonSourceStatusDetails(integration) {
  const credentials = integration?.credentials || {};
  const configured = Object.values(credentials).filter(Boolean).length;
  const authorizedMarketplaces = (integration?.marketplaces || []).filter(
    (marketplace) => marketplace.is_participating
  );
  const failed = /failed|denied|error/i.test(integration?.status || "") || Boolean(integration?.last_error);
  return {
    credentials: configured === 3 ? "Complete" : `${configured} of 3 stored`,
    authorization: authorizedMarketplaces.length
      ? `${authorizedMarketplaces.length} marketplace${authorizedMarketplaces.length === 1 ? "" : "s"} authorized`
      : failed ? "Failed" : configured === 3 ? "Not tested" : "Incomplete",
    automaticSync: integration?.enabled ? "Enabled" : "Disabled",
    dataSync: integration?.last_sync_at ? "Successful" : failed ? "Failed" : "Never run",
    freshness: integration?.last_sync_at ? formatIntegrationTime(integration.last_sync_at) : "No synchronized data",
  };
}

function renderAmazonIntegration() {
  const integration = selectedAmazonSource();
  const form = document.getElementById("amazon-integration-form");
  if (state.amazonSources === null) {
    form.querySelectorAll("input, select, button").forEach((element) => { element.disabled = true; });
    setText("amazon-integration-status", "Server key required");
    document.getElementById("amazon-integration-message").textContent = "Provision the integration encryption key before saving credentials.";
    return;
  }
  form.querySelectorAll("input, select, button").forEach((element) => { element.disabled = false; });
  const sourceSelect = document.getElementById("amazon-source-select");
  sourceSelect.innerHTML = state.amazonSources.length
    ? state.amazonSources.map((source) => `<option value="${escapeAttribute(source.id)}" ${source.id === state.selectedAmazonSourceId ? "selected" : ""}>${escapeHtml(source.name)} — ${escapeHtml(source.status)}</option>`).join("")
    : '<option value="">New Amazon source</option>';
  document.getElementById("amazon-source-list").innerHTML = state.amazonSources.length
    ? state.amazonSources.map((source) => `<div class="integration-source-row"><strong>${escapeHtml(source.name)}</strong><span>${escapeHtml(source.seller_id || "Identity not verified")}</span><span class="integration-status ${source.status === "Connected" ? "connected" : "pending"}">${escapeHtml(source.status)}</span></div>`).join("")
    : '<div class="empty-state compact">No Amazon sources configured.</div>';
  setText("amazon-source-count", `${state.amazonSources.length} source${state.amazonSources.length === 1 ? "" : "s"}`);
  setValue("amazon-source-name", integration?.name || "");
  setValue("amazon-application-id", integration?.application_id || "");
  setValue("amazon-endpoint", integration?.endpoint || "https://sellingpartnerapi-na.amazon.com");
  setValue("amazon-marketplace", integration?.marketplace || "");
  setValue("amazon-sync-frequency", integration?.sync_frequency_minutes || 60);
  document.getElementById("amazon-enabled").checked = Boolean(integration?.enabled);
  ["amazon-client-id", "amazon-client-secret", "amazon-refresh-token"].forEach((id) => setValue(id, ""));
  const credentials = integration?.credentials || {};
  const configured = Object.values(credentials).filter(Boolean).length;
  const statusDetails = amazonSourceStatusDetails(integration);
  const marketplaces = (integration?.marketplaces || [])
    .map((marketplace) => marketplace.name || marketplace.id)
    .filter(Boolean)
    .join(", ");
  document.getElementById("amazon-account-identity").innerHTML = [
    ["Source account", integration?.name || "Select a source"],
    ["Amazon Seller ID", integration?.seller_id || "Run Test Connection"],
    ["Authorized marketplaces", marketplaces || "Not verified"],
    ["Last successful sync", statusDetails.freshness],
  ].map(([label, detail]) => `<div><span>${escapeHtml(label)}</span><strong>${escapeHtml(detail)}</strong></div>`).join("");
  const status = document.getElementById("amazon-integration-status");
  status.textContent = integration?.status || "New source";
  status.className = `integration-status ${integration && configured === 3 ? "connected" : "pending"}`;
  document.getElementById("amazon-integration-summary").innerHTML = [
    ["Credentials", statusDetails.credentials],
    ["Authorization", statusDetails.authorization],
    ["Automatic sync", statusDetails.automaticSync],
    ["Data sync", statusDetails.dataSync],
    ["Freshness", statusDetails.freshness],
  ].map(([label, detail]) => `<div><span>${escapeHtml(label)}</span><strong>${escapeHtml(detail)}</strong></div>`).join("");
  document.getElementById("amazon-integration-message").textContent = integration?.last_error || "Secret fields stay blank after saving; blank values preserve stored credentials.";
  document.getElementById("amazon-sync-history").innerHTML = state.amazonSyncs.length
    ? state.amazonSyncs.map((run) => `<div class="integration-history-row"><span class="integration-run-status ${escapeAttribute(run.status)}">${escapeHtml(run.status)}</span><strong>${escapeHtml(run.message)}</strong><time>${escapeHtml(formatIntegrationTime(run.completed_at))}</time></div>`).join("")
    : '<div class="empty-state compact">No API sync has run yet.</div>';
  ["amazon-test-connection", "amazon-sync-now", "amazon-remove-credentials"].forEach((id) => {
    document.getElementById(id).disabled = !integration;
  });
}

function selectedAmazonSource() {
  return (state.amazonSources || []).find((source) => source.id === state.selectedAmazonSourceId) || null;
}

async function loadSelectedAmazonSource() {
  if (!state.selectedAmazonSourceId) {
    state.amazonSyncs = [];
    return;
  }
  state.amazonSyncs = await fetchJson(
    `/admin/integrations/amazon/sources/${state.selectedAmazonSourceId}/syncs`
  ).catch(() => []);
}

function amazonIntegrationPayload() {
  return {
    name: value("amazon-source-name"),
    application_id: value("amazon-application-id"),
    endpoint: value("amazon-endpoint"),
    marketplace: value("amazon-marketplace"),
    sync_frequency_minutes: Number(value("amazon-sync-frequency")),
    enabled: document.getElementById("amazon-enabled").checked,
    client_id: value("amazon-client-id"),
    client_secret: value("amazon-client-secret"),
    refresh_token: value("amazon-refresh-token"),
  };
}

async function saveAmazonIntegration() {
  const button = document.getElementById("amazon-save-integration");
  setAmazonActionState(button, true, "Saving…");
  try {
    const path = state.selectedAmazonSourceId
      ? `/admin/integrations/amazon/sources/${state.selectedAmazonSourceId}`
      : "/admin/integrations/amazon/sources";
    const saved = await fetchJson(path, {
      method: state.selectedAmazonSourceId ? "PUT" : "POST",
      body: JSON.stringify(amazonIntegrationPayload()),
    });
    state.selectedAmazonSourceId = saved.id;
    state.amazonSources = await fetchJson("/admin/integrations/amazon/sources");
    await loadSelectedAmazonSource();
    document.getElementById("amazon-integration-message").textContent = "Integration saved securely.";
    renderAmazonIntegration();
  } catch (error) {
    document.getElementById("amazon-integration-message").textContent = error.message;
  } finally {
    setAmazonActionState(button, false, "Save Integration");
  }
}

async function runAmazonAction(action) {
  const button = document.getElementById(action === "test" ? "amazon-test-connection" : "amazon-sync-now");
  const label = action === "test" ? "Test Connection" : "Sync Now";
  setAmazonActionState(button, true, action === "test" ? "Testing…" : "Syncing…");
  try {
    const result = await fetchJson(
      `/admin/integrations/amazon/sources/${state.selectedAmazonSourceId}/${action}`,
      { method: "POST" }
    );
    document.getElementById("amazon-integration-message").textContent = action === "test"
      ? `Connection successful. ${result.source.marketplaces.length} marketplace(s) authorized.`
      : result.message;
    state.amazonSources = await fetchJson("/admin/integrations/amazon/sources");
    await loadSelectedAmazonSource();
    if (action === "sync") await loadData(); else renderAmazonIntegration();
  } catch (error) {
    document.getElementById("amazon-integration-message").textContent = error.message;
    state.amazonSources = await fetchJson("/admin/integrations/amazon/sources").catch(() => state.amazonSources);
    renderAmazonIntegration();
  } finally {
    setAmazonActionState(button, false, label);
  }
}

async function removeAmazonCredentials() {
  const source = selectedAmazonSource();
  if (!source || !window.confirm(`Remove stored credentials for ${source.name} and disable its automated sync?`)) return;
  const button = document.getElementById("amazon-remove-credentials");
  setAmazonActionState(button, true, "Removing…");
  try {
    await fetchJson(`/admin/integrations/amazon/sources/${source.id}/credentials`, { method: "DELETE" });
    state.amazonSources = await fetchJson("/admin/integrations/amazon/sources");
    renderAmazonIntegration();
    document.getElementById("amazon-integration-message").textContent = "Amazon credentials removed.";
  } catch (error) {
    document.getElementById("amazon-integration-message").textContent = error.message;
  } finally {
    setAmazonActionState(button, false, "Remove Credentials");
  }
}

function setAmazonActionState(button, busy, label) {
  button.disabled = busy;
  button.textContent = label;
}

function formatIntegrationTime(value) {
  if (!value) return "Never";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("en-CA", { dateStyle: "medium", timeStyle: "short" });
}

function renderSetupCards(editorId, type, rows) {
  const editor = document.getElementById(editorId);
  editor.innerHTML = "";
  rows.forEach((row) => addSetupCard(editorId, type, row));
}

function addSetupCard(editorId, type, row) {
  const editor = document.getElementById(editorId);
  const card = document.createElement("div");
  card.className = `setup-card setup-${type}`;
  card.dataset.type = type;
  card.innerHTML = setupCardTemplate(type, row);
  card.querySelector("button").addEventListener("click", () => card.remove());
  const titleInput = card.querySelector("[data-title-source]");
  if (titleInput) {
    const updateTitle = () => {
      card.querySelector(".setup-card-head strong").textContent =
        titleInput.value.trim() || titleInput.options?.[titleInput.selectedIndex]?.text || "New item";
    };
    titleInput.addEventListener("input", updateTitle);
    titleInput.addEventListener("change", updateTitle);
  }
  editor.appendChild(card);
}

const SETUP_FIELDS = {
  entity: [
    { key: "name", label: "Entity Name", titleSource: true, placeholder: "e.g. Acme Inc.", hint: "The legal entity name as it appears on marketplace and bank accounts." },
    { key: "currency", label: "Currency", placeholder: "e.g. USD", hint: "3-letter currency code (ISO 4217)." },
    { key: "status", type: "select", label: "Status", options: ["Active", "Inactive", "Under review"], hint: "Whether this entity is included in active cash reporting." },
  ],
  outflow: [
    { key: "name", label: "Outflow Name", titleSource: true, placeholder: "e.g. Vendor payments", hint: "Short name used in forecasts and reports." },
    { key: "category", type: "select", label: "Category", options: ["Operating outflow", "Payroll", "Tax", "Debt service", "Capital expenditure", "Other"], hint: "Used to group outflows in the forecast view." },
    { key: "amount", label: "Amount", placeholder: "e.g. 15000.00", hint: "Expected payment amount per occurrence. Use decimals (e.g. 15000.00)." },
    { key: "frequency", type: "select", label: "Frequency", options: ["Daily", "Weekly", "Bi-weekly", "Monthly", "Quarterly", "Annual", "One-time"], hint: "How often this outflow occurs." },
  ],
  horizon: [
    { key: "value", label: "Forecast Horizon", titleSource: true, placeholder: "e.g. 7 days", hint: "Time window shown in the Forecast view (e.g. '7 days', '30 days', '90 days')." },
  ],
  alert: [
    { key: "name", label: "Alert Name", titleSource: true, placeholder: "e.g. Delayed payout", hint: "Short label shown when this alert is triggered." },
    { key: "trigger", label: "Trigger Condition", placeholder: "e.g. Expected receipt is overdue by 3+ days", hint: "Describe the condition that should fire this alert." },
    { key: "recipient", label: "Recipient", placeholder: "e.g. Finance owner", hint: "Role or name that receives this alert notification." },
  ],
  role: [
    { key: "name", label: "Name", titleSource: true, placeholder: "e.g. CFO Office", hint: "Person or team assigned this role." },
    { key: "role", type: "select", label: "Role", options: ["Approver", "Reviewer", "Viewer", "Admin"], hint: "Level of access and approval authority." },
    { key: "scope", label: "Scope", placeholder: "e.g. All entities", hint: "Which entities or data this role can see (e.g. 'All entities' or a specific entity name)." },
  ],
};

function setupFieldHtml(field, currentValue) {
  const hint = field.hint ? `<small class="field-hint">${escapeHtml(field.hint)}</small>` : "";
  if (field.type === "select") {
    const options = field.options.map((opt) =>
      `<option value="${escapeAttribute(opt)}" ${currentValue === opt ? "selected" : ""}>${escapeHtml(opt)}</option>`
    ).join("");
    return `<label>${escapeHtml(field.label)}<select data-key="${escapeAttribute(field.key)}" ${field.titleSource ? "data-title-source" : ""}><option value="">— select —</option>${options}</select>${hint}</label>`;
  }
  return `<label>${escapeHtml(field.label)}<input data-key="${escapeAttribute(field.key)}" ${field.titleSource ? "data-title-source" : ""} value="${escapeAttribute(currentValue || "")}" placeholder="${escapeAttribute(field.placeholder || "")}">${hint}</label>`;
}

function setupCardTemplate(type, row) {
  const title = row.name || row.value || "New item";
  const fields = SETUP_FIELDS[type] || [];

  return `
    <div class="setup-card-head">
      <div>
        <span>${formatHeader(type)}</span>
        <strong>${escapeHtml(title)}</strong>
      </div>
      <button type="button" class="secondary-button">Remove</button>
    </div>
    <div class="setup-card-fields ${type === "horizon" ? "single-field" : ""}">
      ${fields.map((field) => setupFieldHtml(field, row[field.key])).join("")}
    </div>
  `;
}

function renderSourceEditor(id, sources) {
  const editor = document.getElementById(id);
  if (!editor) {
    return;
  }
  editor.innerHTML = "";
  sources.forEach((source) => addSourceRow(id, source));
}

const SOURCE_TYPES = [
  "Marketplace portal report",
  "Marketplace API",
  "Email / manual",
  "Bank / payment export",
];
const SOURCE_STATUSES = [
  "Configured",
  "Awaiting upload",
  "API pending",
  "Authorization pending",
  "Connected",
  "Active",
  "Inactive",
];
const SOURCE_FREQUENCIES = ["Daily", "Weekly", "Bi-weekly", "Monthly", "On demand"];

function sourceSelectHtml(cls, label, options, currentValue, hint) {
  const opts = options.map((opt) =>
    `<option value="${escapeAttribute(opt)}" ${currentValue === opt ? "selected" : ""}>${escapeHtml(opt)}</option>`
  ).join("");
  return `<label>${escapeHtml(label)}<select class="${cls}"><option value="">— select —</option>${opts}</select><small class="field-hint">${hint}</small></label>`;
}

const BANK_CONNECTION_TYPES = [
  "Bank CSV / statement export",
  "Open Banking API",
  "Bank direct API",
  "Payment processor export",
  "Manual entry",
];

function addSourceRow(editorId, source) {
  const editor = document.getElementById(editorId);
  if (!editor) return;
  const isBank = editorId === "bank-sources-editor";
  const row = document.createElement("div");
  row.className = "source-card";
  row.innerHTML = isBank ? _bankSourceCardHtml(source) : _marketplaceSourceCardHtml(source);
  row.querySelector(".remove-source-row-btn").addEventListener("click", () => row.remove());
  editor.appendChild(row);
}

function _marketplaceSourceCardHtml(source) {
  return `
    <div class="source-card-head">
      <div class="source-card-head-title">
        <span>Marketplace Source</span>
        <input class="source-name source-title-input" value="${escapeAttribute(source.name || "")}" placeholder="Click to name this source…" title="Click to edit source name">
      </div>
      <button type="button" class="secondary-button remove-source-row-btn">Remove</button>
    </div>
    <div class="source-card-fields">
      ${sourceSelectHtml("source-type", "Source Type", SOURCE_TYPES, source.type || "", "How data arrives.")}
      ${sourceSelectHtml("source-status", "Status", SOURCE_STATUSES, source.status || "", "Current connection state.")}
      <label>Owner
        <input class="source-owner" value="${escapeAttribute(source.owner || "")}" placeholder="e.g. Finance analyst">
      </label>
      ${sourceSelectHtml("source-frequency", "Report Frequency", SOURCE_FREQUENCIES, source.frequency || "", "How often a new report is expected.")}
      <label>Evidence / Report Name
        <input class="source-evidence" value="${escapeAttribute(source.evidence_type || "")}" placeholder="e.g. Amazon Payments transaction report">
      </label>
      <label>API Endpoint
        <input class="source-api-endpoint" value="${escapeAttribute(source.api_endpoint || "")}" placeholder="https://…">
        <small class="field-hint">Leave blank for file-based sources.</small>
      </label>
      <label>Credential Reference
        <input class="source-credential-reference" value="${escapeAttribute(source.credential_reference || "")}" placeholder="e.g. marketplace-amazon-ca-key">
        <small class="field-hint">Key name or vault path — never paste credentials.</small>
      </label>
    </div>`;
}

function _bankSourceCardHtml(source) {
  return `
    <div class="source-card-head">
      <div class="source-card-head-title">
        <span>Bank / Payment Source</span>
        <input class="source-name source-title-input" value="${escapeAttribute(source.name || "")}" placeholder="Click to name this source…" title="Click to edit source name">
      </div>
      <button type="button" class="secondary-button remove-source-row-btn">Remove</button>
    </div>
    <div class="source-card-fields">
      <label>Bank / Processor Name <small class="field-hint">e.g. RBC Royal Bank, Stripe, PayPal</small>
        <input class="source-bank-name" value="${escapeAttribute(source.bank_name || "")}" placeholder="e.g. RBC Royal Bank">
      </label>
      <label>Account Reference <small class="field-hint">Last 4 digits only — never full account number</small>
        <input class="source-account-ref" value="${escapeAttribute(source.account_reference || "")}" placeholder="e.g. ****1234">
      </label>
      <label>Currency
        <input class="source-currency" value="${escapeAttribute(source.currency || "")}" placeholder="e.g. USD">
      </label>
      ${sourceSelectHtml("source-connection-type", "Connection Type", BANK_CONNECTION_TYPES, source.connection_type || "", "How bank data will be loaded.")}
      ${sourceSelectHtml("source-status", "Status", SOURCE_STATUSES, source.status || "", "Current connection state.")}
      <label>Owner
        <input class="source-owner" value="${escapeAttribute(source.owner || "")}" placeholder="e.g. Treasury team">
      </label>
      ${sourceSelectHtml("source-frequency", "Statement Frequency", SOURCE_FREQUENCIES, source.frequency || "", "How often statements are available.")}
      <label>API Endpoint <small class="field-hint">Leave blank for CSV/manual sources</small>
        <input class="source-api-endpoint" value="${escapeAttribute(source.api_endpoint || "")}" placeholder="https://…">
      </label>
      <label>Credential Reference <small class="field-hint">Key name or vault path — never paste credentials</small>
        <input class="source-credential-reference" value="${escapeAttribute(source.credential_reference || "")}" placeholder="e.g. bank-rbc-api-key">
      </label>
    </div>`;
}

async function saveTenantSettings() {
  const status = document.getElementById("settings-save-status");
  status.textContent = "Saving";
  const payload = {
    product_name: value("settings-product-name"),
    tenant_name: value("settings-tenant-name"),
    poc_entity: value("settings-poc-entity"),
    brand: {
      primary_color: value("settings-primary-color"),
      logo_text: value("settings-logo-text"),
    },
    currencies: value("settings-currencies")
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean),
    reconciliation: {
      ...state.tenant.reconciliation,
      date_tolerance_days: Number(value("settings-date-tolerance") || 0),
      amount_tolerance: value("settings-amount-tolerance") || "0.00",
    },
    sources: {
      marketplaces: state.tenant.sources?.marketplaces || [],
      banks: state.tenant.sources?.banks || [],
    },
    client_setup: {
      entities: readSetupCards("entities-editor"),
      planned_outflows: readSetupCards("outflows-editor"),
      forecast_horizons: readSetupCards("horizons-editor")
        .map((item) => item.value)
        .filter(Boolean),
      alert_rules: readSetupCards("alerts-editor"),
      user_roles: readSetupCards("roles-editor"),
      governance: {
        data_retention: value("settings-data-retention"),
        hosting_region: value("settings-hosting-region"),
        ai_enabled: document.getElementById("settings-ai-enabled").checked,
        human_approval_required: document.getElementById("settings-human-approval")
          .checked,
      },
    },
  };
  const identityError = validateWorkspaceIdentity(payload);
  if (identityError) {
    status.textContent = identityError;
    return;
  }
  try {
    state.tenant = await fetchJson("/tenant/settings", { method: "PUT", body: JSON.stringify(payload) });
    status.textContent = "Saved";
    render();
    setTimeout(() => { if (status.textContent === "Saved") status.textContent = ""; }, 3000);
  } catch (err) {
    status.textContent = err.message.includes("422") ? "Validation error — check required fields" : "Save failed — please retry";
  }
}

function validateWorkspaceIdentity(payload) {
  const marketplaceLabels = payload.sources.marketplaces.flatMap((source) => {
    const name = source.name || "";
    return name.includes(" - ") ? [name, name.split(" - ").pop()] : [name];
  }).map(normalizeIdentity).filter(Boolean);
  const identityValues = [
    payload.tenant_name,
    payload.poc_entity,
    ...payload.client_setup.entities.map((entity) => entity.name || ""),
  ];
  const hasMarketplaceIdentity = identityValues
    .map(normalizeIdentity)
    .some((value) => marketplaceLabels.some((label) => label && value.includes(label)));
  return hasMarketplaceIdentity
    ? "Amazon account names belong in the SP-API registry."
    : "";
}

function normalizeIdentity(value) {
  return String(value || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
}

function readSourceEditor(id) {
  return Array.from(document.querySelectorAll(`#${id} .source-card`))
    .map((row) => ({
      name: row.querySelector(".source-name").value.trim(),
      type: row.querySelector(".source-type")?.value.trim() || "",
      status: row.querySelector(".source-status")?.value.trim() || "",
      owner: row.querySelector(".source-owner")?.value.trim() || "",
      frequency: row.querySelector(".source-frequency")?.value.trim() || "",
      evidence_type: row.querySelector(".source-evidence")?.value.trim() || "",
      api_endpoint: row.querySelector(".source-api-endpoint")?.value.trim() || "",
      credential_reference: row.querySelector(".source-credential-reference")?.value.trim() || "",
    }))
    .filter((s) => s.name);
}

function readBankSourceEditor(id) {
  return Array.from(document.querySelectorAll(`#${id} .source-card`))
    .map((row) => ({
      name: row.querySelector(".source-name")?.value.trim() || "",
      bank_name: row.querySelector(".source-bank-name")?.value.trim() || "",
      account_reference: row.querySelector(".source-account-ref")?.value.trim() || "",
      currency: row.querySelector(".source-currency")?.value.trim() || "",
      connection_type: row.querySelector(".source-connection-type")?.value.trim() || "",
      status: row.querySelector(".source-status")?.value.trim() || "",
      owner: row.querySelector(".source-owner")?.value.trim() || "",
      frequency: row.querySelector(".source-frequency")?.value.trim() || "",
      api_endpoint: row.querySelector(".source-api-endpoint")?.value.trim() || "",
      credential_reference: row.querySelector(".source-credential-reference")?.value.trim() || "",
    }))
    .filter((s) => s.name);
}

function readSetupCards(id) {
  return Array.from(document.querySelectorAll(`#${id} .setup-card`))
    .map((card) => {
      const item = {};
      card.querySelectorAll("[data-key]").forEach((input) => {
        item[input.dataset.key] = input.value.trim();
      });
      return item;
    })
    .filter((item) => Object.values(item).some(Boolean));
}

function formatHeader(key) {
  return escapeHtml(key.replaceAll("_", " ").replace(/\b\w/g, (c) => c.toUpperCase()));
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function escapeAttribute(value) {
  return escapeHtml(value).replaceAll("`", "&#096;");
}

function moneyNumber(value) {
  const parsed = Number(String(value ?? "").replace(/[^0-9.-]/g, ""));
  return Number.isFinite(parsed) ? parsed : 0;
}

function formatMoney(value) {
  return Number(value || 0).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function formatUsd(value) {
  return `USD ${formatMoney(moneyNumber(value))}`;
}

showView(location.hash.slice(1) || "overview");
loadData();
