"""Point-in-time research rules. Scores are diagnostics, never probabilities."""
from __future__ import annotations

import math
import time
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator

RULE_VERSION = "altcoin-observer-v1"
SOURCE_TTLS = {"market": 900, "spot": 300, "derivatives": 7200,
               "holders": 1800, "transfers": 1800, "governance": 7200, "news": 1800}
CRITICAL_SOURCES = {"market", "spot", "holders", "transfers", "governance"}


class WalletLabel(BaseModel):
    address: str = Field(pattern=r"^0x[a-fA-F0-9]{40}$")
    label: str = Field(min_length=1, max_length=60)
    kind: Literal["exchange", "treasury", "bridge", "burn", "unknown"] = "unknown"


class RadarAsset(BaseModel):
    symbol: str = Field(pattern=r"^[A-Z0-9]{2,15}$")
    name: str = Field(min_length=2, max_length=80)
    coingecko_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,99}$")
    chain: Literal["ethereum", "base"] = "ethereum"
    contract: str = Field(pattern=r"^0x[a-fA-F0-9]{40}$")
    governor: Optional[str] = Field(default=None, pattern=r"^0x[a-fA-F0-9]{40}$")
    exchange_symbol: str = Field(pattern=r"^[A-Z0-9]{2,15}USDT$")
    wallet_labels: list[WalletLabel] = Field(default_factory=list, max_length=30)
    enabled: bool = True

    @model_validator(mode="after")
    def validate_mapping(self):
        if self.exchange_symbol != self.symbol + "USDT":
            raise ValueError("交易对必须对应当前币种，暂不支持倍数合约")
        self.contract = self.contract.lower()
        if self.governor:
            self.governor = self.governor.lower()
        for label in self.wallet_labels:
            label.address = label.address.lower()
        if len({x.address for x in self.wallet_labels}) != len(self.wallet_labels):
            raise ValueError("地址标签不能重复")
        return self


