const state = {
  user: null,
  permissions: [],
  strategyCatalog: [],
  strategyConfigs: [],
  backtestRuns: [],
  tradeOrders: [],
  riskSettings: null,
  dataSources: [],
  watchlists: [],
  indicatorCatalog: [],
  auditLogs: [],
  exchangeSyncRuns: [],
  marketSnapshots: [],
  signalLogs: [],
  executionEvents: [],
  automationStatus: null,
  notificationChannels: [],
  notifications: [],
  rolePermissions: {},
  permissionCatalog: [],
  activePage: "status",
};

const PERMISSIONS = {
  VIEW_DASHBOARD: "dashboard:view",
  VIEW_BACKTESTS: "backtests:view",
  RUN_BACKTESTS: "backtests:run",
  MANAGE_API_KEYS: "api_keys:manage_self",
  MANAGE_ALL_API_KEYS: "api_keys:manage_all",
  VIEW_STRATEGIES: "strategies:view",
  MANAGE_STRATEGIES: "strategies:manage_self",
  MANAGE_ALL_STRATEGIES: "strategies:manage_all",
  VIEW_MONITORING: "monitoring:view",
  VIEW_TRADES: "trades:view_self",
  VIEW_ALL_TRADES: "trades:view_all",
  EXECUTE_PAPER_TRADES: "trading:execute_paper",
  MANAGE_SYSTEM: "system:manage",
  MANAGE_USERS: "users:manage",
};

const PERMISSION_LABELS = {
  "api_keys:manage_all": "管理全部 API",
  "api_keys:manage_self": "管理 API",
  "backtests:run": "运行回测",
  "backtests:view": "查看回测",
  "dashboard:view": "查看面板",
  "monitoring:view": "查看监控",
  "strategies:manage_all": "管理全部策略",
  "strategies:manage_self": "管理策略",
  "strategies:view": "查看策略",
  "system:manage": "系统管理",
  "trades:view_all": "查看全部交易",
  "trades:view_self": "查看交易",
  "trading:execute_live": "实盘权限",
  "trading:execute_paper": "Paper 交易",
  "users:manage": "用户管理",
};

function hasPermission(permission) {
  return state.permissions.includes(permission);
}

function roleDefaultPermissions(role) {
  return new Set(state.rolePermissions[role] || []);
}

function userEffectivePermissions(user) {
  if (Array.isArray(user.permissions)) return new Set(user.permissions);
  const defaults = roleDefaultPermissions(user.role);
  const grants = new Set(user.permission_grants || []);
  const revokes = new Set(user.permission_revokes || []);
  grants.forEach((permission) => defaults.add(permission));
  revokes.forEach((permission) => defaults.delete(permission));
  return defaults;
}

function visiblePagesForPermissions(permissions) {
  const pages = [];
  if (
    permissions.has(PERMISSIONS.VIEW_DASHBOARD) ||
    permissions.has(PERMISSIONS.VIEW_MONITORING) ||
    permissions.has(PERMISSIONS.EXECUTE_PAPER_TRADES)
  ) {
    pages.push("状态");
  }
  if (
    permissions.has(PERMISSIONS.VIEW_TRADES) ||
    permissions.has(PERMISSIONS.VIEW_ALL_TRADES) ||
    permissions.has(PERMISSIONS.EXECUTE_PAPER_TRADES)
  ) {
    pages.push("交易记录");
  }
  if (
    permissions.has(PERMISSIONS.VIEW_BACKTESTS) ||
    permissions.has(PERMISSIONS.RUN_BACKTESTS) ||
    permissions.has(PERMISSIONS.VIEW_STRATEGIES) ||
    permissions.has(PERMISSIONS.MANAGE_API_KEYS) ||
    permissions.has(PERMISSIONS.MANAGE_USERS) ||
    permissions.has(PERMISSIONS.MANAGE_SYSTEM)
  ) {
    pages.push("配置");
  }
  return pages;
}

function hasTradePageAccess() {
  return (
    hasPermission(PERMISSIONS.VIEW_TRADES) ||
    hasPermission(PERMISSIONS.VIEW_ALL_TRADES) ||
    hasPermission(PERMISSIONS.EXECUTE_PAPER_TRADES)
  );
}

function hasConfigPageAccess() {
  return (
    hasPermission(PERMISSIONS.VIEW_BACKTESTS) ||
    hasPermission(PERMISSIONS.RUN_BACKTESTS) ||
    hasPermission(PERMISSIONS.VIEW_STRATEGIES) ||
    hasPermission(PERMISSIONS.MANAGE_API_KEYS) ||
    hasPermission(PERMISSIONS.MANAGE_ALL_API_KEYS) ||
    hasPermission(PERMISSIONS.MANAGE_ALL_STRATEGIES) ||
    hasPermission(PERMISSIONS.MANAGE_USERS) ||
    hasPermission(PERMISSIONS.MANAGE_SYSTEM)
  );
}

function isPageAllowed(page) {
  if (page === "status") {
    return (
      hasPermission(PERMISSIONS.VIEW_DASHBOARD) ||
      hasPermission(PERMISSIONS.VIEW_MONITORING) ||
      hasPermission(PERMISSIONS.EXECUTE_PAPER_TRADES)
    );
  }
  if (page === "trades") return hasTradePageAccess();
  if (page === "config") return hasConfigPageAccess();
  return false;
}

function setActivePage(page = state.activePage) {
  const requestedPage = ["status", "trades", "config"].includes(page) ? page : "status";
  const allowedPages = ["status", "trades", "config"].filter(isPageAllowed);
  const nextPage = allowedPages.includes(requestedPage) ? requestedPage : allowedPages[0] || "status";

  state.activePage = nextPage;
  document.querySelectorAll("[data-page]").forEach((pageElement) => {
    pageElement.hidden = pageElement.dataset.page !== nextPage || !isPageAllowed(pageElement.dataset.page);
  });
  document.querySelectorAll("[data-page-tab]").forEach((button) => {
    const isActive = button.dataset.pageTab === nextPage;
    button.hidden = !isPageAllowed(button.dataset.pageTab);
    button.classList.toggle("active", isActive);
    button.setAttribute("aria-current", isActive ? "page" : "false");
  });

  if (state.user && window.location.hash !== `#${nextPage}`) {
    window.history.replaceState(null, "", `#${nextPage}`);
  }
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });

  if (response.status === 401) {
    showLogin();
    throw new Error("not authenticated");
  }

  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.detail || `request failed: ${response.status}`);
  }
  return data;
}

function showLogin() {
  document.getElementById("login-view").hidden = false;
  document.getElementById("app-view").hidden = true;
  window.history.replaceState(null, "", window.location.pathname);
}

function showApp() {
  document.getElementById("login-view").hidden = true;
  document.getElementById("app-view").hidden = false;
}

function toast(message) {
  const box = document.getElementById("toast");
  box.textContent = message;
  box.hidden = false;
  window.clearTimeout(box.dataset.timer);
  box.dataset.timer = window.setTimeout(() => {
    box.hidden = true;
  }, 3200);
}

function setResult(elementId, message, type = "") {
  const box = document.getElementById(elementId);
  box.classList.remove("success", "warning", "danger");
  if (type) box.classList.add(type);
  box.textContent = message;
}

function formatPercent(value) {
  return `${(Number(value) * 100).toFixed(2)}%`;
}

function formatMoney(value) {
  return Number(value).toLocaleString(undefined, { maximumFractionDigits: 2 });
}

function formatNumber(value) {
  return Number(value).toLocaleString(undefined, { maximumFractionDigits: 0 });
}

function inputNumber(id, fallback = 0) {
  const value = Number(document.getElementById(id).value);
  return Number.isFinite(value) ? value : fallback;
}

function formatDate(value) {
  if (!value) return "";
  return String(value).replace("T", " ").replace("+00:00", " UTC");
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function renderPermissionChips() {
  document.getElementById("current-permissions").innerHTML = state.permissions
    .map((permission) => `<span class="chip">${escapeHtml(PERMISSION_LABELS[permission] || permission)}</span>`)
    .join("");
}

function applySessionPayload(data) {
  state.user = data.user;
  state.permissions = data.permissions || data.user?.permissions || [];
  state.rolePermissions = data.role_permissions || state.rolePermissions || {};
  state.permissionCatalog = data.permission_catalog || state.permissionCatalog || [];
}

function defaultDataOutputFile(provider, symbol, interval) {
  const safeSymbol = String(symbol || "SYMBOL").toUpperCase().replace(/[^A-Z0-9_-]+/g, "_").replace(/^_+|_+$/g, "");
  const safeInterval = String(interval || "1h").replace(/[^A-Za-z0-9_-]+/g, "_").replace(/^_+|_+$/g, "");
  return `data/${provider}_${safeSymbol || "SYMBOL"}_${safeInterval || "1h"}.csv`;
}

function monitoringQuery(limit = null, extra = {}) {
  const params = new URLSearchParams();
  if (hasPermission(PERMISSIONS.VIEW_ALL_TRADES)) params.set("all", "1");
  if (limit !== null) params.set("limit", String(limit));
  Object.entries(extra).forEach(([key, value]) => {
    if (value !== null && value !== undefined && value !== "") params.set(key, String(value));
  });
  const query = params.toString();
  return query ? `?${query}` : "";
}

async function loadSession() {
  try {
    const data = await api("/api/me");
    applySessionPayload(data);
    await loadStrategyCatalog();
    renderShell();
    await Promise.all([
      loadMonitoring(),
      loadPaperTrading(),
      loadOrderHistory(),
      loadDataSources(),
      loadIndicatorCatalog(),
      loadWatchlists(),
      loadStrategyConfigs(),
      loadBacktestHistory(),
      loadRiskSettings(),
      loadApiKeys(),
      loadUsers(),
      loadAuditLogs(),
      loadExchangeSyncRuns(),
      loadRealtime(),
      loadExecutionEvents(),
      loadAutomationStatus(),
      loadNotifications(),
    ]);
    showApp();
  } catch (error) {
    showLogin();
  }
}

function renderShell() {
  document.getElementById("current-user").textContent = state.user.email;
  document.getElementById("current-role").textContent = state.user.role;
  renderPermissionChips();
  document.getElementById("monitoring-section").hidden = !hasPermission(PERMISSIONS.VIEW_MONITORING);
  document.getElementById("paper-status-section").hidden = !hasPermission(PERMISSIONS.EXECUTE_PAPER_TRADES);
  document.getElementById("realtime-section").hidden = !(
    hasPermission(PERMISSIONS.VIEW_MONITORING) ||
    hasPermission(PERMISSIONS.RUN_BACKTESTS) ||
    hasPermission(PERMISSIONS.EXECUTE_PAPER_TRADES)
  );
  document.getElementById("trading-section").hidden = !hasPermission(PERMISSIONS.EXECUTE_PAPER_TRADES);
  document.getElementById("order-status-section").hidden = !hasTradePageAccess();
  document.getElementById("trade-records-section").hidden = !hasTradePageAccess();
  document.getElementById("execution-events-section").hidden = !hasTradePageAccess();
  document.getElementById("strategy-section").hidden = !(
    hasPermission(PERMISSIONS.VIEW_STRATEGIES) ||
    hasPermission(PERMISSIONS.MANAGE_STRATEGIES) ||
    hasPermission(PERMISSIONS.MANAGE_ALL_STRATEGIES)
  );
  document.getElementById("save-strategy-config").disabled = !hasPermission(PERMISSIONS.MANAGE_STRATEGIES);
  document.getElementById("save-watchlist").disabled = !hasPermission(PERMISSIONS.MANAGE_STRATEGIES);
  document.getElementById("backtest-section").hidden = !(
    hasPermission(PERMISSIONS.VIEW_BACKTESTS) || hasPermission(PERMISSIONS.RUN_BACKTESTS)
  );
  document.getElementById("risk-section").hidden = !hasPermission(PERMISSIONS.EXECUTE_PAPER_TRADES);
  document.getElementById("sync-section").hidden = !(
    hasPermission(PERMISSIONS.EXECUTE_PAPER_TRADES) || hasPermission(PERMISSIONS.VIEW_ALL_TRADES)
  );
  document.getElementById("automation-section").hidden = !(
    hasPermission(PERMISSIONS.EXECUTE_PAPER_TRADES) ||
    hasPermission(PERMISSIONS.MANAGE_SYSTEM) ||
    hasPermission(PERMISSIONS.VIEW_ALL_TRADES)
  );
  document.getElementById("notification-section").hidden = !(
    hasPermission(PERMISSIONS.EXECUTE_PAPER_TRADES) ||
    hasPermission(PERMISSIONS.MANAGE_USERS) ||
    hasPermission(PERMISSIONS.VIEW_ALL_TRADES)
  );
  document.getElementById("data-source-section").hidden = !(hasPermission(PERMISSIONS.RUN_BACKTESTS) || hasPermission(PERMISSIONS.VIEW_STRATEGIES));
  document.getElementById("watchlist-section").hidden = !(
    hasPermission(PERMISSIONS.VIEW_STRATEGIES) ||
    hasPermission(PERMISSIONS.RUN_BACKTESTS) ||
    hasPermission(PERMISSIONS.MANAGE_STRATEGIES) ||
    hasPermission(PERMISSIONS.MANAGE_ALL_STRATEGIES)
  );
  document.getElementById("indicator-section").hidden = !hasPermission(PERMISSIONS.RUN_BACKTESTS);
  document.getElementById("api-section").hidden = !(
    hasPermission(PERMISSIONS.MANAGE_API_KEYS) || hasPermission(PERMISSIONS.MANAGE_ALL_API_KEYS)
  );
  document.getElementById("users-section").hidden = !hasPermission(PERMISSIONS.MANAGE_USERS);
  document.getElementById("audit-section").hidden = !(hasPermission(PERMISSIONS.MANAGE_USERS) || hasPermission(PERMISSIONS.MANAGE_SYSTEM));
  document.getElementById("run-backtest").disabled = !hasPermission(PERMISSIONS.RUN_BACKTESTS);
  setActivePage(window.location.hash.replace("#", "") || state.activePage);
}

async function login(event) {
  event.preventDefault();
  document.getElementById("login-error").textContent = "";
  try {
    const data = await api("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({
        email: document.getElementById("login-email").value,
        password: document.getElementById("login-password").value,
      }),
    });
    applySessionPayload(data);
    await loadStrategyCatalog();
    renderShell();
    await Promise.all([
      loadMonitoring(),
      loadPaperTrading(),
      loadOrderHistory(),
      loadDataSources(),
      loadIndicatorCatalog(),
      loadWatchlists(),
      loadStrategyConfigs(),
      loadBacktestHistory(),
      loadRiskSettings(),
      loadApiKeys(),
      loadUsers(),
      loadAuditLogs(),
      loadExchangeSyncRuns(),
      loadRealtime(),
      loadExecutionEvents(),
      loadAutomationStatus(),
      loadNotifications(),
    ]);
    showApp();
  } catch (error) {
    document.getElementById("login-error").textContent = "登录失败";
  }
}

