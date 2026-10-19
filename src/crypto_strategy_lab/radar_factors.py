"""Closed-bar factor diagnostics; thresholds are research assumptions, not fitted alpha."""
from __future__ import annotations

import math
import statistics
from typing import Any

GROUPS = {"momentum": "相对强弱与趋势", "flow": "量能与流动性",
          "volatility": "波动与尾部风险", "regime": "市场环境与杠杆"}

# key, label, unit, group, interpretation, source
METRICS = [
    ("return_7d_pct", "7 日动量", "%", "momentum", "最近已收盘日线的区间收益；上涨不等于适合追入", "technicals"),
    ("return_30d_pct", "30 日动量", "%", "momentum", "中期价格变化，不能把持续下跌当成低估", "technicals"),
    ("return_90d_pct", "90 日动量", "%", "momentum", "较长周期背景；不足 91 根日线时缺失", "technicals"),
    ("rs_btc_7d_pct", "相对 BTC / 7 日", "%", "momentum", "币种价格比 BTC 价格的 7 日变化，正值表示相对走强", "technicals"),
    ("rs_btc_30d_pct", "相对 BTC / 30 日", "%", "momentum", "币种与 BTC 同时段总收益之比减一", "technicals"),
    ("rs_eth_7d_pct", "相对 ETH / 7 日", "%", "momentum", "相对 ETH 的强弱，不是行业内排名", "technicals"),
    ("sma20_distance_pct", "距 SMA20", "%", "momentum", "已收盘价格相对 20 日均线的偏离", "technicals"),
    ("sma20_slope_5d_pct", "SMA20 / 5 日变化", "%", "momentum", "短期均线的方向，企稳不等于反转已经确认", "technicals"),
    ("sma20_sma60_pct", "SMA20 相对 SMA60", "%", "momentum", "短中期趋势位置；负值仍处于弱势结构", "technicals"),
    ("price_z20", "价格 Z-score / 20 日", "σ", "momentum", "价格相对均值的标准化偏离；恒定序列不计算", "technicals"),
    ("volume_ratio_7_30", "7 日 / 前 30 日量能", "倍", "flow", "不重叠窗口的日均 USDT 成交额比；不证明真实买入", "technicals"),
    ("taker_buy_share_7d_pct", "主动买入成交占比 / 7 日", "%", "flow", "Binance taker-buy 报价成交额占比；不是资金净流入", "technicals"),
    ("amihud_30d_bps", "Amihud 非流动性 / 30 日", "bps/百万USDT", "flow", "日绝对收益除以同日成交额的均值，越高越难承接资金", "technicals"),
    ("reported_turnover_24h_pct", "报告换手代理 / 24h", "%", "flow", "CoinGecko 报告成交额除以流通市值；不是独立代币实际换手", "market"),
    ("volatility_30d_pct", "30 日年化波动率", "%", "volatility", "日对数收益样本标准差乘 sqrt(365)；不预测未来波动", "technicals"),
    ("atr14_pct", "ATR14 / 收盘价", "%", "volatility", "14 日真实波幅简单均值相对收盘价，衡量止损距离需求", "technicals"),
    ("max_drawdown_90d_pct", "90 日最大回撤", "%", "volatility", "窗口内从历史峰值到随后低点的最差跌幅", "technicals"),
    ("beta_btc_30d", "BTC Beta / 30 日", "倍", "volatility", "同日收益协方差 / BTC 收益方差；小样本估计不稳定", "technicals"),
    ("correlation_btc_30d", "BTC 相关性 / 30 日", "", "volatility", "只表示同步变化，不表示因果关系", "technicals"),
    ("btc_sma60_distance_pct", "BTC 距 SMA60", "%", "regime", "大盘趋势过滤器；弱市场下反弹更容易失败", "technicals"),
    ("btc_return_7d_pct", "BTC / 7 日变化", "%", "regime", "系统性市场压力，不能单独解释某个币种", "technicals"),
    ("perp_basis_bps", "永续标记价 / 指数价偏离", "bps", "regime", "瞬时溢折价，不是到期套利年化收益", "derivatives"),
    ("oi_to_spot_volume", "合约 OI / 现货 24h 成交额", "倍", "regime", "同平台 USDT 名义额的存量/流量比；不是全市场杠杆倍数", "derivatives"),
    ("fdv_market_cap", "FDV / 流通市值", "倍", "regime", "潜在稀释代理；不能代替未来 7/30 天实际解锁日历", "market"),
]
DEFERRED_FACTORS = [
    {"name": "未来 7/30 天代币解锁", "requirement": "需核验归属计划、解锁地址、数量和当时已知的公告时间"},
    {"name": "TVL、协议收入与估值", "requirement": "需逐项目核验协议映射及代币价值捕获；不适用于所有币种"},
    {"name": "活跃地址与新增用户", "requirement": "需剔除机器人、空投刷量及地址聚类，不能用持币地址数代替用户数"},
    {"name": "社交关注与情绪", "requirement": "需合规授权数据源及机器人过滤，不能把新闻条数当成全网热度"},
    {"name": "开发者活跃度与板块轮动", "requirement": "需核验官方代码仓库、板块成分及历史时点，避免幸存者偏差"},
]


