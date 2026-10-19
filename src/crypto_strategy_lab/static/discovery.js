const discoveryState = {data:null,pending:false,page:0,evidenceKey:null,historyCursor:null,historyRequest:0,detailRequest:0};
const discoveryLabels = {binance: "币安 USDT 永续", upbit: "Upbit 韩国现货", catalog: "CoinGecko 币种目录", mapping: "Upbit 交易对映射", caps: "CoinGecko 市值"};
const discoveryReasons = {listing_unavailable: "上市列表不可用或过期", market_cap_missing: "市值缺失或身份不符", market_cap_stale: "市值过期", outside_cap_range: "市值超出区间", insufficient_volume: "报告成交额不足", upbit_warning_or_unknown: "Upbit 警告或警告状态未知", stablecoin: "已知稳定币", ambiguous_identity: "重名映射冲突", unmapped_identity: "尚无可靠映射（含未识别倍数合约）"};
const discoveryFields = [["min_market_cap","最小流通市值（USD）",0,1e13,1], ["max_market_cap","最大流通市值（USD）",1,1e14,1], ["min_volume","最低报告成交额 / 24h（USD）",0,1e13,1], ["interval_seconds","扫描间隔（秒）",900,86400,1]];
const discoveryCautions = {PRICE_FLUCTUATIONS:"价格波动",TRADING_VOLUME_SOARING:"成交量激增",DEPOSIT_AMOUNT_SOARING:"充值量激增",GLOBAL_PRICE_DIFFERENCES:"跨市场价差",CONCENTRATION_OF_SMALL_ACCOUNTS:"小额账户集中"};
Object.assign(discoveryLabels,{binance_tickers:"币安永续成交额",upbit_tickers:"Upbit 现货成交额"});
Object.assign(discoveryReasons,{target_not_listed:"目标市场未上市或被报价范围排除",ticker_unavailable:"目标成交额缺失或过期",venue_volume_low:"目标市场成交额不足",book_not_checked:"等待轮转检查",book_stale:"盘口过期",insufficient_book_size:"盘口无法覆盖完整买入和卖出",spread_too_wide:"买卖价差超限",slippage_too_high:"单边成本超限",depth_too_low:"1% 双边深度不足",outside_listing_scope:"不在上市范围"});
discoveryFields.push(["order_notional","计划名义金额",0.00000001,1e12,"any"],["min_volume_multiple","24h 成交额 / 计划金额下限",1,1e7,"any"],["max_spread_bps","最大买卖价差（bps）",0.01,1000,"any"],["max_slippage_bps","单边最大成本 / 中间价（bps）",0.01,2000,"any"],["min_depth_multiple","双边 1% 深度 / 计划金额下限",1,1000,"any"]);
const discoveryCheckStates = {passed:"采样达标",failed:"未通过",pending:"待检查",stale:"盘口过期",unknown:"数据未知",unsupported:"市场不匹配"};
const discoveryFactorLabels = {momentum:"7 日动量",liquidity:"目标市场成交额",supply:"流通市值 / FDV"};
function discoveryScore(x) {
  const s=x.screen;
  if(s.score==null) return `<span class="radar-badge incomplete">未排名</span><small>${s.missing.length ? "缺少："+s.missing.map(k=>radarEsc(discoveryFactorLabels[k])).join("、") : "完整样本不足 5 个"}</small>`;
  return `<strong>#${s.rank} · ${radarNum(s.score,1)}</strong><details><summary>评分明细</summary>${Object.entries(s.components).map(([k,v])=>`<small>${radarEsc(discoveryFactorLabels[k])} ${radarNum(v,1)}</small>`).join("")}</details>`;
}
function discoveryLiquidity(x) {
  const l=x.liquidity,badge=l.status==="passed" ? "candidate" : l.status==="failed" ? "avoid" : "incomplete",digits=l.quote==="BTC" ? 8 : 0;
  return `<span class="radar-badge ${badge}">${radarEsc(discoveryCheckStates[l.status])}</span><small>${radarEsc(x.target?.symbol || "")} · ${radarNum(l.notional,digits)} ${radarEsc(l.quote)}</small>${l.reasons.map(r=>`<small>${radarEsc(discoveryReasons[r] || r)}</small>`).join("")}${l.spread_bps!=null ? `<details><summary>盘口成本</summary><small>价差 ${radarNum(l.spread_bps,1)} bps</small><small>买入 / 卖出 ${radarNum(l.buy_slippage_bps,1)} / ${radarNum(l.sell_slippage_bps,1)} bps</small><small>1% 买盘 / 卖盘 ${radarNum(l.bid_depth_1pct,digits)} / ${radarNum(l.ask_depth_1pct,digits)} ${radarEsc(l.quote)}</small><small>未含手续费、资金费及延迟</small></details>` : ""}<small>${radarTime(l.observed_at)}</small>`;
}
function resetDiscovery() {
  Object.assign(discoveryState,{data:null,pending:false,page:0,evidenceKey:null});
  for (const id of ["discovery-table","discovery-summary","discovery-evidence","discovery-settings-fields","discovery-error","discovery-config-result","discovery-page"]) radarEl(id).replaceChildren();
  radarEl("discovery-search").value="";
  discoveryState.historyRequest++; discoveryState.detailRequest++;
  discoveryState.historyCursor=null;
  for(const id of ["discovery-history-select","discovery-history-detail","discovery-history-status","discovery-method"]) radarEl(id).replaceChildren();
  radarEl("discovery-filter").value="all";
  radarEl("discovery-history-more").hidden=true;
}
async function loadDiscovery() {
  if (!hasPermission("radar:view") || discoveryState.pending) return;
  const generation=radarState.generation;
  discoveryState.pending=true;
  try {
    const data=await radarRead("/api/radar/discovery");
    if(generation!==radarState.generation) return;
    discoveryState.data=data;
    radarEl("discovery-error").textContent="";
    renderDiscovery();
  } catch(error) {
    if(generation!==radarState.generation) return;
    discoveryState.data=null;
    radarEl("discovery-error").textContent=`扫描读取失败：${radarError(error)}。旧结果暂不可用。`;
    radarEl("discovery-table").replaceChildren();
    radarEl("discovery-summary").replaceChildren();
    radarEl("discovery-status").textContent="连接中断";
  } finally { if(generation===radarState.generation) discoveryState.pending=false; }
}
function renderDiscovery() {
  const d=discoveryState.data;
  if(!d) return;
  const s=d.settings, st=d.state;
  const progress=!s.enabled ? "个人自动扫描已暂停" : !d.worker_running ? "后台未运行" : st.updating ? "后台扫描中" : st.last_error ? "上轮扫描失败" : "自动扫描已启用";
  const caps=d.sources.caps;
  radarEl("discovery-error").textContent=caps && caps.status!=="ok" ? `市值来源不完整：已返回 ${caps.returned ?? 0} / ${caps.requested ?? 0} 个 ID。缺失数据不参与筛选。${(caps.errors || []).some(x=>/429|cooldown/.test(x)) ? "供应商限流，按冷却时间重试。" : ""}` : !d.scope_complete && d.collected_at ? "上市列表缺失或过期，当前范围覆盖不完整。" : "";
  radarEl("discovery-status").textContent=`${progress} · ${d.modes[s.mode]} · 流通市值 $${radarNum(s.min_market_cap,0)} 至 $${radarNum(s.max_market_cap,0)} · ${s.interval_seconds/60} 分钟周期 · ${d.targets[s.target_market].label} · 最近完成 ${radarTime(d.collected_at)}`;

  radarEl("discovery-summary").innerHTML=[["市值初筛",d.matched],["完整排名",d.screening.ranked],["盘口采样达标",d.screening.liquidity_passed],["本轮盘口检查",`${d.book_queue.checked ?? 0} / ${d.book_queue.requested ?? 0}`]].map(([label,value])=>`<div class="radar-metric"><span>${radarEsc(label)}</span><strong>${radarEsc(value)}</strong></div>`).join("");
  const query=radarEl("discovery-search").value.trim().toLowerCase(), sort=radarEl("discovery-sort").value;
  const filter=radarEl("discovery-filter").value;
  const items=d.items.filter(x=>[x.symbol,x.name,x.coingecko_id].some(v=>v.toLowerCase().includes(query)) && (filter==="all" || x.liquidity.status===filter)).sort((a,b)=>sort==="rank" ? (a.screen.rank ?? Infinity)-(b.screen.rank ?? Infinity) : sort==="cap" ? a.market_cap-b.market_cap : (b[sort==="volume" ? "volume_24h" : "change_24h_pct"] ?? -Infinity)-(a[sort==="volume" ? "volume_24h" : "change_24h_pct"] ?? -Infinity));
  const pages=Math.max(1,Math.ceil(items.length/25));
  discoveryState.page=Math.min(discoveryState.page,pages-1);
  const visible=items.slice(discoveryState.page*25,(discoveryState.page+1)*25);
  const write=hasPermission("radar:manage_self");
  const expanded=new Set([...radarEl("discovery-table").querySelectorAll("details[open]")].map(e=>e.dataset.key));
  radarEl("discovery-table").innerHTML=visible.length ? `<table class="discovery-table"><thead><tr><th>币种 / 平台</th><th>市值 / FDV USD</th><th>初筛排名 / 分数</th><th>目标市场 24h 成交额</th><th>24h / 7d 涨跌</th><th>目标市场流动性</th><th>风险 / 时间</th><th>观察</th></tr></thead><tbody>${visible.map(x=>{
    const markets=x.selected_upbit_markets;
    const listed=[x.binance_symbol && d.listing_fresh.binance ? radarLink("https://www.binance.com/en/futures/"+x.binance_symbol,x.binance_symbol) : "", ...markets.filter(()=>d.listing_fresh.upbit).map(m=>radarLink("https://upbit.com/exchange?code=CRIX.UPBIT."+m.market,m.market))].filter(Boolean);
    const watched=radarState.data?.watches.some(w=>w.asset.coingecko_id===x.coingecko_id);
    const warnings=[...new Set(markets.flatMap(m=>m.warning===true ? ["交易警告"] : m.warning==null ? ["警告未知"] : []))];
    const cautions=[...new Set(markets.flatMap(m=>m.cautions))];
    const warningLabel=!markets.length ? "无 Upbit 警告数据" : !d.listing_fresh.upbit ? "Upbit 警告已过期" : warnings.length ? "Upbit " + warnings.join(" / ") : cautions.length ? "Upbit 注意标记" : "Upbit 未标记警告";
    return `<tr data-coin="${radarEsc(x.coingecko_id)}"><td>${radarLink(x.url,x.symbol)}<small>${radarEsc(x.name)}</small><small>${listed.join(" · ")}</small><small>${radarEsc({upbit_pair:"Upbit 交易对映射",name_match:"代码与名称匹配",unique_symbol:"唯一代码匹配，待复核"}[x.mapping])}</small></td><td data-label="市值 / FDV USD">${radarNum(x.market_cap,0)}<small>${radarNum(x.fdv,0)}</small></td><td data-label="初筛排名 / 分数">${discoveryScore(x)}</td><td data-label="目标市场 24h 成交额">${radarNum(x.venue_ticker?.quote_volume_24h,x.target?.quote==="BTC" ? 4 : 0)} ${radarEsc(x.target?.quote || "")}<small>${radarTime(x.venue_ticker?.observed_at)}</small></td><td data-label="24h / 7d 涨跌">${radarPct(x.change_24h_pct)}<small>${radarPct(x.change_7d_pct)}</small></td><td data-label="流动性检查">${discoveryLiquidity(x)}</td><td>${radarEsc(warningLabel)}<small>${cautions.map(k=>radarEsc(discoveryCautions[k] || k)).join(" · ")}</small><small>${radarTime(x.cap_observed_at)}</small></td><td>${write ? `<button type="button" class="secondary" data-prepare="${radarEsc(x.coingecko_id)}" ${watched ? "disabled" : ""}>${watched ? "已在观察" : "配置观察"}</button>` : "只读"}</td></tr>`;
  }).join("")}</tbody></table>` : `<p class="radar-empty">${d.collected_at ? "当前没有通过全部筛选条件且数据有效的币种。请查看排除统计及来源状态。" : "等待首次自动扫描。尚无筛选结论。"}</p>`;
  radarEl("discovery-page").textContent=`${discoveryState.page+1} / ${pages} · ${items.length} 个币种`;
  radarEl("discovery-table").querySelectorAll("tr[data-coin]").forEach(row=>row.querySelectorAll("details").forEach((detail,index)=>{detail.dataset.key=row.dataset.coin+":"+index;detail.open=expanded.has(detail.dataset.key);}));
  radarEl("discovery-prev").disabled=discoveryState.page===0;
  radarEl("discovery-next").disabled=discoveryState.page>=pages-1;
  radarEl("discovery-table").querySelectorAll("[data-prepare]").forEach(button=>button.onclick=()=>prepareDiscovery(button));
  const key=JSON.stringify([d.collected_at,d.sources,d.excluded,d.unresolved.map(x=>x.symbol),s]);
  if(discoveryState.evidenceKey===key) return;
  discoveryState.evidenceKey=key;
  radarEl("discovery-method").textContent=`${d.screening.version} · ${d.targets[s.target_market].label}。完整样本至少 5 个：7 日动量（超过 15% 后不再提高该项）40 分，目标市场成交额 40 分，流通市值 / FDV 20 分；按横截面平均秩计分。缺失不补权重、不排名。分数不是上涨概率，不可跨市场或跨配置直接比较。盘口有效期 120 秒，最多检查前 100 档币安 / 30 档 Upbit；成本未含手续费、资金费或延迟，不保证实际成交。`;
  radarEl("discovery-evidence").innerHTML=`<details><summary>来源、排除统计与未匹配币种</summary><div>${Object.entries(discoveryLabels).map(([key,label])=>{ const source=d.sources[key] || {}; return `<div class="radar-source"><strong>${label}</strong><span>${source.status==="ok" ? "已读取" : "缺失或不完整"} · ${radarTime(source.observed_at)}${source.error ? `<br>${radarEsc(source.error)}` : ""}</span>${radarLink(source.url,"来源")}</div>`; }).join("")}</div><p class="radar-note">${Object.entries(d.excluded).map(([reason,count])=>`${radarEsc(discoveryReasons[reason] || reason)} ${count}`).join(" · ") || "暂无排除统计"}</p><p class="radar-note">${d.unresolved.slice(0,100).map(x=>`${radarEsc(x.symbol)}：${radarEsc(discoveryReasons[x.reason])}`).join("；") || "所选范围内暂无未匹配代码"}${d.unresolved.length>100 ? "；仅展示前 100 个" : ""}</p><p class="radar-note">币安仅覆盖当前交易中的 USDT 永续，不含交割、币本位和非加密资产合约。Upbit 为韩国市场。低流动性、同名代码和第三方目录延迟可能造成漏项。市值、FDV 和报告成交额来自 CoinGecko；表中目标市场成交额来自对应交易所。Upbit 保留原生报价单位，USDT 不保证始终等于美元。</p></details>`;
}
async function loadDiscoveryConfig() {
  if(!hasPermission("radar:view") || !hasPermission("radar:manage_self")) return;
  const generation=radarState.generation;
  const d=await radarRead("/api/radar/discovery");
  if(generation!==radarState.generation) return;
  const s=d.settings;
  radarEl("discovery-settings-fields").innerHTML=`<label class="checkbox-row"><input type="checkbox" name="enabled" ${s.enabled ? "checked" : ""}/>自动扫描</label><label class="checkbox-row"><input type="checkbox" name="exclude_upbit_warning" ${s.exclude_upbit_warning ? "checked" : ""}/>排除 Upbit 警告及警告未知项</label><label>上市范围<select name="mode">${Object.entries(d.modes).map(([k,v])=>`<option value="${k}" ${s.mode===k ? "selected" : ""}>${radarEsc(v)}</option>`).join("")}</select></label><label>Upbit 报价市场<select name="upbit_quote">${["all","KRW","BTC","USDT"].map(k=>`<option value="${k}" ${s.upbit_quote===k ? "selected" : ""}>${k==="all" ? "全部报价" : k}</option>`).join("")}</select></label>${discoveryFields.map(([name,label,min,max,step])=>`<label>${label}<input type="number" name="${name}" min="${min}" max="${max}" step="${step}" value="${s[name]}" required /></label>`).join("")}`;
  const form=radarEl("discovery-settings-form");
  radarEl("discovery-settings-fields").insertAdjacentHTML("beforeend",`<label>排名及盘口目标市场<select name="target_market">${Object.entries(d.targets).map(([k,v])=>`<option value="${k}" ${s.target_market===k ? "selected" : ""}>${radarEsc(v.label)}</option>`).join("")}</select></label>`);
  const label=form.elements.order_notional.parentElement;
  label.before(form.elements.target_market.parentElement);
  label.firstChild.textContent=`计划名义金额（${d.targets[s.target_market].quote}，非保证金）`;
  form.elements.target_market.onchange=()=>{const target=d.targets[form.elements.target_market.value];form.elements.order_notional.value=target.default_notional;label.firstChild.textContent=`计划名义金额（${target.quote}，非保证金）`;};
}
async function prepareDiscovery(button) {
  const generation=radarState.generation;
  button.disabled=true;
  try {
    const result=await api("/api/radar/discovery/prepare/"+encodeURIComponent(button.dataset.prepare),{method:"POST"});
    if(generation!==radarState.generation) return;
    setActivePage("config");
    await loadRadarConfig();
    if(generation!==radarState.generation) return;
    editRadarAsset();
    const form=radarEl("radar-asset-form");
    for(const key of ["symbol","name","coingecko_id","chain","contract","exchange_symbol"]) form.elements[key].value=result.asset[key];
    form.scrollIntoView({block:"start",behavior:"smooth"});
  } catch(error) { if(generation===radarState.generation) toast(radarError(error)); }
  finally { button.disabled=false; }
}
radarEl("discovery-search").oninput=()=>{discoveryState.page=0;renderDiscovery();};
radarEl("discovery-sort").onchange=()=>{discoveryState.page=0;renderDiscovery();};
radarEl("discovery-filter").onchange=()=>{discoveryState.page=0;renderDiscovery();};
radarEl("discovery-prev").onclick=()=>{discoveryState.page=Math.max(0,discoveryState.page-1);renderDiscovery();};
radarEl("discovery-next").onclick=()=>{discoveryState.page++;renderDiscovery();};
radarEl("discovery-refresh").onclick=async event=>{
  const button=event.currentTarget, generation=radarState.generation;
  button.disabled=true;
  try { const r=await api("/api/radar/discovery/refresh",{method:"POST"}); if(generation===radarState.generation) {toast(r.message);await loadDiscovery();} }
  catch(error) {if(generation===radarState.generation) toast(radarError(error));}
  finally {button.disabled=!hasPermission("radar:manage_self");}
};
radarEl("discovery-settings-form").onsubmit=async event=>{
  event.preventDefault();
  const form=event.currentTarget, button=form.querySelector("button[type=submit]"), generation=radarState.generation;
  const config={enabled:form.elements.enabled.checked,exclude_upbit_warning:form.elements.exclude_upbit_warning.checked,mode:form.elements.mode.value,upbit_quote:form.elements.upbit_quote.value,target_market:form.elements.target_market.value};
  discoveryFields.forEach(([key])=>{config[key]=Number(form.elements[key].value);});
  button.disabled=true;
  try {await api("/api/radar/discovery/settings",{method:"PUT",body:JSON.stringify(config)});if(generation===radarState.generation) {radarEl("discovery-config-result").textContent="个人筛选条件已保存。";await loadDiscovery();}}
  catch(error) {if(generation===radarState.generation) radarEl("discovery-config-result").textContent=radarError(error);}
  finally {button.disabled=false;}
};