async function logout() {
  await api("/api/auth/logout", { method: "POST" }).catch(() => {});
  state.user = null;
  state.permissions = [];
  state.rolePermissions = {};
  state.permissionCatalog = [];
  showLogin();
}

async function changePassword(event) {
  event.preventDefault();
  const currentPassword = document.getElementById("current-password").value;
  const newPassword = document.getElementById("new-password").value;
  const confirmPassword = document.getElementById("confirm-password").value;

  if (newPassword.length < 8) {
    setResult("account-security-result", "新密码至少需要 8 位。", "danger");
    return;
  }
  if (newPassword !== confirmPassword) {
    setResult("account-security-result", "两次输入的新密码不一致。", "danger");
    return;
  }

  setResult("account-security-result", "正在修改密码...");
  try {
    await api("/api/me/password", {
      method: "POST",
      body: JSON.stringify({
        current_password: currentPassword,
        new_password: newPassword,
      }),
    });
    document.getElementById("change-password-form").reset();
    setResult("account-security-result", "密码已更新。当前登录会保留，其他设备需要重新登录。", "success");
    toast("密码已更新");
  } catch (error) {
    setResult("account-security-result", error.message === "invalid_current_password" ? "当前密码不正确。" : error.message, "danger");
  }
}

async function runBacktest() {
  const box = document.getElementById("backtest-result");
  box.textContent = "运行中...";
  const strategyConfigId = document.getElementById("backtest-strategy-config").value;
  const payload = {
    bars_file: document.getElementById("bars-file").value,
    events_file: document.getElementById("events-file").value,
    ...collectBacktestSettings(),
  };
  if (strategyConfigId) {
    payload.strategy_config_id = Number(strategyConfigId);
  } else {
    payload.strategy_type = document.getElementById("strategy-type").value || "statistical_event";
    payload.market_type = document.getElementById("market-type").value || "spot";
    payload.strategy_parameters = collectStrategyParameters();
  }

  try {
    const data = await api("/api/backtests/run", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    renderBacktestResult(data);
    await Promise.all([loadMonitoring(), loadBacktestHistory()]);
  } catch (error) {
    box.textContent = error.message;
  }
}

function collectBacktestSettings() {
  return {
    cash: inputNumber("cash", 10000),
    fee_bps: inputNumber("fee-bps", 5),
    slippage_bps: inputNumber("slippage-bps", 2),
    execution_delay_bars: inputNumber("execution-delay-bars", 0),
    funding_bps_per_bar: inputNumber("funding-bps-per-bar", 0),
    stop_loss_pct: inputNumber("stop-loss-pct", 0),
    take_profit_pct: inputNumber("take-profit-pct", 0),
    max_holding_bars: inputNumber("max-holding-bars", 0),
    leverage: inputNumber("leverage", 1),
    maintenance_margin_ratio: inputNumber("maintenance-margin-ratio", 0.005),
    out_of_sample_ratio: inputNumber("out-of-sample-ratio", 0),
    max_order_notional: inputNumber("backtest-max-order-notional", 2500),
    max_symbol_weight: inputNumber("backtest-max-symbol-weight", 0.35),
    max_gross_exposure: inputNumber("backtest-max-gross-exposure", 0.8),
  };
}

function renderBacktestResult(data) {
  const metrics = data.metrics;
  const fills = data.fills || [];
  const validation = data.validation || metrics.validation;
  const fillRows = fills
    .map(
      (fill) => `
        <tr>
          <td>${fill.timestamp}</td>
          <td>${fill.symbol}</td>
          <td>${fill.side}</td>
          <td>${Number(fill.quantity).toFixed(8)}</td>
          <td>${Number(fill.price).toFixed(4)}</td>
          <td>${fill.reason}</td>
        </tr>`
    )
    .join("");

  document.getElementById("backtest-result").innerHTML = `
    <div class="result-meta">策略：${escapeHtml(data.strategy?.config?.name || data.strategy?.strategy_type || "")}</div>
    <div class="metric-grid">
      <div class="metric"><span class="label">结束权益</span><strong>${formatMoney(metrics.ending_equity)}</strong></div>
      <div class="metric"><span class="label">收益率</span><strong>${formatPercent(metrics.total_return)}</strong></div>
      <div class="metric"><span class="label">最大回撤</span><strong>${formatPercent(metrics.max_drawdown)}</strong></div>
      <div class="metric"><span class="label">Sharpe</span><strong>${Number(metrics.sharpe).toFixed(2)}</strong></div>
      <div class="metric"><span class="label">成交次数</span><strong>${formatNumber(metrics.trade_count)}</strong></div>
      <div class="metric"><span class="label">手续费</span><strong>${formatMoney(metrics.total_fees)}</strong></div>
      <div class="metric"><span class="label">资金费</span><strong>${formatMoney(metrics.total_funding || 0)}</strong></div>
      <div class="metric"><span class="label">爆仓 / 强平</span><strong>${formatNumber(metrics.liquidation_count || 0)} / ${formatNumber(metrics.forced_exit_count || 0)}</strong></div>
    </div>
    ${renderValidationBlock(validation)}
    <div class="table-wrap">
      <table>
        <thead><tr><th>时间</th><th>币种</th><th>方向</th><th>数量</th><th>价格</th><th>原因</th></tr></thead>
        <tbody>${fillRows || '<tr><td colspan="6">无成交</td></tr>'}</tbody>
      </table>
    </div>
  `;
}

function renderValidationBlock(validation) {
  if (!validation) return "";
  if (validation.status !== "completed") {
    return `<div class="result-meta">样本外验证：${escapeHtml(validation.reason || validation.status || "skipped")}</div>`;
  }
  return `
    <div class="validation-grid">
      <div class="metric"><span class="label">样本内收益</span><strong>${formatPercent(validation.in_sample.total_return || 0)}</strong></div>
      <div class="metric"><span class="label">样本外收益</span><strong>${formatPercent(validation.out_of_sample.total_return || 0)}</strong></div>
      <div class="metric"><span class="label">样本外回撤</span><strong>${formatPercent(validation.out_of_sample.max_drawdown || 0)}</strong></div>
      <div class="metric"><span class="label">样本外成交</span><strong>${formatNumber(validation.out_of_sample.trade_count || 0)}</strong></div>
    </div>`;
}

async function runGridSearch() {
  const box = document.getElementById("grid-search-result");
  box.textContent = "参数优化中...";
  let parameterGrid = {};
  try {
    parameterGrid = JSON.parse(document.getElementById("grid-search-json").value || "{}");
  } catch (error) {
    box.textContent = "参数网格必须是合法 JSON。";
    box.classList.add("danger");
    return;
  }

  const strategyConfigId = document.getElementById("backtest-strategy-config").value;
  const payload = {
    bars_file: document.getElementById("bars-file").value,
    events_file: document.getElementById("events-file").value,
    ...collectBacktestSettings(),
    parameter_grid: parameterGrid,
    max_combinations: inputNumber("grid-max-combinations", 50),
  };
  delete payload.out_of_sample_ratio;

  if (strategyConfigId) {
    payload.strategy_config_id = Number(strategyConfigId);
  } else {
    payload.strategy_type = document.getElementById("strategy-type").value || "statistical_event";
    payload.market_type = document.getElementById("market-type").value || "spot";
    payload.strategy_parameters = collectStrategyParameters();
  }

  try {
    const data = await api("/api/backtests/grid-search", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    renderGridSearchResult(data);
  } catch (error) {
    box.textContent = error.message;
    box.classList.add("danger");
  }
}

function renderGridSearchResult(data) {
  const rows = (data.results || [])
    .slice(0, 30)
    .map(
      (item) => `
        <tr>
          <td>${item.rank}</td>
          <td>${formatPercent(item.metrics.total_return || 0)}</td>
          <td>${formatPercent(item.metrics.max_drawdown || 0)}</td>
          <td>${Number(item.metrics.sharpe || 0).toFixed(2)}</td>
          <td>${formatNumber(item.metrics.trade_count || 0)}</td>
          <td>${formatMoney(item.metrics.total_funding || 0)}</td>
          <td><code>${escapeHtml(JSON.stringify(item.parameters))}</code></td>
        </tr>`
    )
    .join("");

  document.getElementById("grid-search-result").classList.remove("danger");
  document.getElementById("grid-search-result").innerHTML = `
    <div class="result-meta">共测试 ${formatNumber(data.combination_count)} 组参数，按总收益率排序。</div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>排名</th><th>收益</th><th>回撤</th><th>Sharpe</th><th>成交</th><th>资金费</th><th>参数</th></tr></thead>
        <tbody>${rows || '<tr><td colspan="7">暂无结果</td></tr>'}</tbody>
      </table>
    </div>
  `;
}

async function loadMonitoring() {
  const tasks = [];
  if (hasPermission(PERMISSIONS.VIEW_MONITORING)) {
    tasks.push(
      Promise.all([api(`/api/monitoring/summary${monitoringQuery()}`), api(`/api/trade-runs${monitoringQuery(20)}`)]).then(
        ([summary, runs]) => {
          renderMonitoringSummary(summary);
          renderTradeRuns(runs);
        }
      )
    );
  }
  if (hasTradePageAccess()) {
    tasks.push(api(`/api/trade-records${monitoringQuery(50)}`).then(renderTradeRecords));
  }
  await Promise.all(tasks);
}

async function loadOrderHistory() {
  if (!hasTradePageAccess()) return;
  state.tradeOrders = await api(`/api/orders${monitoringQuery(50)}`);
  renderOrderHistory(state.tradeOrders);
}

async function loadPaperTrading() {
  if (!hasPermission(PERMISSIONS.EXECUTE_PAPER_TRADES)) return;
  const [account, positions] = await Promise.all([
    api("/api/paper-trading/account"),
    api(`/api/paper-trading/positions${hasPermission(PERMISSIONS.VIEW_ALL_TRADES) ? "?all=1" : ""}`),
  ]);
  renderPaperAccount(account, positions);
  renderPaperPositions(positions);
}

async function loadRealtime() {
  if (
    !(
      hasPermission(PERMISSIONS.VIEW_MONITORING) ||
      hasPermission(PERMISSIONS.RUN_BACKTESTS) ||
      hasPermission(PERMISSIONS.EXECUTE_PAPER_TRADES)
    )
  ) {
    return;
  }
  const query = hasPermission(PERMISSIONS.VIEW_ALL_TRADES) ? "?all=1&limit=50" : "?limit=50";
  const [snapshots, signals] = await Promise.all([api("/api/realtime/snapshots?limit=20"), api(`/api/realtime/signals${query}`)]);
  state.marketSnapshots = snapshots;
  state.signalLogs = signals;
  renderRealtime();
}

async function refreshMarketSnapshot() {
  const symbol = document.getElementById("realtime-symbol").value || "BTCUSDT";
  const snapshot = await api(`/api/realtime/snapshot?symbol=${encodeURIComponent(symbol)}`);
  state.marketSnapshots = [snapshot].concat(state.marketSnapshots || []).slice(0, 20);
  renderRealtime();
  toast("行情已刷新");
}

async function evaluateRealtimeSignal() {
  const payload = {
    symbol: document.getElementById("realtime-symbol").value || "BTCUSDT",
    strategy_type: "manual_signal",
    parameters: {},
  };
  const strategyConfigId = document.getElementById("signal-strategy-config").value;
  if (strategyConfigId) payload.strategy_config_id = Number(strategyConfigId);
  const signal = await api("/api/realtime/signals/evaluate", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  state.signalLogs = [signal].concat(state.signalLogs || []).slice(0, 50);
  await loadRealtime();
  toast("信号已记录");
}

function renderRealtime() {
  const latest = state.marketSnapshots[0];
  if (!latest) {
    document.getElementById("realtime-summary").innerHTML = `
      <div class="metric"><span class="label">行情</span><strong>暂无</strong></div>
    `;
  } else {
    const kline = latest.kline || {};
    const orderBook = latest.order_book || {};
    document.getElementById("realtime-summary").innerHTML = `
      <div class="metric"><span class="label">交易对</span><strong>${escapeHtml(latest.symbol)}</strong></div>
      <div class="metric"><span class="label">最新价</span><strong>${formatMoney(latest.last_price)}</strong></div>
      <div class="metric"><span class="label">买一/卖一</span><strong>${formatMoney(latest.best_bid)} / ${formatMoney(latest.best_ask)}</strong></div>
      <div class="metric"><span class="label">点差 bps</span><strong>${Number(latest.spread_bps || 0).toFixed(2)}</strong></div>
      <div class="metric"><span class="label">K线</span><strong>${formatMoney(kline.open || 0)} → ${formatMoney(kline.close || 0)}</strong></div>
      <div class="metric"><span class="label">深度档位</span><strong>${formatNumber(orderBook.depth || 0)}</strong></div>
    `;
  }

  const rows = state.signalLogs
    .map(
      (item) => `
        <tr>
          <td>${item.id}</td>
          <td>${formatDate(item.created_at)}</td>
          <td>${escapeHtml(item.owner_email || "")}</td>
          <td>${escapeHtml(item.symbol)}</td>
          <td><span class="chip ${item.signal === "buy" ? "success" : item.signal === "sell" ? "danger" : "muted"}">${escapeHtml(item.signal)}</span></td>
          <td>${Number(item.score || 0).toFixed(4)}</td>
          <td>${escapeHtml(item.strategy_name || item.strategy_type)}</td>
          <td>${escapeHtml(item.reason || "")}</td>
        </tr>`
    )
    .join("");
  document.getElementById("signal-logs-table").innerHTML = `
    <table>
      <thead><tr><th>ID</th><th>时间</th><th>用户</th><th>交易对</th><th>信号</th><th>分数</th><th>策略</th><th>原因</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="8">暂无信号日志</td></tr>'}</tbody>
    </table>
  `;
}

async function loadExecutionEvents() {
  if (!hasTradePageAccess()) return;
  state.executionEvents = await api(`/api/execution-events${monitoringQuery(80)}`);
  renderExecutionEvents();
}

function renderExecutionEvents() {
  const rows = state.executionEvents
    .map(
      (event) => `
        <tr>
          <td>${event.id}</td>
          <td>${formatDate(event.created_at)}</td>
          <td>${escapeHtml(event.owner_email || "")}</td>
          <td>${escapeHtml(event.event_type)}</td>
          <td><span class="chip ${event.status === "filled" ? "success" : event.error_message ? "danger" : "warning"}">${escapeHtml(event.status || "-")}</span></td>
          <td>${event.order_id ? `#${event.order_id}` : "-"}</td>
          <td>${event.price_before ? formatMoney(event.price_before) : "-"}</td>
          <td>${event.price_after ? formatMoney(event.price_after) : "-"}</td>
          <td>${formatNumber(event.retry_count || 0)}</td>
          <td>${escapeHtml(event.error_message || "")}</td>
          <td><code>${escapeHtml(JSON.stringify((event.risk_checks || []).slice(0, 4)))}</code></td>
        </tr>`
    )
    .join("");
  document.getElementById("execution-events-table").innerHTML = `
    <table>
      <thead><tr><th>ID</th><th>时间</th><th>用户</th><th>事件</th><th>状态</th><th>订单</th><th>下单前</th><th>成交后</th><th>重试</th><th>错误</th><th>风控</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="11">暂无执行审计</td></tr>'}</tbody>
    </table>
  `;
}

async function loadAutomationStatus() {
  if (
    !(
      hasPermission(PERMISSIONS.EXECUTE_PAPER_TRADES) ||
      hasPermission(PERMISSIONS.MANAGE_SYSTEM) ||
      hasPermission(PERMISSIONS.VIEW_ALL_TRADES)
    )
  ) {
    return;
  }
  const query = hasPermission(PERMISSIONS.VIEW_ALL_TRADES) ? "?all=1" : "";
  state.automationStatus = await api(`/api/automation/status${query}`);
  renderAutomationStatus();
}

async function automationControl(action) {
  setResult("automation-result", "正在更新自动交易状态...");
  const strategyConfigId = document.getElementById("automation-strategy-config").value;
  const payload = {
    action,
    run_mode: document.getElementById("automation-run-mode").value,
  };
  if (strategyConfigId) payload.strategy_config_id = Number(strategyConfigId);
  try {
    const control = await api("/api/automation/control", { method: "POST", body: JSON.stringify(payload) });
    setResult("automation-result", `状态已更新：${control.status}`, "success");
    await Promise.all([loadAutomationStatus(), loadNotifications(), loadAuditLogs()]);
  } catch (error) {
    setResult("automation-result", error.message, "danger");
  }
}

async function triggerEmergencyStop() {
  setResult("automation-result", "正在触发紧急停止...");
  try {
    await api("/api/automation/emergency-stop", { method: "POST", body: JSON.stringify({}) });
    setResult("automation-result", "全局紧急停止已开启。", "danger");
    await Promise.all([loadAutomationStatus(), loadNotifications(), loadAuditLogs()]);
  } catch (error) {
    setResult("automation-result", error.message, "danger");
  }
}

async function clearEmergencyStop() {
  setResult("automation-result", "正在解除紧急停止...");
  try {
    await api("/api/automation/clear-emergency-stop", { method: "POST", body: JSON.stringify({}) });
    setResult("automation-result", "全局紧急停止已解除。", "success");
    await Promise.all([loadAutomationStatus(), loadAuditLogs()]);
  } catch (error) {
    setResult("automation-result", error.message, "danger");
  }
}

async function closeAllPaperPositions() {
  setResult("automation-result", "正在平掉 Paper 持仓...");
  try {
    const data = await api("/api/automation/close-all", {
      method: "POST",
      body: JSON.stringify({ run_mode: "paper", price_map: {}, reason: "console_close_all" }),
    });
    setResult("automation-result", `已平掉 ${formatNumber(data.closed_count)} 个 Paper 持仓。`, "success");
    await Promise.all([loadAutomationStatus(), loadPaperTrading(), loadMonitoring(), loadOrderHistory(), loadExecutionEvents(), loadNotifications()]);
  } catch (error) {
    setResult("automation-result", error.message, "danger");
  }
}

function renderAutomationStatus() {
  const status = state.automationStatus || { emergency_stop: { value: "0" }, controls: [] };
  const emergencyValue = typeof status.emergency_stop === "string" ? status.emergency_stop : status.emergency_stop?.value;
  const rows = (status.controls || [])
    .map(
      (control) => `
        <tr>
          <td>${control.id}</td>
          <td>${escapeHtml(control.owner_email || "")}</td>
          <td>${escapeHtml(control.run_mode)}</td>
          <td>${escapeHtml(control.strategy_name || "全部策略")}</td>
          <td><span class="chip ${control.status === "running" ? "success" : control.status === "paused" ? "warning" : "muted"}">${escapeHtml(control.status)}</span></td>
          <td>${control.trading_enabled ? "yes" : "no"}</td>
          <td>${escapeHtml(control.last_action || "")}</td>
          <td>${formatDate(control.updated_at)}</td>
        </tr>`
    )
    .join("");
  document.getElementById("automation-result").classList.toggle("danger", emergencyValue === "1");
  if (!document.getElementById("automation-result").textContent) {
    setResult("automation-result", emergencyValue === "1" ? "全局紧急停止开启中。" : "自动交易控制台待命。", emergencyValue === "1" ? "danger" : "success");
  }
  document.getElementById("automation-status-table").innerHTML = `
    <table>
      <thead><tr><th>ID</th><th>用户</th><th>模式</th><th>策略</th><th>状态</th><th>交易开关</th><th>动作</th><th>更新时间</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="8">暂无自动控制记录</td></tr>'}</tbody>
    </table>
  `;
}

async function loadNotifications() {
  if (
    !(
      hasPermission(PERMISSIONS.EXECUTE_PAPER_TRADES) ||
      hasPermission(PERMISSIONS.MANAGE_USERS) ||
      hasPermission(PERMISSIONS.VIEW_ALL_TRADES)
    )
  ) {
    return;
  }
  const allQuery = hasPermission(PERMISSIONS.MANAGE_USERS) || hasPermission(PERMISSIONS.VIEW_ALL_TRADES) ? "?all=1&limit=80" : "?limit=80";
  state.notifications = await api(`/api/notifications${allQuery}`);
  if (hasPermission(PERMISSIONS.EXECUTE_PAPER_TRADES) || hasPermission(PERMISSIONS.MANAGE_USERS)) {
    const channelQuery = hasPermission(PERMISSIONS.MANAGE_USERS) ? "?all=1" : "";
    state.notificationChannels = await api(`/api/notification-channels${channelQuery}`);
  }
  renderNotifications();
}

async function createNotificationChannel(event) {
  event.preventDefault();
  try {
    await api("/api/notification-channels", {
      method: "POST",
      body: JSON.stringify({
        channel_type: document.getElementById("notification-channel-type").value,
        label: document.getElementById("notification-label").value,
        destination: document.getElementById("notification-destination").value,
        is_active: document.getElementById("notification-active").checked,
      }),
    });
    document.getElementById("notification-channel-form").reset();
    document.getElementById("notification-active").checked = true;
    await loadNotifications();
    toast("通知渠道已添加");
  } catch (error) {
    toast(error.message);
  }
}

function renderNotifications() {
  const channelRows = state.notificationChannels
    .map(
      (channel) => `
        <tr>
          <td>${channel.id}</td>
          <td>${escapeHtml(channel.owner_email || "")}</td>
          <td>${escapeHtml(channel.channel_type)}</td>
          <td>${escapeHtml(channel.label)}</td>
          <td>${escapeHtml(channel.destination || "-")}</td>
          <td><span class="chip ${channel.is_active ? "success" : "muted"}">${channel.is_active ? "启用" : "停用"}</span></td>
        </tr>`
    )
    .join("");
  document.getElementById("notification-channels-table").innerHTML = `
    <table>
      <thead><tr><th>ID</th><th>用户</th><th>渠道</th><th>标签</th><th>目标</th><th>状态</th></tr></thead>
      <tbody>${channelRows || '<tr><td colspan="6">暂无通知渠道</td></tr>'}</tbody>
    </table>
  `;

  const rows = state.notifications
    .map(
      (item) => `
        <tr>
          <td>${item.id}</td>
          <td>${formatDate(item.created_at)}</td>
          <td>${escapeHtml(item.owner_email || "")}</td>
          <td>${escapeHtml(item.channel_type)}</td>
          <td>${escapeHtml(item.event_type)}</td>
          <td>${escapeHtml(item.title)}</td>
          <td><span class="chip ${item.status === "stored" ? "success" : "warning"}">${escapeHtml(item.status)}</span></td>
          <td>${escapeHtml(item.message)}</td>
        </tr>`
    )
    .join("");
  document.getElementById("notifications-table").innerHTML = `
    <table>
      <thead><tr><th>ID</th><th>时间</th><th>用户</th><th>渠道</th><th>事件</th><th>标题</th><th>状态</th><th>内容</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="8">暂无通知记录</td></tr>'}</tbody>
    </table>
  `;
}

async function loadRiskSettings() {
  if (!hasPermission(PERMISSIONS.EXECUTE_PAPER_TRADES)) return;
  state.riskSettings = await api("/api/risk-settings");
  renderRiskSettings();
}

function renderRiskSettings() {
  const settings = state.riskSettings;
  if (!settings) return;
  document.getElementById("paper-trading-enabled").checked = settings.paper_trading_enabled;
  document.getElementById("live-trading-enabled").checked = settings.live_trading_enabled;
  document.getElementById("risk-max-order-notional").value = settings.max_order_notional;
  document.getElementById("risk-max-daily-loss").value = settings.max_daily_loss;
  document.getElementById("risk-max-symbol-weight").value = settings.max_symbol_weight;
  document.getElementById("risk-max-gross-exposure").value = settings.max_gross_exposure;
  document.getElementById("risk-max-open-positions").value = settings.max_open_positions;
  document.getElementById("risk-max-leverage").value = settings.max_leverage;
  document.getElementById("risk-max-consecutive-losses").value = settings.max_consecutive_losses;
  document.getElementById("require-order-confirmation").checked = settings.require_order_confirmation;
  document.getElementById("allow-reverse-position").checked = settings.allow_reverse_position;
  document.getElementById("allow-duplicate-position").checked = settings.allow_duplicate_position;
  document.getElementById("auto-disable-on-daily-loss").checked = settings.auto_disable_on_daily_loss;
  document.getElementById("require-live-confirmation").checked = settings.require_live_confirmation;
  setResult(
    "risk-settings-result",
    `当前：Paper ${settings.paper_trading_enabled ? "开启" : "暂停"}，实盘 ${settings.live_trading_enabled ? "开启" : "暂停"}，单笔上限 ${formatMoney(settings.max_order_notional)}。`,
    settings.paper_trading_enabled ? "success" : "warning"
  );
}

async function saveRiskSettings() {
  if (!hasPermission(PERMISSIONS.EXECUTE_PAPER_TRADES)) return;
  setResult("risk-settings-result", "正在保存风控设置...");
  try {
    state.riskSettings = await api("/api/risk-settings", {
      method: "PATCH",
      body: JSON.stringify({
        paper_trading_enabled: document.getElementById("paper-trading-enabled").checked,
        live_trading_enabled: document.getElementById("live-trading-enabled").checked,
        max_order_notional: Number(document.getElementById("risk-max-order-notional").value),
        max_daily_loss: Number(document.getElementById("risk-max-daily-loss").value),
        max_symbol_weight: Number(document.getElementById("risk-max-symbol-weight").value),
        max_gross_exposure: Number(document.getElementById("risk-max-gross-exposure").value),
        max_open_positions: Number(document.getElementById("risk-max-open-positions").value),
        max_leverage: Number(document.getElementById("risk-max-leverage").value),
        max_consecutive_losses: Number(document.getElementById("risk-max-consecutive-losses").value),
        require_order_confirmation: document.getElementById("require-order-confirmation").checked,
        allow_reverse_position: document.getElementById("allow-reverse-position").checked,
        allow_duplicate_position: document.getElementById("allow-duplicate-position").checked,
        auto_disable_on_daily_loss: document.getElementById("auto-disable-on-daily-loss").checked,
        require_live_confirmation: document.getElementById("require-live-confirmation").checked,
      }),
    });
    renderRiskSettings();
    await loadAuditLogs();
    toast("风控设置已保存");
  } catch (error) {
    setResult("risk-settings-result", error.message, "danger");
  }
}

async function loadDataSources() {
  if (!(hasPermission(PERMISSIONS.VIEW_STRATEGIES) || hasPermission(PERMISSIONS.RUN_BACKTESTS))) return;
  state.dataSources = await api("/api/data-sources/catalog");
  renderDataSources();
}

function selectedDataSource() {
  const provider = document.getElementById("data-provider").value;
  return state.dataSources.find((source) => source.id === provider) || state.dataSources[0];
}

function renderDataSources() {
  const select = document.getElementById("data-provider");
  select.innerHTML = state.dataSources
    .map((source) => `<option value="${escapeHtml(source.id)}">${escapeHtml(source.name)}</option>`)
    .join("");
  document.getElementById("data-source-cards").innerHTML = state.dataSources
    .map(
      (source) => `
        <article class="strategy-card">
          <div>
            <h3>${escapeHtml(source.name)}</h3>
            <p>${escapeHtml(source.description)}</p>
          </div>
          <div class="card-meta">
            <span class="chip muted">${escapeHtml(source.symbol_hint)}</span>
            <span class="chip muted">max ${formatNumber(source.max_limit)}</span>
            ${source.requires_api_key ? '<span class="chip warning">需要 API Key</span>' : '<span class="chip success">公开</span>'}
          </div>
          <p>${escapeHtml(source.notes || "OHLCV K 线数据源。")}</p>
          <button class="secondary" data-select-data-source="${escapeHtml(source.id)}" type="button">选择数据源</button>
        </article>`
    )
    .join("");
  updateDataSourceFields({ resetSymbol: true });
}

function updateDataSourceFields({ resetSymbol = false } = {}) {
  const source = selectedDataSource();
  if (!source) return;
  const symbolInput = document.getElementById("data-symbol");
  const intervalSelect = document.getElementById("data-interval");
  const limitInput = document.getElementById("data-limit");

  if (resetSymbol || !symbolInput.value) {
    symbolInput.value = source.symbol_hint;
  }
  intervalSelect.innerHTML = source.intervals.map((interval) => `<option value="${escapeHtml(interval)}">${escapeHtml(interval)}</option>`).join("");
  if (!source.intervals.includes(intervalSelect.value)) {
    intervalSelect.value = source.intervals[0];
  }
  limitInput.max = source.max_limit;
  if (Number(limitInput.value) > Number(source.max_limit)) {
    limitInput.value = source.max_limit;
  }
  const outputInput = document.getElementById("data-output-file");
  if (!outputInput.value || resetSymbol) {
    outputInput.value = defaultDataOutputFile(source.id, symbolInput.value, intervalSelect.value);
  }
}

async function fetchMarketData() {
  if (!hasPermission(PERMISSIONS.RUN_BACKTESTS)) return;
  setResult("data-source-result", "正在拉取行情数据...");
  try {
    const data = await api("/api/data-sources/fetch", {
      method: "POST",
      body: JSON.stringify({
        provider: document.getElementById("data-provider").value,
        symbol: document.getElementById("data-symbol").value,
        interval: document.getElementById("data-interval").value,
        start: document.getElementById("data-start").value || null,
        end: document.getElementById("data-end").value || null,
        limit: Number(document.getElementById("data-limit").value),
        output_file: document.getElementById("data-output-file").value || null,
      }),
    });
    document.getElementById("bars-file").value = data.output_file;
    document.getElementById("indicator-bars-file").value = data.output_file;
    document.getElementById("indicator-symbol").value = data.symbol;
    setResult(
      "data-source-result",
      `已保存 ${formatNumber(data.bar_count)} 根 K 线到 ${data.output_file}。首根 ${formatDate(data.first_bar.timestamp)}，末根 ${formatDate(data.last_bar.timestamp)}。`,
      "success"
    );
    toast("行情 CSV 已保存并填入回测");
  } catch (error) {
    setResult("data-source-result", error.message, "danger");
  }
}

async function loadIndicatorCatalog() {
  if (!(hasPermission(PERMISSIONS.VIEW_STRATEGIES) || hasPermission(PERMISSIONS.RUN_BACKTESTS))) return;
  state.indicatorCatalog = await api("/api/analysis/indicator-catalog");
  renderIndicatorCatalog();
}

function renderIndicatorCatalog() {
  const box = document.getElementById("indicator-catalog");
  if (!box) return;
  box.innerHTML = state.indicatorCatalog
    .map((item) => `<span class="chip muted" title="${escapeHtml(item.description)}">${escapeHtml(item.name)}</span>`)
    .join("");
}

async function runIndicatorAnalysis() {
  if (!hasPermission(PERMISSIONS.RUN_BACKTESTS)) return;
  setResult("indicator-result", "正在计算指标...");
  try {
    const data = await api("/api/analysis/indicators", {
      method: "POST",
      body: JSON.stringify({
        bars_file: document.getElementById("indicator-bars-file").value || document.getElementById("bars-file").value,
        symbol: document.getElementById("indicator-symbol").value || null,
        fast_window: inputNumber("indicator-fast-window", 12),
        slow_window: inputNumber("indicator-slow-window", 26),
        rsi_window: inputNumber("indicator-rsi-window", 14),
        bollinger_window: inputNumber("indicator-bollinger-window", 20),
        atr_window: inputNumber("indicator-atr-window", 14),
        macd_signal_window: inputNumber("indicator-macd-signal-window", 9),
        limit: 80,
      }),
    });
    renderIndicatorResult(data);
  } catch (error) {
    setResult("indicator-result", error.message, "danger");
  }
}

function renderIndicatorResult(data) {
  const result = data.results?.[0];
  if (!result) {
    setResult("indicator-result", "没有可用指标结果。", "warning");
    return;
  }
  const latest = result.latest;
  const rows = (result.rows || [])
    .slice(-30)
    .map(
      (row) => `
        <tr>
          <td>${formatDate(row.timestamp)}</td>
          <td>${Number(row.close).toFixed(4)}</td>
          <td>${formatNullable(row.sma_fast)}</td>
          <td>${formatNullable(row.sma_slow)}</td>
          <td>${formatNullable(row.rsi)}</td>
          <td>${formatNullable(row.atr)}</td>
          <td>${formatNullable(row.macd_histogram)}</td>
        </tr>`
    )
    .join("");

  document.getElementById("indicator-result").classList.remove("danger", "warning");
  document.getElementById("indicator-result").innerHTML = `
    <div class="result-meta">${escapeHtml(result.symbol)} · ${formatNumber(result.bar_count)} 根 K 线 · ${formatDate(result.first_timestamp)} 至 ${formatDate(result.last_timestamp)}</div>
    <div class="metric-grid">
      <div class="metric"><span class="label">收盘价</span><strong>${Number(latest.close).toFixed(4)}</strong></div>
      <div class="metric"><span class="label">SMA 快/慢</span><strong>${formatNullable(latest.sma_fast)} / ${formatNullable(latest.sma_slow)}</strong></div>
      <div class="metric"><span class="label">RSI</span><strong>${formatNullable(latest.rsi)}</strong></div>
      <div class="metric"><span class="label">Bollinger</span><strong>${formatNullable(latest.bollinger_lower)} - ${formatNullable(latest.bollinger_upper)}</strong></div>
      <div class="metric"><span class="label">ATR</span><strong>${formatNullable(latest.atr)}</strong></div>
      <div class="metric"><span class="label">MACD Hist</span><strong>${formatNullable(latest.macd_histogram)}</strong></div>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>时间</th><th>收盘</th><th>SMA 快</th><th>SMA 慢</th><th>RSI</th><th>ATR</th><th>MACD Hist</th></tr></thead>
        <tbody>${rows || '<tr><td colspan="7">暂无指标序列</td></tr>'}</tbody>
      </table>
    </div>
  `;
}

function formatNullable(value) {
  return value === null || value === undefined ? "-" : Number(value).toFixed(4);
}

async function loadWatchlists() {
  if (
    !(
      hasPermission(PERMISSIONS.VIEW_STRATEGIES) ||
      hasPermission(PERMISSIONS.RUN_BACKTESTS) ||
      hasPermission(PERMISSIONS.MANAGE_STRATEGIES) ||
      hasPermission(PERMISSIONS.MANAGE_ALL_STRATEGIES)
    )
  ) {
    return;
  }
  const query = hasPermission(PERMISSIONS.MANAGE_ALL_STRATEGIES) ? "?all=1" : "";
  state.watchlists = await api(`/api/watchlists${query}`);
  renderWatchlists();
}

function parseSymbols(value) {
  return String(value || "")
    .split(/[\s,，]+/)
    .map((item) => item.trim().toUpperCase())
    .filter(Boolean);
}

async function saveWatchlist() {
  if (!hasPermission(PERMISSIONS.MANAGE_STRATEGIES)) return;
  setResult("watchlist-result", "正在保存交易对池...");
  try {
    await api("/api/watchlists", {
      method: "POST",
      body: JSON.stringify({
        name: document.getElementById("watchlist-name").value,
        symbols: parseSymbols(document.getElementById("watchlist-symbols").value),
        notes: document.getElementById("watchlist-notes").value,
        is_default: document.getElementById("watchlist-is-default").checked,
      }),
    });
    document.getElementById("watchlist-is-default").checked = false;
    setResult("watchlist-result", "交易对池已保存。", "success");
    await loadWatchlists();
  } catch (error) {
    setResult("watchlist-result", error.message, "danger");
  }
}

function renderWatchlists() {
  const canManage = hasPermission(PERMISSIONS.MANAGE_STRATEGIES) || hasPermission(PERMISSIONS.MANAGE_ALL_STRATEGIES);
  const rows = state.watchlists
    .map(
      (item) => `
        <article class="strategy-card">
          <div>
            <h3>${escapeHtml(item.name)}${item.is_default ? " *" : ""}</h3>
            <p>${escapeHtml(item.notes || "用户保存的交易对池。")}</p>
          </div>
          <div class="chip-list">${item.symbols.map((symbol) => `<span class="chip muted">${escapeHtml(symbol)}</span>`).join("")}</div>
          <div class="card-meta">
            <span class="chip muted">${escapeHtml(item.owner_email)}</span>
            <span class="chip muted">${formatNumber(item.symbols.length)} symbols</span>
          </div>
          <div class="button-row">
            <button class="secondary" data-apply-watchlist="${item.id}" type="button">使用第一个交易对</button>
            ${canManage ? `<button class="secondary" data-default-watchlist="${item.id}" type="button">默认</button>` : ""}
            ${canManage ? `<button class="danger" data-delete-watchlist="${item.id}" type="button">删除</button>` : ""}
          </div>
        </article>`
    )
    .join("");
  document.getElementById("watchlist-cards").innerHTML = rows || '<div class="result-box compact-result">暂无交易对池</div>';
}

function applyWatchlist(id) {
  const watchlist = state.watchlists.find((item) => String(item.id) === String(id));
  if (!watchlist || !watchlist.symbols.length) return;
  const symbol = watchlist.symbols[0];
  document.getElementById("data-symbol").value = symbol;
  document.getElementById("paper-symbol").value = symbol;
  document.getElementById("indicator-symbol").value = symbol;
  const source = selectedDataSource();
  if (source) {
    document.getElementById("data-output-file").value = defaultDataOutputFile(source.id, symbol, document.getElementById("data-interval").value);
  }
  toast("已填入交易对");
}

async function setDefaultWatchlist(id) {
  await api(`/api/watchlists/${id}`, {
    method: "PATCH",
    body: JSON.stringify({ is_default: true }),
  });
  await loadWatchlists();
  toast("默认交易对池已更新");
}

async function deleteWatchlist(id) {
  await api(`/api/watchlists/${id}`, { method: "DELETE" });
  await loadWatchlists();
  toast("交易对池已删除");
}

function renderPaperAccount(account, positions) {
  const grossExposure = positions.reduce((sum, position) => sum + Math.abs(Number(position.quantity) * Number(position.average_price)), 0);
  const realizedPnl = positions.reduce((sum, position) => sum + Number(position.realized_pnl), 0);
  document.getElementById("paper-account-summary").innerHTML = `
    <div class="metric"><span class="label">Paper 现金</span><strong>${formatMoney(account.cash)}</strong></div>
    <div class="metric"><span class="label">持仓数量</span><strong>${formatNumber(positions.length)}</strong></div>
    <div class="metric"><span class="label">成本敞口</span><strong>${formatMoney(grossExposure)}</strong></div>
    <div class="metric"><span class="label">已实现 PnL</span><strong>${formatMoney(realizedPnl)}</strong></div>
  `;
}

function renderPaperPositions(positions) {
  const rows = positions
    .map(
      (position) => `
        <tr>
          <td>${escapeHtml(position.owner_email)}</td>
          <td>${escapeHtml(position.symbol)}</td>
          <td>${Number(position.quantity).toFixed(8)}</td>
          <td>${Number(position.average_price).toFixed(4)}</td>
          <td>${formatMoney(Number(position.quantity) * Number(position.average_price))}</td>
          <td>${formatMoney(position.realized_pnl)}</td>
          <td>${formatDate(position.updated_at)}</td>
        </tr>`
    )
    .join("");

  document.getElementById("paper-positions-table").innerHTML = `
    <table>
      <thead><tr><th>用户</th><th>币种</th><th>数量</th><th>均价</th><th>成本敞口</th><th>已实现 PnL</th><th>更新时间</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="7">暂无 paper 持仓</td></tr>'}</tbody>
    </table>
  `;
}

function collectPaperOrderPayload() {
  const quantityValue = document.getElementById("paper-quantity").value;
  const optionalNumber = (id) => {
    const value = document.getElementById(id).value;
    return value === "" ? null : Number(value);
  };
  return {
    action: document.getElementById("paper-action").value,
    symbol: document.getElementById("paper-symbol").value,
    quantity: quantityValue ? Number(quantityValue) : null,
    price: Number(document.getElementById("paper-price").value),
    order_type: document.getElementById("paper-order-type").value,
    time_in_force: document.getElementById("paper-time-in-force").value,
    limit_price: optionalNumber("paper-limit-price"),
    stop_price: optionalNumber("paper-stop-price"),
    trailing_delta_pct: optionalNumber("paper-trailing-delta"),
    post_only: document.getElementById("paper-post-only").checked,
    reduce_only: document.getElementById("paper-reduce-only").checked,
    leverage: Number(document.getElementById("paper-leverage").value),
    maintenance_margin_ratio: Number(document.getElementById("paper-maintenance-margin-ratio").value),
    funding_bps_per_8h: Number(document.getElementById("paper-funding-bps").value),
    confirm_order: document.getElementById("paper-confirm-order").checked,
    reason: document.getElementById("paper-reason").value,
    market_type: "spot",
  };
}

async function preflightPaperOrder() {
  const result = document.getElementById("paper-order-result");
  result.textContent = "预检中...";
  try {
    const data = await api("/api/trading/preflight", {
      method: "POST",
      body: JSON.stringify({ ...collectPaperOrderPayload(), run_mode: "paper" }),
    });
    renderPreflightResult(data);
    await loadAuditLogs();
  } catch (error) {
    result.textContent = error.message;
  }
}

function renderPreflightResult(data) {
  const rows = (data.checks || [])
    .map(
      (check) => `
        <tr>
          <td><span class="chip ${check.passed ? "success" : "danger"}">${check.passed ? "通过" : "拒绝"}</span></td>
          <td>${escapeHtml(check.name)}</td>
          <td>${escapeHtml(check.detail || "")}</td>
        </tr>`
    )
    .join("");

  document.getElementById("paper-order-result").innerHTML = `
    <div class="result-meta">${data.approved ? "预检通过" : "预检拒绝"}：${escapeHtml(data.reason)} · ${escapeHtml(data.symbol)} ${escapeHtml(data.side)} ${escapeHtml(data.order_type || "market")} ${Number(data.quantity).toFixed(8)}，预计名义额 ${formatMoney(data.estimated_notional)}</div>
    <div class="metric-grid compact-metrics">
      <div class="metric"><span class="label">预计成交价</span><strong>${formatMoney(data.estimated_fill_price)}</strong></div>
      <div class="metric"><span class="label">手续费</span><strong>${formatMoney(data.estimated_fee)}</strong></div>
      <div class="metric"><span class="label">滑点成本</span><strong>${formatMoney(data.estimated_slippage_cost || 0)}</strong></div>
      <div class="metric"><span class="label">初始保证金</span><strong>${formatMoney(data.initial_margin_required || 0)}</strong></div>
      <div class="metric"><span class="label">维持保证金</span><strong>${formatMoney(data.maintenance_margin_required || 0)}</strong></div>
      <div class="metric"><span class="label">8h 资金费</span><strong>${formatMoney(data.estimated_funding_8h || 0)}</strong></div>
      <div class="metric"><span class="label">预估强平价</span><strong>${data.estimated_liquidation_price ? formatMoney(data.estimated_liquidation_price) : "-"}</strong></div>
      <div class="metric"><span class="label">风险上限</span><strong>${data.will_trigger_risk_limit ? "触发" : "未触发"}</strong></div>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>状态</th><th>检查项</th><th>说明</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
  `;
}

async function submitPaperOrder() {
  const result = document.getElementById("paper-order-result");
  result.textContent = "提交中...";
  try {
    const data = await api("/api/paper-trading/order", {
      method: "POST",
      body: JSON.stringify(collectPaperOrderPayload()),
    });
    if (data.trade) {
      result.innerHTML = `
        <div class="result-meta">Paper 订单已成交：${escapeHtml(data.trade.symbol)} ${escapeHtml(data.trade.side)} ${Number(data.trade.quantity).toFixed(8)} @ ${Number(data.trade.price).toFixed(4)}</div>
        <div class="result-meta">订单 #${data.order.id} · 状态 ${escapeHtml(data.order.status)} · Exchange ID ${escapeHtml(data.order.exchange_order_id || "-")}</div>
      `;
    } else {
      result.innerHTML = `
        <div class="result-meta">订单 #${data.order.id} · 状态 ${escapeHtml(data.order.status)} · ${escapeHtml(data.order.error_message || data.order.reason || "")}</div>
      `;
    }
    await Promise.all([loadPaperTrading(), loadMonitoring(), loadOrderHistory(), loadExecutionEvents(), loadNotifications(), loadAuditLogs()]);
  } catch (error) {
    result.textContent = error.message;
  }
}

async function autoPaperExecute() {
  const result = document.getElementById("paper-order-result");
  result.textContent = "策略执行中...";
  const strategyConfigId = document.getElementById("backtest-strategy-config").value;
  const payload = {
    bars_file: document.getElementById("bars-file").value,
    events_file: document.getElementById("events-file").value,
  };
  if (strategyConfigId) {
    payload.strategy_config_id = Number(strategyConfigId);
  } else {
    payload.strategy_type = document.getElementById("strategy-type").value || "statistical_event";
    payload.strategy_parameters = collectStrategyParameters();
  }

  try {
    const data = await api("/api/paper-trading/auto-execute", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    result.innerHTML = `
      <div class="result-meta">策略 paper 执行完成：成交 ${formatNumber(data.trade_count)} 笔，现金 ${formatMoney(data.account.cash)}</div>
    `;
    await Promise.all([loadPaperTrading(), loadMonitoring(), loadOrderHistory(), loadExecutionEvents(), loadNotifications(), loadAuditLogs()]);
  } catch (error) {
    result.textContent = error.message;
  }
}

function renderMonitoringSummary(summary) {
  const latest = summary.latest_run;
  document.getElementById("monitoring-summary").innerHTML = `
    <div class="metric"><span class="label">运行次数</span><strong>${formatNumber(summary.total_runs)}</strong></div>
    <div class="metric"><span class="label">交易笔数</span><strong>${formatNumber(summary.total_trades)}</strong></div>
    <div class="metric"><span class="label">成交名义额</span><strong>${formatMoney(summary.total_notional)}</strong></div>
    <div class="metric"><span class="label">手续费</span><strong>${formatMoney(summary.total_fees)}</strong></div>
    <div class="metric"><span class="label">策略配置</span><strong>${formatNumber(summary.strategy_configs)}</strong></div>
    <div class="metric"><span class="label">API Key</span><strong>${formatNumber(summary.api_keys)}</strong></div>
    <div class="metric"><span class="label">异常运行</span><strong>${formatNumber(summary.non_completed_runs)}</strong></div>
    <div class="metric"><span class="label">最近运行</span><strong>${escapeHtml(latest?.status || "none")}</strong></div>
  `;
}

function renderTradeRuns(runs) {
  const rows = runs
    .map((run) => {
      const metrics = run.metrics || {};
      return `
        <tr>
          <td>${run.id}</td>
          <td>${escapeHtml(run.owner_email)}</td>
          <td>${escapeHtml(run.run_mode)}</td>
          <td>${escapeHtml(run.strategy_name || run.strategy_type)}</td>
          <td>${escapeHtml(run.status)}</td>
          <td>${formatPercent(metrics.total_return || 0)}</td>
          <td>${formatMoney(metrics.ending_equity || 0)}</td>
          <td>${formatDate(run.finished_at)}</td>
        </tr>`;
    })
    .join("");

  document.getElementById("trade-runs-table").innerHTML = `
    <table>
      <thead><tr><th>ID</th><th>用户</th><th>模式</th><th>策略</th><th>状态</th><th>收益</th><th>权益</th><th>完成时间</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="8">暂无运行记录</td></tr>'}</tbody>
    </table>
  `;
}

function renderTradeRecords(trades) {
  const rows = trades
    .map(
      (trade) => `
        <tr>
          <td>${trade.id}</td>
          <td>${escapeHtml(trade.owner_email)}</td>
          <td>${escapeHtml(trade.run_mode)}</td>
          <td>${escapeHtml(trade.strategy_name || "")}</td>
          <td>${escapeHtml(trade.symbol)}</td>
          <td>${escapeHtml(trade.side)}</td>
          <td>${Number(trade.quantity).toFixed(8)}</td>
          <td>${Number(trade.price).toFixed(4)}</td>
          <td>${formatMoney(trade.notional)}</td>
          <td>${formatMoney(trade.fee)}</td>
          <td>${formatDate(trade.traded_at)}</td>
        </tr>`
    )
    .join("");

  document.getElementById("trade-records-table").innerHTML = `
    <table>
      <thead><tr><th>ID</th><th>用户</th><th>模式</th><th>策略</th><th>币种</th><th>方向</th><th>数量</th><th>价格</th><th>名义额</th><th>手续费</th><th>时间</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="11">暂无交易记录</td></tr>'}</tbody>
    </table>
  `;
}

function renderOrderHistory(orders) {
  const rows = orders
    .map((order) => {
      const statusClass = {
        filled: "success",
        preflight_rejected: "danger",
        rejected: "danger",
        canceled: "warning",
        expired: "warning",
        partially_filled: "warning",
        submitted: "muted",
        created: "muted",
      }[order.status] || "muted";
      return `
        <tr>
          <td>${order.id}</td>
          <td>${escapeHtml(order.owner_email)}</td>
          <td>${escapeHtml(order.run_mode)}</td>
          <td>${escapeHtml(order.exchange)}</td>
          <td>${escapeHtml(order.symbol)}</td>
          <td>${escapeHtml(order.side)}</td>
          <td>${escapeHtml(order.order_type)}</td>
          <td>${escapeHtml(order.time_in_force || "")}</td>
          <td>${order.post_only ? "yes" : "no"} / ${order.reduce_only ? "yes" : "no"}</td>
          <td>${Number(order.quantity).toFixed(8)}</td>
          <td>${order.limit_price ? Number(order.limit_price).toFixed(4) : "-"}</td>
          <td>${order.stop_price ? Number(order.stop_price).toFixed(4) : "-"}</td>
          <td>${order.leverage ? Number(order.leverage).toFixed(2) : "1.00"}</td>
          <td>${Number(order.filled_quantity || 0).toFixed(8)}</td>
          <td>${order.average_fill_price ? Number(order.average_fill_price).toFixed(4) : "-"}</td>
          <td><span class="chip ${statusClass}">${escapeHtml(order.status)}</span></td>
          <td>${escapeHtml(order.error_message || order.reason || "")}</td>
          <td>${formatDate(order.updated_at)}</td>
        </tr>`;
    })
    .join("");

  document.getElementById("orders-table").innerHTML = `
    <table>
      <thead><tr><th>ID</th><th>用户</th><th>模式</th><th>交易所</th><th>币种</th><th>方向</th><th>类型</th><th>TIF</th><th>Post/Reduce</th><th>委托数量</th><th>限价</th><th>触发价</th><th>杠杆</th><th>成交数量</th><th>均价</th><th>状态</th><th>说明</th><th>更新时间</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="18">暂无订单</td></tr>'}</tbody>
    </table>
  `;
}

async function loadStrategyCatalog() {
  if (
    !(
      hasPermission(PERMISSIONS.VIEW_STRATEGIES) ||
      hasPermission(PERMISSIONS.RUN_BACKTESTS) ||
      hasPermission(PERMISSIONS.MANAGE_STRATEGIES) ||
      hasPermission(PERMISSIONS.MANAGE_ALL_STRATEGIES)
    )
  ) {
    return;
  }
  state.strategyCatalog = await api("/api/strategies/catalog");
  const select = document.getElementById("strategy-type");
  select.innerHTML = state.strategyCatalog
    .map((definition) => `<option value="${escapeHtml(definition.id)}">${escapeHtml(definition.name)}</option>`)
    .join("");
  renderStrategyCatalogCards();
  renderStrategyParameterForm();
}

function renderStrategyCatalogCards() {
  document.getElementById("strategy-catalog-cards").innerHTML = state.strategyCatalog
    .map(
      (definition) => `
        <article class="strategy-card">
          <div>
            <h3>${escapeHtml(definition.name)}</h3>
            <p>${escapeHtml(definition.description)}</p>
          </div>
          <div class="card-meta">
            ${definition.market_types.map((marketType) => `<span class="chip muted">${escapeHtml(marketType)}</span>`).join("")}
          </div>
          <p>${escapeHtml(definition.public_basis || "内置策略模板。")}</p>
          <button class="secondary" data-select-strategy="${escapeHtml(definition.id)}" type="button">选择策略</button>
        </article>`
    )
    .join("");
}

function selectedStrategyDefinition() {
  const strategyType = document.getElementById("strategy-type").value;
  return state.strategyCatalog.find((definition) => definition.id === strategyType) || state.strategyCatalog[0];
}

function renderStrategyParameterForm(parameters = null) {
  const definition = selectedStrategyDefinition();
  if (!definition) return;
  document.getElementById("strategy-description").textContent = definition.description;
  document.getElementById("market-type").innerHTML = definition.market_types
    .map((marketType) => `<option value="${escapeHtml(marketType)}">${escapeHtml(marketType)}</option>`)
    .join("");

  const values = parameters || Object.fromEntries(definition.parameters.map((parameter) => [parameter.key, parameter.default]));
  document.getElementById("strategy-param-grid").innerHTML = definition.parameters
    .map((parameter) => {
      const value = values[parameter.key] ?? parameter.default;
      const type = parameter.type === "int" || parameter.type === "float" ? "number" : "text";
      const min = parameter.min === null || parameter.min === undefined ? "" : ` min="${parameter.min}"`;
      const max = parameter.max === null || parameter.max === undefined ? "" : ` max="${parameter.max}"`;
      const step = parameter.step === null || parameter.step === undefined ? "" : ` step="${parameter.step}"`;
      return `
        <label>${escapeHtml(parameter.label)}
          <input data-strategy-param="${escapeHtml(parameter.key)}" data-param-type="${escapeHtml(parameter.type)}"
            type="${type}" value="${escapeHtml(value)}"${min}${max}${step} />
        </label>`;
    })
    .join("");
}

function collectStrategyParameters() {
  const values = {};
  document.querySelectorAll("[data-strategy-param]").forEach((input) => {
    const key = input.dataset.strategyParam;
    const type = input.dataset.paramType;
    if (type === "int") {
      values[key] = Number.parseInt(input.value, 10);
    } else if (type === "float") {
      values[key] = Number.parseFloat(input.value);
    } else {
      values[key] = input.value;
    }
  });
  return values;
}

async function saveStrategyConfig() {
  if (!hasPermission(PERMISSIONS.MANAGE_STRATEGIES)) return;
  try {
    await api("/api/strategy-configs", {
      method: "POST",
      body: JSON.stringify({
        name: document.getElementById("strategy-config-name").value,
        strategy_type: document.getElementById("strategy-type").value,
        market_type: document.getElementById("market-type").value,
        parameters: collectStrategyParameters(),
        is_default: document.getElementById("strategy-is-default").checked,
      }),
    });
    document.getElementById("strategy-is-default").checked = false;
    await loadStrategyConfigs();
    toast("策略配置已保存");
  } catch (error) {
    toast(error.message);
  }
}

async function loadStrategyConfigs() {
  if (
    !(
      hasPermission(PERMISSIONS.VIEW_STRATEGIES) ||
      hasPermission(PERMISSIONS.MANAGE_STRATEGIES) ||
      hasPermission(PERMISSIONS.MANAGE_ALL_STRATEGIES)
    )
  ) {
    return;
  }
  const query = hasPermission(PERMISSIONS.MANAGE_ALL_STRATEGIES) ? "?all=1" : "";
  state.strategyConfigs = await api(`/api/strategy-configs${query}`);
  renderStrategyConfigs();
  renderBacktestStrategySelect();
}

function renderStrategyConfigs() {
  const rows = state.strategyConfigs
    .map((config) => {
      const definition = state.strategyCatalog.find((item) => item.id === config.strategy_type);
      const paramText = Object.entries(config.parameters)
        .map(([key, value]) => `${key}=${value}`)
        .join(", ");
      return `
        <tr>
          <td>${config.id}</td>
          <td>${escapeHtml(config.owner_email)}</td>
          <td>${escapeHtml(config.name)}${config.is_default ? " *" : ""}</td>
          <td>${escapeHtml(definition?.name || config.strategy_type)}</td>
          <td>${escapeHtml(config.market_type)}</td>
          <td>${escapeHtml(paramText)}</td>
          <td>
            <button class="secondary" data-load-strategy="${config.id}" type="button">载入</button>
            <button class="secondary" data-default-strategy="${config.id}" type="button">默认</button>
            <button class="danger" data-delete-strategy="${config.id}" type="button">删除</button>
          </td>
        </tr>`;
    })
    .join("");

  document.getElementById("strategy-configs-table").innerHTML = `
    <table>
      <thead><tr><th>ID</th><th>用户</th><th>名称</th><th>模板</th><th>市场</th><th>参数</th><th></th></tr></thead>
      <tbody>${rows || '<tr><td colspan="7">暂无策略配置</td></tr>'}</tbody>
    </table>
  `;
}

function renderBacktestStrategySelect() {
  const options = ['<option value="">当前表单参数</option>'].concat(
    state.strategyConfigs.map(
      (config) =>
        `<option value="${config.id}" ${config.is_default ? "selected" : ""}>${escapeHtml(config.name)} (${escapeHtml(config.strategy_type)})</option>`
    )
  );
  ["backtest-strategy-config", "signal-strategy-config", "automation-strategy-config"].forEach((id) => {
    const select = document.getElementById(id);
    if (select) select.innerHTML = options.join("");
  });
}

async function loadBacktestHistory() {
  if (!(hasPermission(PERMISSIONS.VIEW_TRADES) || hasPermission(PERMISSIONS.VIEW_ALL_TRADES))) return;
  state.backtestRuns = await api(`/api/trade-runs${monitoringQuery(50, { run_mode: "backtest" })}`);
  renderBacktestHistory();
}

function renderBacktestHistory() {
  const rows = state.backtestRuns
    .map((run) => {
      const metrics = run.metrics || {};
      const statusClass = run.status === "completed" ? "success" : "warning";
      return `
        <tr>
          <td>${run.id}</td>
          <td>${escapeHtml(run.owner_email)}</td>
          <td>${escapeHtml(run.strategy_name || run.strategy_type)}</td>
          <td><span class="chip ${statusClass}">${escapeHtml(run.status)}</span></td>
          <td>${formatMoney(metrics.starting_equity || 0)}</td>
          <td>${formatMoney(metrics.ending_equity || 0)}</td>
          <td>${formatPercent(metrics.total_return || 0)}</td>
          <td>${formatPercent(metrics.max_drawdown || 0)}</td>
          <td>${Number(metrics.sharpe || 0).toFixed(2)}</td>
          <td>${formatMoney(metrics.total_funding || 0)}</td>
          <td>${formatNumber(metrics.liquidation_count || 0)} / ${formatNumber(metrics.forced_exit_count || 0)}</td>
          <td>${formatNumber(metrics.trade_count || 0)}</td>
          <td>${formatDate(run.finished_at)}</td>
          <td><button class="secondary" data-view-backtest="${run.id}" type="button">查看报告</button></td>
        </tr>`;
    })
    .join("");

  document.getElementById("backtest-history-table").innerHTML = `
    <table>
      <thead><tr><th>ID</th><th>用户</th><th>策略</th><th>状态</th><th>初始资金</th><th>结束权益</th><th>收益</th><th>最大回撤</th><th>Sharpe</th><th>资金费</th><th>爆仓/强平</th><th>成交</th><th>完成时间</th><th></th></tr></thead>
      <tbody>${rows || '<tr><td colspan="14">暂无回测结果</td></tr>'}</tbody>
    </table>
  `;
}

async function viewBacktestReport(runId) {
  const box = document.getElementById("backtest-report-detail");
  box.hidden = false;
  box.classList.remove("success", "warning", "danger");
  box.textContent = "正在加载回测报告...";
  try {
    const [run, records] = await Promise.all([
      api(`/api/trade-runs/${runId}`),
      api(`/api/trade-runs/${runId}/records`),
    ]);
    renderBacktestReport(run, records);
  } catch (error) {
    box.textContent = error.message;
    box.classList.add("danger");
  }
}

function renderBacktestReport(run, records) {
  const metrics = run.metrics || {};
  const recordRows = records
    .map(
      (record) => `
        <tr>
          <td>${record.id}</td>
          <td>${escapeHtml(record.symbol)}</td>
          <td>${escapeHtml(record.side)}</td>
          <td>${Number(record.quantity).toFixed(8)}</td>
          <td>${Number(record.price).toFixed(4)}</td>
          <td>${formatMoney(record.notional)}</td>
          <td>${formatMoney(record.fee)}</td>
          <td>${escapeHtml(record.reason || "")}</td>
          <td>${formatDate(record.traded_at)}</td>
        </tr>`
    )
    .join("");

  document.getElementById("backtest-report-detail").innerHTML = `
    <div class="report-detail">
      <div>
        <p class="eyebrow">Backtest Report</p>
        <h3>#${run.id} ${escapeHtml(run.strategy_name || run.strategy_type)}</h3>
      </div>
      <div class="button-row">
        <a class="button-link" href="/api/trade-runs/${run.id}/export?format=json" target="_blank" rel="noreferrer">导出 JSON</a>
        <a class="button-link secondary-link" href="/api/trade-runs/${run.id}/export?format=csv" target="_blank" rel="noreferrer">导出成交 CSV</a>
      </div>
      <div class="metric-grid">
        <div class="metric"><span class="label">结束权益</span><strong>${formatMoney(metrics.ending_equity || 0)}</strong></div>
        <div class="metric"><span class="label">收益率</span><strong>${formatPercent(metrics.total_return || 0)}</strong></div>
        <div class="metric"><span class="label">最大回撤</span><strong>${formatPercent(metrics.max_drawdown || 0)}</strong></div>
        <div class="metric"><span class="label">Sharpe</span><strong>${Number(metrics.sharpe || 0).toFixed(2)}</strong></div>
        <div class="metric"><span class="label">手续费</span><strong>${formatMoney(metrics.total_fees || 0)}</strong></div>
        <div class="metric"><span class="label">资金费</span><strong>${formatMoney(metrics.total_funding || 0)}</strong></div>
        <div class="metric"><span class="label">爆仓 / 强平</span><strong>${formatNumber(metrics.liquidation_count || 0)} / ${formatNumber(metrics.forced_exit_count || 0)}</strong></div>
      </div>
      ${renderValidationBlock(metrics.validation)}
      <pre>${escapeHtml(JSON.stringify({ parameters: run.parameters, bars_file: run.bars_file, events_file: run.events_file, validation: metrics.validation || null }, null, 2))}</pre>
      <div class="table-wrap">
        <table>
          <thead><tr><th>ID</th><th>币种</th><th>方向</th><th>数量</th><th>价格</th><th>名义额</th><th>手续费</th><th>原因</th><th>时间</th></tr></thead>
          <tbody>${recordRows || '<tr><td colspan="9">无成交记录</td></tr>'}</tbody>
        </table>
      </div>
    </div>
  `;
}

function loadStrategyConfigIntoForm(id) {
  const config = state.strategyConfigs.find((item) => String(item.id) === String(id));
  if (!config) return;
  document.getElementById("strategy-config-name").value = config.name;
  document.getElementById("strategy-type").value = config.strategy_type;
  renderStrategyParameterForm(config.parameters);
  document.getElementById("market-type").value = config.market_type;
  document.getElementById("strategy-is-default").checked = config.is_default;
  document.getElementById("backtest-strategy-config").value = String(config.id);
}

async function setDefaultStrategyConfig(id) {
  await api(`/api/strategy-configs/${id}`, {
    method: "PATCH",
    body: JSON.stringify({ is_default: true }),
  });
  await loadStrategyConfigs();
  toast("默认策略已更新");
}

async function deleteStrategyConfig(id) {
  await api(`/api/strategy-configs/${id}`, { method: "DELETE" });
  await loadStrategyConfigs();
  toast("策略配置已删除");
}

async function createApiKey(event) {
  event.preventDefault();
  try {
    await api("/api/api-keys", {
      method: "POST",
      body: JSON.stringify({
        exchange: document.getElementById("exchange").value,
        label: document.getElementById("key-label").value,
        api_key: document.getElementById("api-key").value,
        api_secret: document.getElementById("api-secret").value,
        passphrase: document.getElementById("passphrase").value,
        scopes: document
          .getElementById("scopes")
          .value.split(",")
          .map((item) => item.trim())
          .filter(Boolean),
        can_trade: document.getElementById("can-trade").checked,
      }),
    });
    document.getElementById("api-key-form").reset();
    await Promise.all([loadApiKeys(), loadAuditLogs()]);
    toast("API 已保存");
  } catch (error) {
    toast(error.message);
  }
}

async function loadApiKeys() {
  if (!(hasPermission(PERMISSIONS.MANAGE_API_KEYS) || hasPermission(PERMISSIONS.MANAGE_ALL_API_KEYS))) return;
  const query = hasPermission(PERMISSIONS.MANAGE_ALL_API_KEYS) ? "?all=1" : "";
  const keys = await api(`/api/api-keys${query}`);
  const rows = keys
    .map(
      (key) => `
        <tr>
          <td>${key.id}</td>
          <td>${key.owner_email}</td>
          <td>${key.exchange}</td>
          <td>${key.label}</td>
          <td>${key.api_key_masked}</td>
          <td>${key.scopes.join(", ")}</td>
          <td>${key.can_trade ? "yes" : "no"}</td>
          <td><button class="danger" data-delete-key="${key.id}" type="button">删除</button></td>
        </tr>`
    )
    .join("");

  document.getElementById("api-keys-table").innerHTML = `
    <table>
      <thead><tr><th>ID</th><th>用户</th><th>交易所</th><th>标签</th><th>Key</th><th>Scopes</th><th>Trade</th><th></th></tr></thead>
      <tbody>${rows || '<tr><td colspan="8">暂无 API</td></tr>'}</tbody>
    </table>
  `;
}

async function deleteApiKey(id) {
  await api(`/api/api-keys/${id}`, { method: "DELETE" });
  await Promise.all([loadApiKeys(), loadAuditLogs()]);
  toast("API 已删除");
}

async function loadAuditLogs() {
  if (!(hasPermission(PERMISSIONS.MANAGE_USERS) || hasPermission(PERMISSIONS.MANAGE_SYSTEM))) return;
  state.auditLogs = await api("/api/audit-logs?all=1&limit=120");
  renderAuditLogs();
}

function renderAuditLogs() {
  const rows = state.auditLogs
    .map(
      (item) => `
        <tr>
          <td>${item.id}</td>
          <td>${formatDate(item.created_at)}</td>
          <td>${escapeHtml(item.owner_email || "-")}</td>
          <td>${escapeHtml(item.action)}</td>
          <td><span class="chip ${item.status === "ok" || item.status === "filled" || item.status === "approved" ? "success" : "warning"}">${escapeHtml(item.status)}</span></td>
          <td>${escapeHtml(item.entity_type || "")}${item.entity_id ? ` #${escapeHtml(item.entity_id)}` : ""}</td>
          <td>${escapeHtml(item.ip_address || "")}</td>
          <td><code>${escapeHtml(JSON.stringify(item.detail || {}))}</code></td>
        </tr>`
    )
    .join("");

  document.getElementById("audit-logs-table").innerHTML = `
    <table>
      <thead><tr><th>ID</th><th>时间</th><th>用户</th><th>动作</th><th>状态</th><th>对象</th><th>IP</th><th>详情</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="8">暂无审计日志</td></tr>'}</tbody>
    </table>
  `;
}

async function loadExchangeSyncRuns() {
  if (!(hasPermission(PERMISSIONS.EXECUTE_PAPER_TRADES) || hasPermission(PERMISSIONS.VIEW_ALL_TRADES))) return;
  const query = hasPermission(PERMISSIONS.VIEW_ALL_TRADES) ? "?all=1&limit=30" : "?limit=30";
  state.exchangeSyncRuns = await api(`/api/exchange-sync/runs${query}`);
  renderExchangeSyncRuns();
}

async function runExchangeSync() {
  if (!hasPermission(PERMISSIONS.EXECUTE_PAPER_TRADES)) return;
  setResult("exchange-sync-result", "正在对账...");
  try {
    const data = await api("/api/exchange-sync/run", {
      method: "POST",
      body: JSON.stringify({ exchange: "paper", run_mode: "paper" }),
    });
    setResult(
      "exchange-sync-result",
      `对账完成：${data.status}，差异 ${formatNumber((data.differences || []).length)} 项。`,
      (data.differences || []).length ? "warning" : "success"
    );
    await Promise.all([loadExchangeSyncRuns(), loadAuditLogs()]);
  } catch (error) {
    setResult("exchange-sync-result", error.message, "danger");
  }
}

function renderExchangeSyncRuns() {
  const rows = state.exchangeSyncRuns
    .map(
      (run) => `
        <tr>
          <td>${run.id}</td>
          <td>${formatDate(run.created_at)}</td>
          <td>${escapeHtml(run.owner_email)}</td>
          <td>${escapeHtml(run.exchange)}</td>
          <td>${escapeHtml(run.run_mode)}</td>
          <td><span class="chip ${(run.differences || []).length ? "warning" : "success"}">${escapeHtml(run.status)}</span></td>
          <td>${formatNumber((run.differences || []).length)}</td>
          <td><code>${escapeHtml(JSON.stringify((run.differences || []).slice(0, 3)))}</code></td>
        </tr>`
    )
    .join("");

  document.getElementById("exchange-sync-table").innerHTML = `
    <table>
      <thead><tr><th>ID</th><th>时间</th><th>用户</th><th>交易所</th><th>模式</th><th>状态</th><th>差异</th><th>摘要</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="8">暂无对账记录</td></tr>'}</tbody>
    </table>
  `;
}

async function createUser(event) {
  event.preventDefault();
  try {
    await api("/api/users", {
      method: "POST",
      body: JSON.stringify({
        email: document.getElementById("new-user-email").value,
        password: document.getElementById("new-user-password").value,
        role: document.getElementById("new-user-role").value,
      }),
    });
    document.getElementById("create-user-form").reset();
    await Promise.all([loadUsers(), loadAuditLogs()]);
    toast("用户已创建");
  } catch (error) {
    toast(error.message);
  }
}

async function loadUsers() {
  if (!hasPermission(PERMISSIONS.MANAGE_USERS)) return;
  const users = await api("/api/users");
  const cards = users
    .map(
      (user) => {
        const permissions = userEffectivePermissions(user);
        const pages = visiblePagesForPermissions(permissions);
        return `
          <article class="user-card">
            <div class="user-card-head">
              <div>
                <h3>${escapeHtml(user.email)}</h3>
                <p class="result-meta">ID ${user.id} · 创建 ${formatDate(user.created_at)}</p>
              </div>
              <span class="chip ${user.status === "active" ? "success" : "warning"}">${escapeHtml(user.status)}</span>
            </div>
            <div class="form-grid compact-form-grid">
              <label>角色
                <select data-user-role="${user.id}">
                  ${["viewer", "analyst", "trader", "admin"]
                    .map((role) => `<option value="${role}" ${role === user.role ? "selected" : ""}>${role}</option>`)
                    .join("")}
                </select>
              </label>
              <label>状态
                <select data-user-status="${user.id}">
                  ${["active", "disabled"]
                    .map((status) => `<option value="${status}" ${status === user.status ? "selected" : ""}>${status}</option>`)
                    .join("")}
                </select>
              </label>
            </div>
            <div>
              <span class="label">可见页面</span>
              <div class="chip-list">${pages.map((page) => `<span class="chip muted">${page}</span>`).join("") || '<span class="chip warning">无页面</span>'}</div>
            </div>
            <details class="permission-details">
              <summary>权限覆盖</summary>
              <div class="permission-grid">
                ${renderUserPermissionGrid(user, permissions)}
              </div>
            </details>
          </article>`;
      }
    )
    .join("");

  document.getElementById("users-table").innerHTML = cards || '<div class="result-box">暂无用户</div>';
}

function renderUserPermissionGrid(user, permissions) {
  const roleDefaults = roleDefaultPermissions(user.role);
  const permissionList = state.permissionCatalog.length ? state.permissionCatalog : Object.keys(PERMISSION_LABELS);
  return permissionList
    .map((permission) => {
      const checked = permissions.has(permission);
      const inherited = roleDefaults.has(permission);
      const revoked = (user.permission_revokes || []).includes(permission);
      const granted = (user.permission_grants || []).includes(permission);
      const source = revoked ? "已撤销默认" : granted ? "额外授权" : inherited ? "角色默认" : "未授权";
      return `
        <label class="permission-row ${checked ? "enabled" : ""}">
          <input data-user-permission="${user.id}" data-permission="${escapeHtml(permission)}" type="checkbox" ${checked ? "checked" : ""} />
          <span>
            <strong>${escapeHtml(PERMISSION_LABELS[permission] || permission)}</strong>
            <small>${escapeHtml(source)} · ${escapeHtml(permission)}</small>
          </span>
        </label>`;
    })
    .join("");
}

async function updateUser(id, patch) {
  await api(`/api/users/${id}`, {
    method: "PATCH",
    body: JSON.stringify(patch),
  });
  if (state.user && String(state.user.id) === String(id)) {
    applySessionPayload(await api("/api/me"));
    renderShell();
  }
  await Promise.all([loadUsers(), loadAuditLogs()]);
  toast("用户已更新");
}

async function updateUserPermission(id, permission, checked) {
  const user = await api(`/api/users`).then((users) => users.find((item) => String(item.id) === String(id)));
  if (!user) {
    toast("用户不存在");
    return;
  }
  const defaults = roleDefaultPermissions(user.role);
  const grants = new Set(user.permission_grants || []);
  const revokes = new Set(user.permission_revokes || []);

  if (checked) {
    revokes.delete(permission);
    if (!defaults.has(permission)) grants.add(permission);
  } else {
    grants.delete(permission);
    if (defaults.has(permission)) revokes.add(permission);
  }

  await updateUser(id, {
    permission_grants: [...grants].sort(),
    permission_revokes: [...revokes].sort(),
  });
}

document.getElementById("login-form").addEventListener("submit", login);
document.getElementById("logout-button").addEventListener("click", logout);
document.getElementById("run-backtest").addEventListener("click", runBacktest);
document.getElementById("run-grid-search").addEventListener("click", () => runGridSearch().catch((error) => toast(error.message)));
document.getElementById("refresh-monitoring").addEventListener("click", () => loadMonitoring().catch((error) => toast(error.message)));
document.getElementById("refresh-trades").addEventListener("click", () => loadMonitoring().catch((error) => toast(error.message)));
document.getElementById("refresh-orders").addEventListener("click", () => loadOrderHistory().catch((error) => toast(error.message)));
document.getElementById("refresh-paper").addEventListener("click", () => loadPaperTrading().catch((error) => toast(error.message)));
document.getElementById("refresh-market-snapshot").addEventListener("click", () => refreshMarketSnapshot().catch((error) => toast(error.message)));
document.getElementById("evaluate-signal").addEventListener("click", () => evaluateRealtimeSignal().catch((error) => toast(error.message)));
document.getElementById("preflight-paper-order").addEventListener("click", () => preflightPaperOrder().catch((error) => toast(error.message)));
document.getElementById("submit-paper-order").addEventListener("click", () => submitPaperOrder().catch((error) => toast(error.message)));
document.getElementById("auto-paper-execute").addEventListener("click", () => autoPaperExecute().catch((error) => toast(error.message)));
document.getElementById("strategy-type").addEventListener("change", () => renderStrategyParameterForm());
document.getElementById("save-strategy-config").addEventListener("click", saveStrategyConfig);
document.getElementById("api-key-form").addEventListener("submit", createApiKey);
document.getElementById("create-user-form").addEventListener("submit", createUser);
document.getElementById("change-password-form").addEventListener("submit", changePassword);
document.getElementById("save-risk-settings").addEventListener("click", () => saveRiskSettings().catch((error) => toast(error.message)));
document.getElementById("fetch-market-data").addEventListener("click", () => fetchMarketData().catch((error) => toast(error.message)));
document.getElementById("run-indicator-analysis").addEventListener("click", () => runIndicatorAnalysis().catch((error) => toast(error.message)));
document.getElementById("save-watchlist").addEventListener("click", () => saveWatchlist().catch((error) => toast(error.message)));
document.getElementById("refresh-audit-logs").addEventListener("click", () => loadAuditLogs().catch((error) => toast(error.message)));
document.getElementById("run-exchange-sync").addEventListener("click", () => runExchangeSync().catch((error) => toast(error.message)));
document.getElementById("refresh-execution-events").addEventListener("click", () => loadExecutionEvents().catch((error) => toast(error.message)));
document.getElementById("automation-start").addEventListener("click", () => automationControl("start"));
document.getElementById("automation-pause").addEventListener("click", () => automationControl("pause"));
document.getElementById("automation-stop").addEventListener("click", () => automationControl("stop"));
document.getElementById("emergency-stop").addEventListener("click", () => triggerEmergencyStop());
document.getElementById("clear-emergency-stop").addEventListener("click", () => clearEmergencyStop());
document.getElementById("close-all-paper").addEventListener("click", () => closeAllPaperPositions());
document.getElementById("refresh-notifications").addEventListener("click", () => loadNotifications().catch((error) => toast(error.message)));
document.getElementById("notification-channel-form").addEventListener("submit", createNotificationChannel);
document.getElementById("data-provider").addEventListener("change", () => updateDataSourceFields({ resetSymbol: true }));
document.getElementById("data-symbol").addEventListener("change", () => {
  const source = selectedDataSource();
  if (source) {
    document.getElementById("data-output-file").value = defaultDataOutputFile(
      source.id,
      document.getElementById("data-symbol").value,
      document.getElementById("data-interval").value
    );
  }
});
document.getElementById("data-interval").addEventListener("change", () => {
  const source = selectedDataSource();
  if (source) {
    document.getElementById("data-output-file").value = defaultDataOutputFile(
      source.id,
      document.getElementById("data-symbol").value,
      document.getElementById("data-interval").value
    );
  }
});

document.querySelectorAll("[data-page-tab]").forEach((button) => {
  button.addEventListener("click", () => setActivePage(button.dataset.pageTab));
});

window.addEventListener("hashchange", () => {
  if (state.user) setActivePage(window.location.hash.replace("#", "") || "status");
});

document.body.addEventListener("click", (event) => {
  const keyId = event.target.dataset?.deleteKey;
  const loadStrategyId = event.target.dataset?.loadStrategy;
  const selectStrategyId = event.target.dataset?.selectStrategy;
  const selectDataSourceId = event.target.dataset?.selectDataSource;
  const applyWatchlistId = event.target.dataset?.applyWatchlist;
  const defaultWatchlistId = event.target.dataset?.defaultWatchlist;
  const deleteWatchlistId = event.target.dataset?.deleteWatchlist;
  const viewBacktestId = event.target.dataset?.viewBacktest;
  const defaultStrategyId = event.target.dataset?.defaultStrategy;
  const deleteStrategyId = event.target.dataset?.deleteStrategy;
  if (keyId) deleteApiKey(keyId).catch((error) => toast(error.message));
  if (loadStrategyId) loadStrategyConfigIntoForm(loadStrategyId);
  if (selectStrategyId) {
    document.getElementById("strategy-type").value = selectStrategyId;
    renderStrategyParameterForm();
    toast("策略模板已选择");
  }
  if (selectDataSourceId) {
    document.getElementById("data-provider").value = selectDataSourceId;
    updateDataSourceFields({ resetSymbol: true });
    toast("数据源已选择");
  }
  if (applyWatchlistId) applyWatchlist(applyWatchlistId);
  if (defaultWatchlistId) setDefaultWatchlist(defaultWatchlistId).catch((error) => toast(error.message));
  if (deleteWatchlistId) deleteWatchlist(deleteWatchlistId).catch((error) => toast(error.message));
  if (viewBacktestId) viewBacktestReport(viewBacktestId).catch((error) => toast(error.message));
  if (defaultStrategyId) setDefaultStrategyConfig(defaultStrategyId).catch((error) => toast(error.message));
  if (deleteStrategyId) deleteStrategyConfig(deleteStrategyId).catch((error) => toast(error.message));
});

document.body.addEventListener("change", (event) => {
  const roleUserId = event.target.dataset?.userRole;
  const statusUserId = event.target.dataset?.userStatus;
  const permissionUserId = event.target.dataset?.userPermission;
  if (roleUserId) updateUser(roleUserId, { role: event.target.value }).catch((error) => toast(error.message));
  if (statusUserId) updateUser(statusUserId, { status: event.target.value }).catch((error) => toast(error.message));
  if (permissionUserId) {
    updateUserPermission(permissionUserId, event.target.dataset.permission, event.target.checked).catch((error) => {
      toast(error.message);
      loadUsers().catch((loadError) => toast(loadError.message));
    });
  }
});

loadSession();
