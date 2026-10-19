/* Research views are isolated from order entry and reset on every session change. */
const radarState = { data: null, selected: null, signals: [], timer: null, generation: 0, pending: false, editId: null, userId: null };
const radarRequests = new Set();
const radarNames = { market: "市值与供给", spot: "现货盘口", derivatives: "合约持仓", holders: "大户地址", transfers: "链上转账", governance: "权限与多签", news: "新闻", technicals: "日线与基准" };
const radarStatuses = { candidate: "观察候选", avoid: "风险复核", observe: "继续观察", incomplete: "数据不全", warming: "积累样本", paused: "已暂停", waiting: "等待采集" };
const radarBlocks = {
  asset_identity_unverified: "币种与合约身份未核验", market_cap_outside_range: "流通市值不在设定区间", insufficient_volume: "报告成交额不足",
  inconsistent_supply: "供给数据矛盾", already_extended: "短期涨幅过大", insufficient_executable_liquidity: "盘口深度或价差未达标",
  price_divergence: "不同来源价格偏差过大", holder_baseline_warming: "大户对比样本不足，至少积累 1 小时", governance_unverified: "控制权限未完成核验",
  token_paused: "代币已暂停", recent_authority_change_review: "近期权限变更待复核", news_requires_review: "新闻风险关键词待复核",
  insufficient_coverage: "数据覆盖不足", score_below_threshold: "机会分未达门槛", risk_above_threshold: "风险分超限",
  watch_configuration_changed: "币种配置已修改，等待重新采集核验",
  supply_unavailable: "流通或总供给数据缺失",
  factor_data_incomplete: "已启用因子的有效数据覆盖不足",
};
const radarFields = [
  ["interval_seconds", "更新间隔（秒）", 60, 86400, 1], ["min_market_cap", "最小流通市值（USD）", 0, 1e13, 1],
  ["max_market_cap", "最大流通市值（USD）", 1, 1e14, 1], ["min_volume", "最小报告成交额 / 24h（USD）", 0, 1e13, 1],
  ["min_depth_usdt", "最小双边深度 / 1%（USDT）", 0, 1e10, 1], ["max_spread_bps", "最大买卖价差（bps）", 1, 1000, 1],
  ["min_score", "机会分门槛", 40, 100, 1], ["max_risk", "风险分上限", 0, 60, 1], ["min_coverage", "最低覆盖率（%）", 70, 100, 1],
  ["whale_transfer_usd", "大额转账门槛（USD）", 1000, 1e10, 1], ["fee_bps", "前瞻观察单边手续费（bps）", 0, 100, 0.1],
  ["slippage_bps", "前瞻观察单边滑点（bps）", 0, 500, 0.1],
  ["min_factor_coverage", "最低因子覆盖率（%）", 70, 100, 1],
  ["max_volatility_pct", "年化波动率上限（%）", 10, 1000, 1],
  ["max_atr_pct", "ATR / 价格上限（%）", 0.1, 100, 0.1],
  ["max_amihud_bps", "Amihud 上限（bps/百万USDT）", 0.01, 100000, 0.01],
  ["max_basis_bps", "永续标记价偏离上限（bps）", 1, 5000, 1],
  ["max_oi_volume_ratio", "OI / 现货成交额上限（倍）", 0.1, 1000, 0.1],
];
const radarEl = (id) => document.getElementById(id);
const radarEsc = (value) => escapeHtml(String(value ?? ""));
const radarNum = (value, digits = 2) => value === null || value === undefined || !Number.isFinite(Number(value)) ? "—" : Number(value).toLocaleString("zh-CN", { maximumFractionDigits: digits });
const radarPct = (value) => value === null || value === undefined ? "—" : `${Number(value) > 0 ? "+" : ""}${radarNum(value)}%`;
const radarTime = (value) => value ? new Date(value * 1000).toLocaleString("zh-CN", { hour12: false }) : "尚无记录";
const radarBadge = (status) => `<span class="radar-badge ${radarEsc(status)}">${radarStatuses[status] || "未知"}</span>`;
const radarBlock = (code) => code.startsWith("source_unavailable:") ? `${radarNames[code.split(":")[1]] || "来源"}缺失或过期` : radarBlocks[code] || code;
function radarLink(url, label) {
  try {
    const parsed = new URL(url);
    if (!["https:", "http:"].includes(parsed.protocol)) return radarEsc(label);
    return `<a href="${radarEsc(parsed.href)}" target="_blank" rel="noopener noreferrer">${radarEsc(label)}</a>`;
  } catch { return radarEsc(label); }
}
function radarError(error) {
  return error.message === "not authenticated" ? "会话已过期，请重新登录" : error.message;
}
async function radarRead(path) {
  const controller = new AbortController();
  radarRequests.add(controller);
  const timeout = setTimeout(() => controller.abort(), 20000);
  try { return await api(path, {signal: controller.signal}); }
  catch (error) { if (error.name === "AbortError") throw new Error("读取超时或会话已结束"); throw error; }
  finally { clearTimeout(timeout); radarRequests.delete(controller); }
}
function resetRadar() {
  radarState.generation += 1;
  radarRequests.forEach(controller => controller.abort());
  radarRequests.clear();
  clearInterval(radarState.timer);
  Object.assign(radarState, { data: null, selected: null, signals: [], timer: null, pending: false, editId: null, userId: null });
  radarState.detailKey = null;
  ["radar-table", "radar-detail", "radar-summary", "radar-config-list", "radar-journal", "radar-signal-detail", "radar-settings-fields", "radar-labels"].forEach(id => { radarEl(id).replaceChildren(); });
  radarEl("radar-asset-form").reset();
  radarEl("radar-asset-form").hidden = true;
}
function radarSession() {
  if (radarState.userId !== state.user?.id) resetRadar();
  radarState.userId = state.user?.id;
  const read = hasPermission("radar:view"), write = read && hasPermission("radar:manage_self");
  radarEl("radar-section").hidden = !read;
  radarEl("radar-journal-section").hidden = !read;
  radarEl("radar-config-section").hidden = !write;
  radarEl("radar-refresh").disabled = !write;
  if (!read) { resetRadar(); return; }
  if (!radarState.timer) radarState.timer = setInterval(() => {
    if (document.hidden || !state.user || radarEl("app-view").hidden) return;
    if (state.activePage === "status") loadRadar();
    if (state.activePage === "trades") loadRadarSignals();
  }, 15000);
}
async function loadRadar() {
  if (!hasPermission("radar:view") || radarState.pending) return;
  const generation = radarState.generation;
  radarState.pending = true;
  try {
    const data = await radarRead("/api/radar");
    if (generation !== radarState.generation) return;
    radarState.data = data;
    radarEl("radar-error").textContent = "";
    renderRadar();
  } catch (error) {
    if (generation === radarState.generation) {
      radarEl("radar-error").textContent = `更新失败：${radarError(error)}。当前内容可能过期。`;
      radarEl("radar-worker").textContent = "连接中断，候选状态暂不可用";
      radarState.data = null;
      radarEl("radar-table").innerHTML = '<p class="radar-empty">无法取得最新状态</p>';
      radarEl("radar-detail").replaceChildren();
      radarEl("radar-summary").replaceChildren();
    }
  } finally { if (generation === radarState.generation) radarState.pending = false; }
}
function renderRadar() {
  const data = radarState.data;
  if (!data) return;
  const worker = data.worker;
  const running = worker.running && worker.heartbeat && data.server_time - worker.heartbeat < 240;
  radarEl("radar-worker").textContent = `${data.settings.enabled ? (running ? "后台采集中" : "后台未运行或无心跳") : "个人观察已暂停"} · ${data.settings.interval_seconds} 秒周期 · ${radarTime(worker.heartbeat)}`;
  const all = data.watches;
  radarEl("radar-summary").innerHTML = [["观察币种", all.length], ["观察候选", all.filter(w => w.snapshot?.evaluation.status === "candidate").length],
    ["风险复核", all.filter(w => w.snapshot?.evaluation.status === "avoid").length], ["平均覆盖率", `${radarNum(all.reduce((n,w) => n + (w.snapshot?.evaluation.coverage || 0), 0) / (all.length || 1), 0)}%`]]
    .map(([label,value]) => `<div class="radar-metric"><span>${label}</span><strong>${value}</strong></div>`).join("");
  const filter = radarEl("radar-filter").value, sort = radarEl("radar-sort").value;
  const watches = all.filter(w => filter === "all" || w.snapshot?.evaluation.status === filter).sort((a,b) => sort === "symbol" ? a.asset.symbol.localeCompare(b.asset.symbol) : (b.snapshot?.evaluation[sort === "risk" ? "risk_score" : "score"] || 0) - (a.snapshot?.evaluation[sort === "risk" ? "risk_score" : "score"] || 0));
  if (!watches.some(w => w.id === radarState.selected)) radarState.selected = watches[0]?.id ?? null;
  radarEl("radar-table").innerHTML = watches.length ? `<table><thead><tr><th>币种 / 链</th><th>状态</th><th>机会 / 风险</th><th>覆盖</th><th>现价（USDT）</th><th>流通市值（USD）</th><th>FDV（USD）</th><th>OI / 24h</th><th>采集完成</th></tr></thead><tbody>${watches.map(w => {
    const s = w.snapshot, e = s?.evaluation, m = s?.sources.market?.data || {}, spot = s?.sources.spot?.data || {}, d = s?.sources.derivatives?.data || {};
    return `<tr class="${w.id === radarState.selected ? "is-selected" : ""}"><td><button class="radar-select" data-radar-select="${w.id}" aria-pressed="${w.id === radarState.selected}">${radarEsc(w.asset.symbol)}</button><small>${radarEsc(w.asset.chain)}</small></td><td>${radarBadge(!w.enabled || !data.settings.enabled ? "paused" : e?.status || "waiting")}${w.updating ? "<small>更新中</small>" : ""}</td><td>${e ? `${e.score} / ${e.risk_score}` : "—"}</td><td>${e ? e.coverage + "%" : "—"}</td><td>${e?.fresh_sources.spot ? radarNum(spot.price,8) : "—"}</td><td>${e?.fresh_sources.market ? radarNum(m.market_cap,0) : "—"}</td><td>${e?.fresh_sources.market ? radarNum(m.fdv,0) : "—"}</td><td>${e?.fresh_sources.derivatives ? radarPct(d.oi_change_24h_pct) : "—"}</td><td>${radarTime(w.last_finished)}${w.last_error ? `<small>${radarEsc(w.last_error)}</small>` : ""}</td></tr>`;
  }).join("")}</tbody></table>` : '<p class="radar-empty">没有符合筛选条件的币种</p>';
  radarEl("radar-table").querySelectorAll("[data-radar-select]").forEach(button => button.onclick = () => { radarState.selected = Number(button.dataset.radarSelect); renderRadar(); });
  renderRadarDetail(all.find(w => w.id === radarState.selected));
}
function renderRadarDetail(watch) {
  const box = radarEl("radar-detail");
  if (!watch) { box.replaceChildren(); return; }
  if (!watch.snapshot) { box.innerHTML = '<p class="radar-empty">等待首次采集。尚无可用判断。</p>'; return; }
  const detailKey = JSON.stringify([watch.id, watch.snapshot.id, watch.snapshot.evaluation, watch.enabled]);
  // Ignore clock-only changes so polling does not close evidence/history or disturb keyboard focus.
  const stableKey = detailKey.replace(/"evaluated_at":\s*[\d.]+/, '"evaluated_at":0');
  if (radarState.detailKey === stableKey && box.children.length) return;
  radarState.detailKey = stableKey;
  const s = watch.snapshot, e = s.evaluation, sources = s.sources;
  const fresh = key => e.fresh_sources[key] ? sources[key]?.data || {} : {};
  const governance = sources.governance;
  const partialGovernance = governance?.status === "partial" && Date.now()/1000 - governance.observed_at < 7200;
  const m = fresh("market"), d = fresh("derivatives"), g = partialGovernance ? governance.data : fresh("governance"), h = fresh("holders"), t = fresh("transfers"), n = fresh("news");
  const list = (items, empty) => items.length ? `<ul>${items.map(x => `<li>${x}</li>`).join("")}</ul>` : `<p class="radar-note">${empty}</p>`;
  box.innerHTML = `<div class="radar-detail-head"><div><h3>${radarEsc(watch.asset.symbol)} · ${radarEsc(watch.asset.name)}</h3><p>${radarEsc(watch.asset.chain)} · ${radarEsc(watch.asset.contract)}</p></div>${radarBadge(e.status)}</div>
    <p class="radar-note">观察分不是上涨概率。仅研究，不自动下单。${!watch.enabled ? "此币种已暂停。" : ""}</p>
    <div class="radar-two"><section class="radar-evidence"><h3>价格与供给</h3>
      <p class="radar-note">总供给估值 $${radarNum(m.total_supply_value,0)} · 流通市值 $${radarNum(m.market_cap,0)} · FDV $${radarNum(m.fdv,0)}<br>流通比例 ${m.float_ratio == null ? "—" : radarNum(m.float_ratio*100) + "%"} · 24h ${radarPct(m.change_24h_pct)} · 7d ${radarPct(m.change_7d_pct)}</p>
      <canvas id="radar-price-chart" class="radar-chart" role="img" aria-label="最近三十根已收盘日 K 线收盘价"></canvas>
      <p class="radar-note">Binance 已收盘日线 · RSI14（简单均值）${radarNum(fresh("spot").rsi14)} · 30 日回撤 ${radarPct(fresh("spot").drawdown_30d_pct)}</p>
      <h3>合约持仓与资金费</h3><p class="radar-note">Binance USD-M 持仓名义额 ${radarNum(d.open_interest_usdt,0)} USDT · 24h 持仓变化 ${radarPct(d.oi_change_24h_pct)}<br>本期资金费 ${radarNum(d.funding_bps)} bps · 最近结算间隔 ${radarNum(d.observed_funding_interval_hours)} 小时 · 下次结算 ${radarTime(d.next_funding_at)}</p></section>
    <section class="radar-evidence"><h3>数据来源与时效</h3>${Object.entries(radarNames).map(([key,label]) => { const source=sources[key] || {}; return `<div class="radar-source"><strong>${label}</strong><span>${e.fresh_sources[key] ? "有效" : source.status === "error" ? `不可用 · ${radarEsc(source.error)}` : "缺失或过期"}<br>${radarTime(source.observed_at)}</span>${radarLink(source.url,"原始来源")}</div>`; }).join("")}<p class="radar-note">${radarEsc(e.rule_version)} · ${radarTime(e.evaluated_at)}</p></section></div>
    <div class="radar-two"><section class="radar-evidence"><h3>机会分 ${e.score} / 100</h3>${list(e.factors.map(f => `+${f.points} · ${radarEsc(f.label)}`),"暂无符合条件的正向因子")}</section>
    <section class="radar-evidence"><h3>风险分 ${e.risk_score} / 100</h3>${list(e.risks.map(r => `+${r.points} · ${radarEsc(r.label)}`),"已覆盖数据中未触发规则，不代表没有风险")}${list(e.blockers.map(b => radarEsc(radarBlock(b))),"已达到规则观察门槛，仍须人工复核")}</section></div>
    ${renderRadarFactors(e.factor_model, sources)}
    <div class="radar-two"><section class="radar-evidence"><h3>大户持仓与转账</h3><p class="radar-note">${e.holder_delta ? `共同 ${e.holder_delta.addresses} 个地址 · ${radarNum(e.holder_delta.window_seconds/3600,1)} 小时余额变化 ${radarPct(e.holder_delta.pct)}` : "尚无可比较的大户历史样本"}<br>前十地址占比 ${h.top10_share == null ? "—" : radarNum(h.top10_share*100) + "%"} · 样本 ${radarNum(h.sample_size,0)} 个地址</p>
      ${list((t.items || []).filter(x => Number(x.value_usd) >= radarState.data.settings.whale_transfer_usd && Date.now()/1000 - x.timestamp <= 86400).slice(0,8).map(x => `${radarLink(x.url,`${x.from_label || x.from.slice(0,8)} → ${x.to_label || x.to.slice(0,8)}`)} · 约 $${radarNum(x.value_usd,0)} · ${radarTime(x.timestamp)}`),"最近一页样本中暂无符合门槛的大额转账，或数据不可用")}
      <p class="radar-note">地址不等于独立投资者，余额增加或转入交易所不等于买入或卖出。转账仅最近一页，不是完整净流量。</p></section>
    <section class="radar-evidence"><h3>控制权限与近期多签</h3><p class="radar-note">${partialGovernance ? "权限快照已读取，但变更历史不完整，不参与候选判断。<br>" : ""}控制类型 ${radarEsc(g.owner_kind || "未核验")} · 核验区块 ${radarNum(g.block,0)} · Timelock ${g.min_delay_seconds == null ? "—" : radarNum(g.min_delay_seconds/86400) + " 天"}<br>Owner ${radarEsc(g.owner || "未读取到")}</p>
      ${list((g.safes || []).map(x => `${radarLink(x.url,x.address)} · ${x.threshold}/${x.owners.length} · ${radarEsc(x.role)}`),"当前未验证到标准 Safe 多签")}
      ${list((g.events || []).slice(0,8).map(x => `${radarLink(x.url,x.event)} · ${radarTime(x.timestamp)}`),"已覆盖事件中，近 90 天未发现权限变更；不可用来源不算无事件")}<p class="radar-note">仅已执行链上事件；多签不等于独立签名人，非完整安全审计。</p></section></div>
    <section class="radar-evidence"><h3>近期新闻</h3>${list((n.items || []).slice(0,10).map(x => `${radarLink(x.url,x.title)} · ${radarEsc(x.publisher)} · ${radarTime(x.published_at)}${x.risk_terms.length ? " · 关键词待复核" : ""}`),"新闻样本未匹配报道，或来源不可用；不代表没有事件")}
    <p class="radar-note">可用新闻源 ${n.feeds_available ?? 0}/${n.feeds_total ?? 3} · 仅标题关键词匹配，尚未覆盖全网、社交媒体或全部公告。</p></section>
    <details><summary>数据局限与观察历史</summary>${list([...e.limitations,...Object.values(sources).flatMap(x => x.warnings || [])].map(radarEsc),"")}<button id="radar-history" class="secondary" type="button">读取观察历史</button><div id="radar-history-list" class="table-wrap"></div></details>`;
  drawRadarChart(fresh("spot").daily_closes || []);
  radarEl("radar-history").onclick = async () => {
    const generation = radarState.generation, selected = watch.id;
    try {
      const result = await radarRead(`/api/radar/watches/${watch.id}/history`);
      if (generation !== radarState.generation || selected !== radarState.selected) return;
      radarEl("radar-history-list").innerHTML = `<table><thead><tr><th>时间</th><th>规则版本</th><th>状态</th><th>机会 / 风险</th><th>覆盖</th></tr></thead><tbody>${result.items.map(x => `<tr><td>${radarTime(x.created_at)}</td><td>${radarEsc(x.evaluation.rule_version)}</td><td>${radarBadge(x.evaluation.status)}</td><td>${x.evaluation.score} / ${x.evaluation.risk_score}</td><td>${x.evaluation.coverage}%</td></tr>`).join("")}</tbody></table>`;
    } catch(error) { toast(radarError(error)); }
  };
}
function renderRadarFactors(model, sources) {
  if (!model) return "";
  const groups = radarState.data.factor_groups || {};
  const checks = Object.fromEntries(model.groups.flatMap(group => group.checks.map(check => [check.key, check])));
  const selected = new Set(model.groups.map(group => group.id));
  const stateLabel = {missing: "缺失", passed: "达标", not_met: "未达标"};
  return `<section class="radar-evidence radar-factor-section"><h3>选币因子 · ${model.points} / ${model.max_points} 分</h3>
    <p class="radar-note">有效评分数据 ${model.coverage}% · 日线截至 ${radarTime(model.as_of)}<br>日线因子每日更新；每组上限 5 分，缺失值不补分。基础观察规则占 ${100-model.max_points} 分。规则未经过样本外收益验证。</p>
    <div class="radar-factor-groups">${Object.entries(groups).map(([key,label]) => { const group=model.groups.find(g => g.id===key); return `<span>${radarEsc(label)} <strong>${group ? group.points + " / 5" : "不评分"}</strong></span>`; }).join("")}</div>
    <div class="table-wrap"><table class="radar-factor-table"><thead><tr><th>因子</th><th>数值</th><th>评分条件</th><th>结果 / 分数</th><th>口径与来源</th></tr></thead><tbody>${model.metrics.map(metric => {
      const check=checks[metric.key], source=sources[metric.source] || {};
      return `<tr><td>${radarEsc(metric.label)}<small>${radarEsc(groups[metric.group])}</small></td><td>${radarNum(metric.value,3)} ${metric.value == null ? "" : radarEsc(metric.unit)}</td><td>${check ? radarEsc(check.condition) : "不单独加分"}</td><td>${check ? `${stateLabel[check.status]} · ${check.points}/${check.max_points}` : selected.has(metric.group) ? "背景指标" : "组已关闭"}</td><td>${radarEsc(metric.interpretation)}<small>${radarEsc(radarNames[metric.source])} · ${radarTime(source.observed_at)}${metric.key === "oi_to_spot_volume" ? `<br>现货成交额 · ${radarTime(sources.spot?.observed_at)}` : ""}</small></td></tr>`;
    }).join("")}</tbody></table></div>
    <details><summary>尚未接入的因子</summary><ul>${(radarState.data.deferred_factors || []).map(x => `<li>${radarEsc(x.name)}：${radarEsc(x.requirement)}。不参与评分。</li>`).join("")}</ul></details></section>`;
}
function drawRadarChart(points) {
  const canvas = radarEl("radar-price-chart");
  if (!canvas) return;
  const width = canvas.clientWidth || 300, height = 160, ratio = Math.min(devicePixelRatio || 1, 2);
  canvas.width = width*ratio; canvas.height = height*ratio;
  const ctx = canvas.getContext("2d"); ctx.scale(ratio,ratio); ctx.clearRect(0,0,width,height);
  ctx.font = "12px system-ui"; ctx.fillStyle = "#647067";
  if (points.length < 2) { ctx.fillText("暂无有效日线数据",12,80); return; }
  const prices=points.map(x => x.price), min=Math.min(...prices), max=Math.max(...prices), range=max-min || max*.01 || 1;
  ctx.strokeStyle = "#d9ded8"; ctx.lineWidth = 1;
  [25,75,125].forEach(y => { ctx.beginPath(); ctx.moveTo(0,y); ctx.lineTo(width,y); ctx.stroke(); });
  ctx.strokeStyle = "#286f96"; ctx.lineWidth = 2; ctx.beginPath();
  points.forEach((p,i) => { const x=4+i*(width-8)/(points.length-1), y=130-(p.price-min)/range*100; i ? ctx.lineTo(x,y) : ctx.moveTo(x,y); }); ctx.stroke();
  ctx.fillText(radarNum(max,6),4,14); ctx.fillText(radarNum(min,6),4,151);
}
async function loadRadarConfig() {
  if (!hasPermission("radar:view") || !hasPermission("radar:manage_self")) return;
  const generation = radarState.generation;
  const data = await radarRead("/api/radar");
  if (generation !== radarState.generation) return;
  radarState.data = data;
  radarEl("radar-settings-fields").innerHTML = `<label class="checkbox-row"><input name="enabled" type="checkbox" ${data.settings.enabled ? "checked" : ""} />后台自动观察</label><label class="checkbox-row"><input name="notifications_enabled" type="checkbox" ${data.settings.notifications_enabled ? "checked" : ""} />候选与风险通知</label>${radarFields.map(([name,label,min,max,step]) => `<label>${label}<input name="${name}" type="number" min="${min}" max="${max}" step="${step}" value="${data.settings[name]}" required /></label>`).join("")}`;
  radarEl("radar-settings-fields").insertAdjacentHTML("beforeend", `<fieldset class="radar-factor-options"><legend>参与评分的因子组</legend>${Object.entries(data.factor_groups).map(([key,label]) => `<label class="checkbox-row"><input name="factor_group" type="checkbox" value="${radarEsc(key)}" ${data.settings.enabled_factor_groups.includes(key) ? "checked" : ""} />${radarEsc(label)}</label>`).join("")}</fieldset>`);
  radarEl("radar-config-list").innerHTML = `<table><thead><tr><th>币种</th><th>链</th><th>状态</th><th>操作</th></tr></thead><tbody>${data.watches.map(w => `<tr><td>${radarEsc(w.asset.symbol)}</td><td>${radarEsc(w.asset.chain)}</td><td>${w.enabled ? "启用" : "暂停"}</td><td><div class="button-row"><button class="secondary" data-edit="${w.id}" type="button">编辑</button><button class="secondary" data-toggle="${w.id}" type="button">${w.enabled ? "暂停" : "启用"}</button><button class="danger" data-delete="${w.id}" type="button">删除</button></div></td></tr>`).join("")}</tbody></table>`;
  radarEl("radar-config-list").querySelectorAll("[data-edit]").forEach(b => b.onclick = () => editRadarAsset(data.watches.find(w => w.id === Number(b.dataset.edit))));
  radarEl("radar-config-list").querySelectorAll("[data-toggle]").forEach(b => b.onclick = () => radarAction(b, async () => {
    const watch = data.watches.find(w => w.id === Number(b.dataset.toggle));
    await api(`/api/radar/watches/${watch.id}`, { method: "PUT", body: JSON.stringify({...watch.asset,enabled:!watch.enabled}) }); await loadRadarConfig();
  }));
  radarEl("radar-config-list").querySelectorAll("[data-delete]").forEach(b => b.onclick = () => radarAction(b, async () => {
    if (!confirm("删除此观察项及其全部观察历史和信号记录？")) return;
    await api(`/api/radar/watches/${b.dataset.delete}`,{method:"DELETE"}); await loadRadarConfig();
  }));
}
function editRadarAsset(watch=null) {
  radarState.editId = watch?.id ?? null;
  const form=radarEl("radar-asset-form"); form.reset(); form.hidden=false;
  radarEl("radar-asset-title").textContent = watch ? `编辑 ${watch.asset.symbol}` : "添加币种";
  for (const name of ["symbol","name","coingecko_id","chain","contract","governor","exchange_symbol"]) {
    form.elements[name].value = watch?.asset[name] || (name === "chain" ? "ethereum" : "");
    form.elements[name].disabled = Boolean(watch && ["chain","contract"].includes(name));
  }
  form.elements.enabled.checked = watch?.enabled ?? true;
  radarEl("radar-labels").replaceChildren();
  (watch?.asset.wallet_labels || []).forEach(addRadarLabel);
  form.elements.symbol.focus();
}
function addRadarLabel(label={}) {
  if (radarEl("radar-labels").children.length >= 30) { toast("最多 30 个地址标签"); return; }
  const row=document.createElement("div"); row.className="radar-label-row";
  row.innerHTML=`<label>钱包地址<input data-label="address" value="${radarEsc(label.address || "")}" required pattern="0x[a-fA-F0-9]{40}" /></label><label>名称<input data-label="label" value="${radarEsc(label.label || "")}" maxlength="60" required /></label><label>类型<select data-label="kind">${[["unknown","未知"],["exchange","交易所"],["treasury","金库"],["bridge","跨链桥"],["burn","销毁"]].map(([value,name]) => `<option value="${value}" ${label.kind===value ? "selected" : ""}>${name}</option>`).join("")}</select></label><button type="button" class="secondary">移除</button>`;
  row.querySelector("button").onclick=() => row.remove(); radarEl("radar-labels").append(row);
}
async function radarAction(button, action) {
  button.disabled=true;
  try { await action(); } catch(error) { toast(radarError(error)); radarEl("radar-config-result").textContent=radarError(error); }
  finally { button.disabled=false; }
}
async function loadRadarSignals() {
  if (!hasPermission("radar:view")) return;
  const generation=radarState.generation;
  try {
    const data=await radarRead("/api/radar/signals");
    if (generation!==radarState.generation) return;
    radarState.signals=data.items;
    if (!data.items.some(x => x.id === radarState.signalId)) radarEl("radar-signal-detail").replaceChildren();
    const outcome=x => !x ? "待观察" : x.status === "missed" ? "缺少到期报价" : `${radarPct(x.net_return_pct)}<small>${radarTime(x.observed_at)}</small>`;
    radarEl("radar-journal").innerHTML=data.items.length ? `<table><thead><tr><th>时间</th><th>币种</th><th>信号</th><th>机会 / 风险</th><th>1h 净变化</th><th>24h 净变化</th><th>7d 净变化</th><th>证据</th></tr></thead><tbody>${data.items.map(x => `<tr><td>${radarTime(x.created_at)}</td><td>${radarEsc(x.evidence.asset.symbol)}</td><td>${radarBadge(x.status)}</td><td>${x.evidence.evaluation.score} / ${x.evidence.evaluation.risk_score}</td>${["1h","24h","7d"].map(k => `<td>${x.status === "candidate" ? outcome(x.outcomes[k]) : "不适用"}</td>`).join("")}<td><button class="secondary" data-signal="${x.id}">查看证据</button></td></tr>`).join("")}</tbody></table>` : '<p class="radar-empty">尚无候选或风险信号。未产生信号前不生成收益记录。</p>';
    radarEl("radar-journal").querySelectorAll("[data-signal]").forEach(b => b.onclick=() => {
      const signal=data.items.find(x => x.id === Number(b.dataset.signal));
      radarState.signalId = signal.id;
      radarEl("radar-signal-detail").innerHTML=`<details open><summary>${radarEsc(signal.evidence.asset.symbol)} · ${radarTime(signal.created_at)} · 冻结证据与原始参数</summary><pre class="radar-signal-json">${radarEsc(JSON.stringify({evidence:signal.evidence,outcomes:signal.outcomes},null,2))}</pre></details>`;
    });
  } catch(error) { if (generation===radarState.generation) radarEl("radar-journal").textContent=`信号读取失败：${radarError(error)}`; }
}
radarEl("radar-filter").onchange=renderRadar;
radarEl("radar-sort").onchange=renderRadar;
radarEl("radar-refresh").onclick=event => radarAction(event.currentTarget,async () => { const r=await api("/api/radar/refresh",{method:"POST"}); toast(r.message); await loadRadar(); });
radarEl("radar-signals-refresh").onclick=() => loadRadarSignals();
radarEl("radar-add").onclick=() => editRadarAsset();
radarEl("radar-add-label").onclick=() => addRadarLabel();
radarEl("radar-cancel-asset").onclick=() => { radarEl("radar-asset-form").hidden=true; radarState.editId=null; };
radarEl("radar-settings-form").onsubmit=event => {
  event.preventDefault();
  const form=event.currentTarget, config={enabled:form.elements.enabled.checked,notifications_enabled:form.elements.notifications_enabled.checked};
  radarFields.forEach(([name]) => { config[name]=Number(form.elements[name].value); });
  config.enabled_factor_groups = Array.from(form.querySelectorAll('input[name="factor_group"]:checked'), input => input.value);
  radarAction(form.querySelector("button[type=submit]"),async () => { await api("/api/radar/settings",{method:"PUT",body:JSON.stringify(config)}); radarEl("radar-config-result").textContent="配置已保存，后台按新参数更新。"; await loadRadarConfig(); });
};
radarEl("radar-asset-form").onsubmit=event => {
  event.preventDefault();
  const form=event.currentTarget, asset={};
  ["symbol","name","coingecko_id","chain","contract","governor","exchange_symbol"].forEach(name => { asset[name]=form.elements[name].value.trim(); });
  asset.governor=asset.governor || null; asset.enabled=form.elements.enabled.checked;
  asset.wallet_labels=Array.from(radarEl("radar-labels").children).map(row => Object.fromEntries(Array.from(row.querySelectorAll("[data-label]")).map(input => [input.dataset.label,input.value.trim()])));
  radarAction(form.querySelector("button[type=submit]"),async () => { await api(`/api/radar/watches${radarState.editId ? "/"+radarState.editId : ""}`,{method:radarState.editId ? "PUT" : "POST",body:JSON.stringify(asset)}); form.hidden=true; radarState.editId=null; radarEl("radar-config-result").textContent="币种已保存。新数据通过身份核验后才参与评分。"; await loadRadarConfig(); });
};
window.addEventListener("resize",() => {
  const watch=radarState.data?.watches.find(w => w.id === radarState.selected);
  if (watch?.snapshot?.evaluation.fresh_sources.spot) drawRadarChart(watch.snapshot.sources.spot.data.daily_closes || []);
});