async function loadDiscoveryHistory(older=false) {
  if(!hasPermission("radar:view")) return;
  const generation=radarState.generation,request=++discoveryState.historyRequest;
  try {
    const data=await radarRead("/api/radar/discovery/history?limit=20"+(older && discoveryState.historyCursor ? "&before_id="+discoveryState.historyCursor : ""));
    if(generation!==radarState.generation || request!==discoveryState.historyRequest) return;
    discoveryState.historyCursor=data.next_before_id;
    const select=radarEl("discovery-history-select"),selected=select.value;
    const html=data.items.map(x=>`<option value="${x.id}">${radarTime(x.created_at)} · ${radarEsc(x.settings.target_market)} · 排名 ${x.ranked} / 盘口达标 ${x.liquidity_passed}</option>`).join("");
    if(older) select.insertAdjacentHTML("beforeend",html);else select.innerHTML=html;
    if([...select.options].some(x=>x.value===selected)) select.value=selected;
    radarEl("discovery-history-more").hidden=!data.next_before_id;
    radarEl("discovery-history-status").textContent=select.options.length ? `最多保留 ${data.retention_days} 天或 ${data.max_runs} 轮。历史为当时冻结结果，非当前交易信号。` : "尚无个人扫描历史。下一轮起记录，不会补造过去排名。";
    if(select.value && select.value!==selected) await loadDiscoveryHistorical();
    if(!select.value) radarEl("discovery-history-detail").replaceChildren();
  } catch(error) {if(generation===radarState.generation && request===discoveryState.historyRequest) radarEl("discovery-history-status").textContent=radarError(error);}
}
async function loadDiscoveryHistorical() {
  const id=radarEl("discovery-history-select").value;
  if(!id) return;
  const generation=radarState.generation,request=++discoveryState.detailRequest;
  radarEl("discovery-history-detail").textContent="正在读取冻结记录";
  try {
    const data=await radarRead("/api/radar/discovery/history/"+encodeURIComponent(id));
    if(generation!==radarState.generation || request!==discoveryState.detailRequest) return;
    const d=data.result,records=[...d.items,...d.rejected,...d.unresolved];
    radarEl("discovery-history-detail").innerHTML=`<p class="radar-note">${radarTime(data.created_at)} · ${radarEsc(data.version)} · ${d.matched} 个初筛入围 · ${d.rejected.length} 个排除 · ${d.unresolved.length} 个身份未确认</p><details><summary>当时配置与来源</summary><pre class="radar-signal-json">${radarEsc(JSON.stringify({settings:d.settings,sources:d.sources,queue:d.book_queue},null,2))}</pre></details><div class="table-wrap" tabindex="0" aria-label="冻结筛选名单"><table class="discovery-history-table"><thead><tr><th>币种</th><th>当时排名 / 分数</th><th>当时状态 / 原因</th></tr></thead><tbody>${records.map(x=>`<tr><td>${radarEsc(x.symbol)}<small>${radarEsc(x.coingecko_id || "身份未知")}</small></td><td>${x.screen?.rank ? "#"+x.screen.rank+" / "+radarNum(x.screen.score,1) : "未排名"}</td><td>${x.liquidity ? radarEsc(discoveryCheckStates[x.liquidity.status])+"<small>"+x.liquidity.reasons.map(r=>radarEsc(discoveryReasons[r] || r)).join(" · ")+"</small>" : radarEsc((x.exclusion_reasons || [x.reason]).map(r=>discoveryReasons[r] || r).join(" · "))}</td></tr>`).join("")}</tbody></table></div>`;
  } catch(error) {if(generation===radarState.generation && request===discoveryState.detailRequest) radarEl("discovery-history-detail").textContent=radarError(error);}
}
radarEl("discovery-history-refresh").onclick=()=>loadDiscoveryHistory();
radarEl("discovery-history-more").onclick=()=>loadDiscoveryHistory(true);
radarEl("discovery-history-select").onchange=()=>loadDiscoveryHistorical();
