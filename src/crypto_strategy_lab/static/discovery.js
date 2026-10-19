const discoveryState = {data: null, pending: false, page: 0, evidenceKey: null};
const discoveryLabels = {binance: "币安 USDT 永续", upbit: "Upbit 韩国现货", catalog: "CoinGecko 币种目录", mapping: "Upbit 交易对映射", caps: "CoinGecko 市值"};
const discoveryReasons = {listing_unavailable: "上市列表不可用或过期", market_cap_missing: "市值缺失或身份不符", market_cap_stale: "市值过期", outside_cap_range: "市值超出区间", insufficient_volume: "报告成交额不足", upbit_warning_or_unknown: "Upbit 警告或警告状态未知", stablecoin: "已知稳定币", ambiguous_identity: "重名映射冲突", unmapped_identity: "尚无可靠映射（含未识别倍数合约）"};
const discoveryFields = [["min_market_cap","最小流通市值（USD）",0,1e13,1], ["max_market_cap","最大流通市值（USD）",1,1e14,1], ["min_volume","最低报告成交额 / 24h（USD）",0,1e13,1], ["interval_seconds","扫描间隔（秒）",900,86400,1]];
const discoveryCautions = {PRICE_FLUCTUATIONS:"价格波动",TRADING_VOLUME_SOARING:"成交量激增",DEPOSIT_AMOUNT_SOARING:"充值量激增",GLOBAL_PRICE_DIFFERENCES:"跨市场价差",CONCENTRATION_OF_SMALL_ACCOUNTS:"小额账户集中"};
function resetDiscovery() {
  Object.assign(discoveryState,{data:null,pending:false,page:0,evidenceKey:null});
  for (const id of ["discovery-table","discovery-summary","discovery-evidence","discovery-settings-fields","discovery-error","discovery-config-result","discovery-page"]) radarEl(id).replaceChildren();
  radarEl("discovery-search").value="";
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
  radarEl("discovery-status").textContent=`${progress} · ${d.modes[s.mode]} · 流通市值 $${radarNum(s.min_market_cap,0)} 至 $${radarNum(s.max_market_cap,0)} · ${s.interval_seconds/60} 分钟周期 · 最近完成 ${radarTime(d.collected_at)}`;
  const required=["intersection","union"].includes(s.mode) ? ["binance","upbit"] : [s.mode];
  radarEl("discovery-summary").innerHTML=[["筛选命中",d.matched],["范围内已映射",d.mapped_in_scope],["未匹配币种",d.unresolved.length],["有效上市列表",`${required.filter(k=>d.listing_fresh[k]).length} / ${required.length}`]].map(([label,value])=>`<div class="radar-metric"><span>${radarEsc(label)}</span><strong>${radarEsc(value)}</strong></div>`).join("");
  const query=radarEl("discovery-search").value.trim().toLowerCase(), sort=radarEl("discovery-sort").value;
  const items=d.items.filter(x=>[x.symbol,x.name,x.coingecko_id].some(v=>v.toLowerCase().includes(query))).sort((a,b)=>sort==="cap" ? a.market_cap-b.market_cap : (b[sort==="volume" ? "volume_24h" : "change_24h_pct"] ?? -Infinity)-(a[sort==="volume" ? "volume_24h" : "change_24h_pct"] ?? -Infinity));
  const pages=Math.max(1,Math.ceil(items.length/25));
  discoveryState.page=Math.min(discoveryState.page,pages-1);
  const visible=items.slice(discoveryState.page*25,(discoveryState.page+1)*25);
  const write=hasPermission("radar:manage_self");
  radarEl("discovery-table").innerHTML=visible.length ? `<table class="discovery-table"><thead><tr><th>币种 / 映射</th><th>流通市值 USD</th><th>FDV USD</th><th>24h 成交额 USD</th><th>24h 变化</th><th>上市平台</th><th>风险 / 时间</th><th>观察</th></tr></thead><tbody>${visible.map(x=>{
    const markets=x.selected_upbit_markets;
    const listed=[x.binance_symbol && d.listing_fresh.binance ? radarLink("https://www.binance.com/en/futures/"+x.binance_symbol,x.binance_symbol) : "", ...markets.filter(()=>d.listing_fresh.upbit).map(m=>radarLink("https://upbit.com/exchange?code=CRIX.UPBIT."+m.market,m.market))].filter(Boolean);
    const watched=radarState.data?.watches.some(w=>w.asset.coingecko_id===x.coingecko_id);
    const warnings=[...new Set(markets.flatMap(m=>m.warning===true ? ["交易警告"] : m.warning==null ? ["警告未知"] : []))];
    const cautions=[...new Set(markets.flatMap(m=>m.cautions))];
    const warningLabel=!markets.length ? "无 Upbit 警告数据" : !d.listing_fresh.upbit ? "Upbit 警告已过期" : warnings.length ? "Upbit " + warnings.join(" / ") : cautions.length ? "Upbit 注意标记" : "Upbit 未标记警告";
    return `<tr><td>${radarLink(x.url,x.symbol)}<small>${radarEsc(x.name)}</small><small>${radarEsc({upbit_pair:"Upbit 交易对映射",name_match:"代码与名称匹配",unique_symbol:"唯一代码匹配，待复核"}[x.mapping])}</small></td><td data-label="流通市值">${radarNum(x.market_cap,0)}</td><td data-label="FDV">${radarNum(x.fdv,0)}</td><td data-label="报告成交额 / 24h">${radarNum(x.volume_24h,0)}</td><td data-label="24h 变化">${radarPct(x.change_24h_pct)}</td><td>${listed.join("<br>")}</td><td>${radarEsc(warningLabel)}<small>${cautions.map(k=>radarEsc(discoveryCautions[k] || k)).join(" · ")}</small><small>${radarTime(x.cap_observed_at)}</small></td><td>${write ? `<button type="button" class="secondary" data-prepare="${radarEsc(x.coingecko_id)}" ${watched ? "disabled" : ""}>${watched ? "已在观察" : "配置观察"}</button>` : "只读"}</td></tr>`;
  }).join("")}</tbody></table>` : `<p class="radar-empty">${d.collected_at ? "当前没有通过全部筛选条件且数据有效的币种。请查看排除统计及来源状态。" : "等待首次自动扫描。尚无筛选结论。"}</p>`;
  radarEl("discovery-page").textContent=`${discoveryState.page+1} / ${pages} · ${items.length} 个币种`;
  radarEl("discovery-prev").disabled=discoveryState.page===0;
  radarEl("discovery-next").disabled=discoveryState.page>=pages-1;
  radarEl("discovery-table").querySelectorAll("[data-prepare]").forEach(button=>button.onclick=()=>prepareDiscovery(button));
  const key=JSON.stringify([d.collected_at,d.sources,d.excluded,d.unresolved.map(x=>x.symbol),s]);
  if(discoveryState.evidenceKey===key) return;
  discoveryState.evidenceKey=key;
  radarEl("discovery-evidence").innerHTML=`<details><summary>来源、排除统计与未匹配币种</summary><div>${Object.entries(discoveryLabels).map(([key,label])=>{ const source=d.sources[key] || {}; return `<div class="radar-source"><strong>${label}</strong><span>${source.status==="ok" ? "已读取" : "缺失或不完整"} · ${radarTime(source.observed_at)}${source.error ? `<br>${radarEsc(source.error)}` : ""}</span>${radarLink(source.url,"来源")}</div>`; }).join("")}</div><p class="radar-note">${Object.entries(d.excluded).map(([reason,count])=>`${radarEsc(discoveryReasons[reason] || reason)} ${count}`).join(" · ") || "暂无排除统计"}</p><p class="radar-note">${d.unresolved.slice(0,100).map(x=>`${radarEsc(x.symbol)}：${radarEsc(discoveryReasons[x.reason])}`).join("；") || "所选范围内暂无未匹配代码"}${d.unresolved.length>100 ? "；仅展示前 100 个" : ""}</p><p class="radar-note">币安仅覆盖当前交易中的 USDT 永续，不含交割、币本位和非加密资产合约。Upbit 为韩国市场。低流动性、同名代码和第三方目录延迟可能造成漏项。市值和成交额为 CoinGecko 报告值，不是交易所保证值。</p></details>`;
}
async function loadDiscoveryConfig() {
  if(!hasPermission("radar:view") || !hasPermission("radar:manage_self")) return;
  const generation=radarState.generation;
  const d=await radarRead("/api/radar/discovery");
  if(generation!==radarState.generation) return;
  const s=d.settings;
  radarEl("discovery-settings-fields").innerHTML=`<label class="checkbox-row"><input type="checkbox" name="enabled" ${s.enabled ? "checked" : ""}/>自动扫描</label><label class="checkbox-row"><input type="checkbox" name="exclude_upbit_warning" ${s.exclude_upbit_warning ? "checked" : ""}/>排除 Upbit 警告及警告未知项</label><label>上市范围<select name="mode">${Object.entries(d.modes).map(([k,v])=>`<option value="${k}" ${s.mode===k ? "selected" : ""}>${radarEsc(v)}</option>`).join("")}</select></label><label>Upbit 报价市场<select name="upbit_quote">${["all","KRW","BTC","USDT"].map(k=>`<option value="${k}" ${s.upbit_quote===k ? "selected" : ""}>${k==="all" ? "全部报价" : k}</option>`).join("")}</select></label>${discoveryFields.map(([name,label,min,max,step])=>`<label>${label}<input type="number" name="${name}" min="${min}" max="${max}" step="${step}" value="${s[name]}" required /></label>`).join("")}`;
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
  const config={enabled:form.elements.enabled.checked,exclude_upbit_warning:form.elements.exclude_upbit_warning.checked,mode:form.elements.mode.value,upbit_quote:form.elements.upbit_quote.value};
  discoveryFields.forEach(([key])=>{config[key]=Number(form.elements[key].value);});
  button.disabled=true;
  try {await api("/api/radar/discovery/settings",{method:"PUT",body:JSON.stringify(config)});if(generation===radarState.generation) {radarEl("discovery-config-result").textContent="个人筛选条件已保存。";await loadDiscovery();}}
  catch(error) {if(generation===radarState.generation) radarEl("discovery-config-result").textContent=radarError(error);}
  finally {button.disabled=false;}
};