def finite(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except (ValueError, TypeError, OverflowError):
        return None


def closed_daily_bars(raw: list, now: float) -> list[dict]:
    """Accept only contiguous, completed UTC candles; never fill a missing session."""
    bars = []
    for row in raw:
        if len(row) < 12:
            raise ValueError("invalid_daily_schema")
        opened, closed = int(row[0]), int(row[6])
        if closed / 1000 > now:
            continue
        if opened % 86400000 or closed != opened + 86400000 - 1:
            raise ValueError("invalid_daily_interval")
        values = [finite(row[i]) for i in (1, 2, 3, 4, 5, 7, 10)]
        if any(x is None or x < 0 for x in values) or min(values[:4]) <= 0:
            raise ValueError("invalid_daily_value")
        op, high, low, close, volume, quote, buy = values
        if high < max(op, close) or low > min(op, close) or buy > quote + max(1e-6, quote*1e-8):
            raise ValueError("inconsistent_daily_values")
        bars.append({"at": closed/1000, "open": op, "high": high, "low": low,
                     "close": close, "volume": volume, "quote_volume": quote, "buy_quote": buy})
    bars.sort(key=lambda bar: bar["at"])
    if len(bars) < 31 or any(abs(b["at"]-a["at"]-86400) > .01 for a, b in zip(bars, bars[1:])):
        raise ValueError("insufficient_or_gapped_daily_history")
    if now-bars[-1]["at"] > 86400+120:
        raise ValueError("stale_daily_history")
    return bars


def calculate_technicals(asset: list[dict], btc: list[dict], eth: list[dict]) -> dict:
    if not (asset[-1]["at"] == btc[-1]["at"] == eth[-1]["at"]):
        raise ValueError("unaligned_benchmark_close")
    btc_by_time, eth_by_time = {b["at"]: b for b in btc}, {b["at"]: b for b in eth}
    if any(b["at"] not in btc_by_time or b["at"] not in eth_by_time for b in asset[-91:]):
        raise ValueError("unaligned_benchmark_history")
    closes = [b["close"] for b in asset]
    quote = [b["quote_volume"] for b in asset]
    output: dict[str, Any] = {"as_of": asset[-1]["at"], "bars": len(asset)}

    def ret(bars, days):
        return (bars[-1]["close"]/bars[-days-1]["close"]-1)*100 if len(bars) > days else None

    for days in (7, 30, 90):
        output[f"return_{days}d_pct"] = ret(asset, days)
    for name, benchmark, days in (("btc", btc_by_time, 7), ("btc", btc_by_time, 30), ("eth", eth_by_time, 7)):
        start, end = benchmark[asset[-days-1]["at"]], benchmark[asset[-1]["at"]]
        output[f"rs_{name}_{days}d_pct"] = (closes[-1]/closes[-days-1]/(end["close"]/start["close"])-1)*100
    sma20 = statistics.mean(closes[-20:])
    output["sma20_distance_pct"] = (closes[-1]/sma20-1)*100
    output["sma20_slope_5d_pct"] = (sma20/statistics.mean(closes[-25:-5])-1)*100
    output["sma20_sma60_pct"] = (sma20/statistics.mean(closes[-60:])-1)*100 if len(closes) >= 60 else None
    sd20 = statistics.stdev(closes[-20:])
    output["price_z20"] = (closes[-1]-sma20)/sd20 if sd20 > 0 else None
    prior_volume = statistics.mean(quote[-37:-7]) if len(quote) >= 37 else None
    output["volume_ratio_7_30"] = statistics.mean(quote[-7:])/prior_volume if prior_volume else None
    volume7 = sum(quote[-7:])
    output["taker_buy_share_7d_pct"] = sum(b["buy_quote"] for b in asset[-7:])/volume7*100 if volume7 else None
    returns = [math.log(b/a) for a, b in zip(closes[-31:-1], closes[-30:])]
    output["volatility_30d_pct"] = statistics.stdev(returns)*math.sqrt(365)*100
    output["zero_volume_days_30"] = sum(q <= 0 for q in quote[-30:])
    output["amihud_30d_bps"] = (statistics.mean(abs(math.expm1(r))/q*1e10 for r, q in zip(returns, quote[-30:]))
                                 if min(quote[-30:]) > 0 else None)
    ranges = [max(b["high"]-b["low"], abs(b["high"]-a["close"]), abs(b["low"]-a["close"]))
              for a, b in zip(asset[-15:-1], asset[-14:])]
    output["atr14_pct"] = statistics.mean(ranges)/closes[-1]*100
    output["max_drawdown_90d_pct"] = None
    if len(closes) >= 91:
        peak, worst = closes[-91], 0
        for close in closes[-90:]:
            peak = max(peak, close)
            worst = min(worst, close/peak-1)
        output["max_drawdown_90d_pct"] = worst*100
    benchmark_returns = [math.log(btc_by_time[b["at"]]["close"]/btc_by_time[a["at"]]["close"])
                         for a, b in zip(asset[-31:-1], asset[-30:])]
    var_b, var_a = statistics.variance(benchmark_returns), statistics.variance(returns)
    mean_a, mean_b = statistics.mean(returns), statistics.mean(benchmark_returns)
    covariance = sum((a-mean_a)*(b-mean_b) for a, b in zip(returns, benchmark_returns))/(len(returns)-1)
    output["beta_btc_30d"] = covariance/var_b if var_b > 1e-16 else None
    output["correlation_btc_30d"] = max(-1, min(1, covariance/math.sqrt(var_a*var_b))) if min(var_a, var_b) > 1e-16 else None
    output["btc_sma60_distance_pct"] = (btc[-1]["close"]/statistics.mean(b["close"] for b in btc[-60:])-1)*100 if len(btc) >= 60 else None
    output["btc_return_7d_pct"] = ret(btc, 7)
    if any(isinstance(value, float) and not math.isfinite(value) for value in output.values()):
        raise ValueError("nonfinite_factor_calculation")
    return output


def factor_diagnostics(data: dict, config: dict) -> dict:
    technical, market, derivative, spot = (data.get(k, {}) for k in ("technicals", "market", "derivatives", "spot"))
    values = {key: finite(technical.get(key)) for key, *_ in METRICS}
    cap, volume, fdv = (finite(market.get(k)) for k in ("market_cap", "volume_24h", "fdv"))
    values["reported_turnover_24h_pct"] = volume/cap*100 if volume is not None and volume >= 0 and cap and cap > 0 else None
    values["fdv_market_cap"] = fdv/cap if fdv is not None and fdv > 0 and cap and cap > 0 else None
    values["perp_basis_bps"] = finite(derivative.get("basis_bps"))
    oi, spot_volume = finite(derivative.get("open_interest_usdt")), finite(spot.get("quote_volume_24h"))
    values["oi_to_spot_volume"] = oi/spot_volume if oi is not None and oi >= 0 and spot_volume and spot_volume > 0 else None
    values = {key: finite(value) for key, value in values.items()}
    groups, risks, blockers, points, available = [], [], [], 0, 0
    selected = config["enabled_factor_groups"]
    checks = {
        "momentum": [("rs_btc_7d_pct", 2, lambda x: x > 0), ("rs_btc_30d_pct", 1, lambda x: x > 0),
                     ("rs_eth_7d_pct", 1, lambda x: x > 0), ("sma20_slope_5d_pct", 1, lambda x: x > 0)],
        "flow": [("volume_ratio_7_30", 2, lambda x: 1.2 <= x <= 3),
                 ("taker_buy_share_7d_pct", 2, lambda x: 52 <= x <= 65),
                 ("amihud_30d_bps", 1, lambda x: x <= config["max_amihud_bps"])],
        "volatility": [("volatility_30d_pct", 2, lambda x: x <= config["max_volatility_pct"]),
                       ("atr14_pct", 2, lambda x: x <= config["max_atr_pct"]),
                       ("max_drawdown_90d_pct", 1, lambda x: x >= -60)],
        "regime": [("btc_sma60_distance_pct", 2, lambda x: x >= 0), ("btc_return_7d_pct", 1, lambda x: x > -10),
                   ("perp_basis_bps", 1, lambda x: abs(x) <= config["max_basis_bps"]),
                   ("oi_to_spot_volume", 1, lambda x: x <= config["max_oi_volume_ratio"])],
    }
    labels = {key: label for key, label, *_ in METRICS}
    conditions = {"rs_btc_7d_pct": "> 0%", "rs_btc_30d_pct": "> 0%", "rs_eth_7d_pct": "> 0%",
                  "sma20_slope_5d_pct": "> 0%", "volume_ratio_7_30": "1.2 - 3 倍",
                  "taker_buy_share_7d_pct": "52 - 65%", "amihud_30d_bps": f"<= {config['max_amihud_bps']:g} bps/百万USDT",
                  "volatility_30d_pct": f"<= {config['max_volatility_pct']:g}%", "atr14_pct": f"<= {config['max_atr_pct']:g}%",
                  "max_drawdown_90d_pct": ">= -60%", "btc_sma60_distance_pct": ">= 0%", "btc_return_7d_pct": "> -10%",
                  "perp_basis_bps": f"绝对值 <= {config['max_basis_bps']:g} bps",
                  "oi_to_spot_volume": f"<= {config['max_oi_volume_ratio']:g} 倍"}
    for group in selected:
        earned, covered, details = 0, 0, []
        for key, weight, predicate in checks[group]:
            value = values[key]
            passed = value is not None and predicate(value)
            earned += weight if passed else 0
            covered += weight if value is not None else 0
            details.append({"key": key, "label": labels[key], "value": value, "max_points": weight,
                            "condition": conditions[key],
                            "points": weight if passed else 0, "status": "missing" if value is None else "passed" if passed else "not_met"})
        groups.append({"id": group, "label": GROUPS[group], "points": earned, "max_points": 5,
                       "coverage": covered/5*100, "checks": details})
        points += earned
        available += covered

    def risk(group, code, label, weight, condition):
        if group in selected and condition:
            risks.append({"code": code, "label": label, "points": weight})

    def above(key, threshold, absolute=False):
        v = values.get(key)
        return v is not None and (abs(v) if absolute else v) > threshold

    risk("volatility", "high_volatility", "波动或真实波幅超出个人门槛", 15,
         above("volatility_30d_pct", config["max_volatility_pct"]) or above("atr14_pct", config["max_atr_pct"]))
    risk("volatility", "high_market_beta", "BTC Beta 超过 2.5，市场风险放大", 5, above("beta_btc_30d", 2.5))
    risk("flow", "volume_price_anomaly", "放量超过五倍，价格未同步转强，需复核成交质量", 10,
         above("volume_ratio_7_30", 5) and values.get("return_7d_pct") is not None and values["return_7d_pct"] <= 0)
    risk("flow", "illiquidity", "价格冲击代理高于门槛", 10, above("amihud_30d_bps", config["max_amihud_bps"]))
    risk("flow", "zero_volume", "近 30 日存在零成交额日线，流动性或停牌需复核", 10, (technical.get("zero_volume_days_30") or 0) > 0)
    risk("regime", "derivative_crowding", "永续溢折价或 OI/现货成交额偏高", 15,
         above("perp_basis_bps", config["max_basis_bps"], True) or above("oi_to_spot_volume", config["max_oi_volume_ratio"]))
    risk("regime", "market_stress", "BTC 一周跌幅超过 10%，市场承压", 10,
         values["btc_return_7d_pct"] is not None and values["btc_return_7d_pct"] < -10)
    max_points = len(selected)*5
    coverage = round(available/max_points*100) if max_points else 100
    if max_points and coverage < config["min_factor_coverage"]:
        blockers.append("factor_data_incomplete")
    return {"points": points, "max_points": max_points, "coverage": coverage, "groups": groups,
            "risks": risks, "blockers": blockers, "as_of": technical.get("as_of"),
            "metrics": [{"key": key, "label": label, "value": values[key], "unit": unit, "group": group,
                         "interpretation": interpretation, "source": source}
                        for key, label, unit, group, interpretation, source in METRICS]}