class RadarSettings(BaseModel):
    enabled: bool = True
    interval_seconds: int = Field(default=300, ge=60, le=86400)
    min_market_cap: float = Field(default=10_000_000, ge=0, le=1e13, allow_inf_nan=False)
    max_market_cap: float = Field(default=1_000_000_000, ge=1, le=1e14, allow_inf_nan=False)
    min_volume: float = Field(default=2_000_000, ge=0, le=1e13, allow_inf_nan=False)
    min_depth_usdt: float = Field(default=20_000, ge=0, le=1e10, allow_inf_nan=False)
    max_spread_bps: float = Field(default=30, ge=1, le=1000, allow_inf_nan=False)
    min_score: int = Field(default=60, ge=40, le=100)
    max_risk: int = Field(default=35, ge=0, le=60)
    min_coverage: int = Field(default=85, ge=70, le=100)
    whale_transfer_usd: float = Field(default=100_000, ge=1000, le=1e10, allow_inf_nan=False)
    notifications_enabled: bool = True
    fee_bps: float = Field(default=10, ge=0, le=100, allow_inf_nan=False)
    slippage_bps: float = Field(default=10, ge=0, le=500, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_caps(self):
        if self.min_market_cap >= self.max_market_cap:
            raise ValueError("最小市值必须小于最大市值")
        return self


DEFAULT_ASSETS = [
    {"symbol": "AWE", "name": "AWE Network", "coingecko_id": "stp-network", "chain": "base",
     "contract": "0x1b4617734c43f6159f3a70b7e06d883647512778", "exchange_symbol": "AWEUSDT",
     "governor": "0xae18aed3dd3c9cd1d0a180315b6b5fcd61ef20f1"},
    {"symbol": "ARPA", "name": "ARPA Network", "coingecko_id": "arpa", "chain": "ethereum",
     "contract": "0xba50933c268f567bdc86e1ac131be072c6b0b71a", "exchange_symbol": "ARPAUSDT"},
    {"symbol": "AGLD", "name": "Adventure Gold", "coingecko_id": "adventure-gold", "chain": "ethereum",
     "contract": "0x32353a6c91143bfd6c7d363b546e62a9a2489a20", "exchange_symbol": "AGLDUSDT",
     "governor": "0x7ae22bebf28366c328d5558e6fad935487299dfe"},
    {"symbol": "BEL", "name": "Bella Protocol", "coingecko_id": "bella-protocol", "chain": "ethereum",
     "contract": "0xa91ac63d040deb1b7a5e4d4134ad23eb0ba07e14", "exchange_symbol": "BELUSDT"},
]


def number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def source_fresh(source: dict, key: str, now: float) -> bool:
    observed = number(source.get("observed_at"))
    return (source.get("status") == "ok" and observed is not None
            and -60 <= now - observed <= SOURCE_TTLS[key])


def evaluate(sources: dict, settings: dict, previous_holders: Optional[dict] = None,
             now: Optional[float] = None) -> dict:
    now = time.time() if now is None else now
    cfg = RadarSettings(**settings)
    fresh = {k: source_fresh(sources.get(k, {}), k, now) for k in SOURCE_TTLS}
    data = {k: sources.get(k, {}).get("data", {}) if fresh[k] else {} for k in SOURCE_TTLS}
    factors, risks, blockers = [], [], []

    def factor(code: str, label: str, points: int, value: Any = None):
        factors.append({"code": code, "label": label, "points": points, "value": value})

    def risk(code: str, label: str, points: int, value: Any = None):
        risks.append({"code": code, "label": label, "points": points, "value": value})

    for key in sorted(CRITICAL_SOURCES):
        if not fresh[key]:
            blockers.append("source_unavailable:" + key)

    market, spot, deriv = data["market"], data["spot"], data["derivatives"]
    cap, volume = number(market.get("market_cap")), number(market.get("volume_24h"))
    if market.get("identity_verified") is not True:
        blockers.append("asset_identity_unverified")
    if cap is not None and cfg.min_market_cap <= cap <= cfg.max_market_cap:
        factor("cap_range", "流通市值位于观察区间", 10, cap)
    else:
        blockers.append("market_cap_outside_range")
    if volume is not None and volume >= cfg.min_volume:
        factor("volume", "报告成交额达到门槛", 5, volume)
    else:
        blockers.append("insufficient_volume")

    floating = number(market.get("float_ratio"))
    if floating is None or floating <= 0:
        blockers.append("supply_unavailable")
    else:
        if 0.6 <= floating <= 1.01:
            factor("float", "流通比例较高", 5, floating)
        elif floating < 0.3:
            risk("dilution", "低流通比例，潜在稀释压力", 20, floating)
        elif floating > 1.01:
            blockers.append("inconsistent_supply")

    drawdown, rsi = number(spot.get("drawdown_30d_pct")), number(spot.get("rsi14"))
    if drawdown is not None and -65 <= drawdown <= -20:
        factor("drawdown", "距近 30 日高点有明显回撤", 20, drawdown)
    elif drawdown is not None and drawdown < -65:
        risk("deep_decline", "极深回撤，可能存在基本面恶化", 15, drawdown)
    if rsi is not None and 25 <= rsi <= 45:
        factor("rsi", "日线 RSI 位于低位观察区", 15, rsi)
    elif rsi is not None and rsi < 25:
        risk("falling", "超卖仍可能继续下跌", 10, rsi)
    day, week = number(market.get("change_24h_pct")), number(market.get("change_7d_pct"))
    if day is not None and week is not None and week < 0 and 0 < day < 8:
        factor("stabilization", "周线下跌后出现日内企稳", 15, day)
    if day is not None and day > 15:
        risk("chasing", "短期涨幅过大，追涨与回撤风险", 20, day)
        blockers.append("already_extended")

    spread, depth = number(spot.get("spread_bps")), number(spot.get("depth_1pct_usdt"))
    if spread is None or depth is None or spread > cfg.max_spread_bps or depth < cfg.min_depth_usdt:
        blockers.append("insufficient_executable_liquidity")
        if fresh["spot"]:
            risk("liquidity", "价差过大或一侧盘口深度不足", 25, depth)
    else:
        factor("liquidity", "盘口价差和双边深度达到门槛", 10, depth)
    cg_price, spot_price = number(market.get("price")), number(spot.get("price"))
    if cg_price and spot_price and abs(spot_price / cg_price - 1) > 0.1:
        risk("price_divergence", "行情源价格偏差超过 10%", 30)
        blockers.append("price_divergence")

    funding, oi_change = number(deriv.get("funding_bps")), number(deriv.get("oi_change_24h_pct"))
    if funding is not None:
        if abs(funding) <= 5:
            factor("funding", "本期资金费未呈现极端拥挤", 5, funding)
        elif abs(funding) >= 15:
            risk("funding_crowding", "本期资金费极端，杠杆交易拥挤", 15, funding)
    if oi_change is not None and oi_change > 25:
        risk("oi_surge", "合约持仓陡增，需警惕杠杆堆积", 20, oi_change)
        if day is not None and day < -5:
            risk("oi_price_divergence", "价格下跌同时持仓增加", 15)

    holder_delta = None
    holders = data["holders"]
    if holders and previous_holders:
        old_time = number(previous_holders.get("observed_at"))
        current_time = number(sources["holders"].get("observed_at"))
        if old_time and current_time and 3600 <= current_time - old_time <= 172800:
            old = {h["address"]: number(h["balance"]) for h in previous_holders.get("data", {}).get("holders", [])}
            current = {h["address"]: number(h["balance"]) for h in holders.get("holders", [])}
            common = set(old) & set(current)
            common = {x for x in common if old[x] is not None and current[x] is not None}
            before = sum(old[x] for x in common)
            after = sum(current[x] for x in common)
            if len(common) >= 10 and before > 0:
                holder_delta = {"pct": (after / before - 1) * 100, "addresses": len(common),
                                "window_seconds": current_time - old_time}
                if holder_delta["pct"] >= 1:
                    factor("holder_balance", "共同大户地址样本余额增加（非买入证明）", 15, holder_delta)
                elif holder_delta["pct"] <= -2:
                    risk("holder_outflow", "共同大户地址样本余额下降", 15, holder_delta)
    if holder_delta is None:
        blockers.append("holder_baseline_warming")
    concentration = number(holders.get("top10_share"))
    if concentration is not None and concentration > 0.65:
        risk("concentration", "前十地址集中度高，可能包含交易所和桥", 15, concentration)

    transfers = data["transfers"]
    for transfer in transfers.get("items", []):
        usd = number(transfer.get("value_usd"))
        at = number(transfer.get("timestamp"))
        if (usd is not None and usd >= cfg.whale_transfer_usd and transfer.get("to_kind") == "exchange"
                and at is not None and 0 <= now - at <= 86400):
            risk("exchange_deposit", "大额流入已标记交易所地址，不等于已卖出", 20, transfer["tx_hash"])
            break
    gov = data["governance"]
    if gov.get("control_verified") is not True:
        blockers.append("governance_unverified")
    if gov.get("paused") is True:
        risk("paused", "代币已暂停", 100)
        blockers.append("token_paused")
    for safe in gov.get("safes", []):
        if safe.get("threshold", 0) < 2:
            risk("single_signature", "管理钱包可由单个签名执行", 25)
    if gov.get("owner_kind") == "eoa":
        risk("single_owner", "代币管理员为单地址账户", 25)
    recent_changes = [e for e in gov.get("events", []) if 0 <= now - e.get("timestamp", 0) < 259200]
    if recent_changes:
        risk("authority_change", "近 72 小时有已执行的权限或多签配置变更", 25, len(recent_changes))
        blockers.append("recent_authority_change_review")
    for item in data["news"].get("items", []):
        if item.get("risk_terms") and 0 <= now - item.get("published_at", 0) < 259200:
            risk("news_review", "近期新闻命中安全、退市或解锁关键词，需核实", 20, item["title"])
            blockers.append("news_requires_review")
            break

    coverage = round(sum(fresh.values()) / len(fresh) * 100)
    score = min(100, sum(x["points"] for x in factors))
    risk_score = min(100, sum(x["points"] for x in risks))
    if coverage < cfg.min_coverage:
        blockers.append("insufficient_coverage")
    if score < cfg.min_score:
        blockers.append("score_below_threshold")
    if risk_score > cfg.max_risk:
        blockers.append("risk_above_threshold")
    status = "candidate" if not blockers else "observe"
    if any(not fresh[k] for k in CRITICAL_SOURCES) or "asset_identity_unverified" in blockers:
        status = "incomplete"
    elif risk_score > cfg.max_risk or any(x in blockers for x in (
            "token_paused", "recent_authority_change_review", "news_requires_review", "price_divergence")):
        status = "avoid"
    elif "holder_baseline_warming" in blockers:
        status = "warming"
    return {"rule_version": RULE_VERSION, "status": status, "score": score, "risk_score": risk_score,
            "coverage": coverage, "factors": factors, "risks": risks, "blockers": sorted(set(blockers)),
            "holder_delta": holder_delta, "fresh_sources": fresh, "evaluated_at": now,
            "limitations": ["观察分不是上涨概率，规则尚未经过样本外收益验证。",
                            "地址不等于独立投资者，转账不等于买卖。",
                            "单次盘口不能证明刷量、对敲或虚假挂单。",
                            "新闻覆盖有限，未匹配报道不代表没有事件。"]}
