from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .strategy import (
    BollingerRsiConfig,
    BollingerRsiStrategy,
    ChannelBreakoutConfig,
    ChannelBreakoutStrategy,
    EventScoreConfig,
    EventScoreStrategy,
    FixedWeightConfig,
    FixedWeightStrategy,
    MovingAverageCrossoverConfig,
    MovingAverageCrossoverStrategy,
    RsiMeanReversionConfig,
    RsiMeanReversionStrategy,
    StatisticalEventConfig,
    StatisticalEventStrategy,
    Strategy,
)


@dataclass(frozen=True)
class StrategyParameter:
    key: str
    label: str
    type: str
    default: float | int | str | bool
    min: float | int | None = None
    max: float | int | None = None
    step: float | int | None = None


@dataclass(frozen=True)
class StrategyDefinition:
    id: str
    name: str
    description: str
    market_types: list[str]
    parameters: list[StrategyParameter]
    public_basis: str = ""

    def defaults(self) -> dict[str, Any]:
        return {parameter.key: parameter.default for parameter in self.parameters}

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


STRATEGY_DEFINITIONS: dict[str, StrategyDefinition] = {
    "statistical_event": StrategyDefinition(
        id="statistical_event",
        name="事件 + Z-score 均值回归",
        description="价格低于 rolling mean 时建仓，正面事件提高目标仓位，负面事件清仓。",
        market_types=["spot", "perpetual-long-only"],
        parameters=[
            StrategyParameter("lookback", "Lookback", "int", 20, 2, 200, 1),
            StrategyParameter("entry_z", "Entry Z", "float", -1.2, -5.0, 0.0, 0.1),
            StrategyParameter("exit_z", "Exit Z", "float", 0.2, -2.0, 5.0, 0.1),
            StrategyParameter("base_weight", "基础仓位", "float", 0.20, 0.0, 1.0, 0.01),
            StrategyParameter("event_weight", "事件仓位上限", "float", 0.25, 0.0, 1.0, 0.01),
            StrategyParameter("event_threshold", "事件阈值", "float", 0.60, 0.0, 1.0, 0.05),
            StrategyParameter("event_ttl_bars", "事件有效 Bar 数", "int", 4, 1, 100, 1),
        ],
    ),
    "event_score": StrategyDefinition(
        id="event_score",
        name="事件分数策略",
        description="只根据外部事件 score 调整仓位，适合公告、链上或情绪事件流。",
        market_types=["spot", "perpetual-long-only"],
        parameters=[
            StrategyParameter("positive_threshold", "正面事件阈值", "float", 0.60, 0.0, 1.0, 0.05),
            StrategyParameter("negative_threshold", "负面事件阈值", "float", -0.60, -1.0, 0.0, 0.05),
            StrategyParameter("target_weight", "目标仓位", "float", 0.20, 0.0, 1.0, 0.01),
            StrategyParameter("event_ttl_bars", "事件有效 Bar 数", "int", 4, 1, 100, 1),
        ],
    ),
    "ma_crossover": StrategyDefinition(
        id="ma_crossover",
        name="均线交叉趋势",
        description="短均线强于长均线时持仓，短均线跌回长均线下方时退出，适合趋势行情。",
        market_types=["spot", "perpetual-long-only"],
        public_basis="公开趋势跟随模板：moving average crossover / trend following。",
        parameters=[
            StrategyParameter("fast_window", "短均线周期", "int", 8, 2, 100, 1),
            StrategyParameter("slow_window", "长均线周期", "int", 21, 3, 300, 1),
            StrategyParameter("target_weight", "目标仓位", "float", 0.20, 0.0, 1.0, 0.01),
            StrategyParameter("signal_buffer", "信号缓冲", "float", 0.0, 0.0, 0.2, 0.005),
        ],
    ),
    "channel_breakout": StrategyDefinition(
        id="channel_breakout",
        name="通道突破",
        description="价格突破过去 N 根 K 线高点时进场，跌破较短退出通道时离场，适合强趋势山寨币。",
        market_types=["spot", "perpetual-long-only"],
        public_basis="公开突破模板：Donchian channel / high-volume breakout 的价格通道版本。",
        parameters=[
            StrategyParameter("breakout_window", "突破周期", "int", 20, 2, 300, 1),
            StrategyParameter("exit_window", "退出周期", "int", 10, 2, 200, 1),
            StrategyParameter("target_weight", "目标仓位", "float", 0.20, 0.0, 1.0, 0.01),
        ],
    ),
    "rsi_mean_reversion": StrategyDefinition(
        id="rsi_mean_reversion",
        name="RSI 均值回归",
        description="RSI 超卖时建仓，RSI 回到中性区间后退出，适合震荡或急跌反弹行情。",
        market_types=["spot", "perpetual-long-only"],
        public_basis="公开震荡策略模板：RSI oversold / mean reversion。",
        parameters=[
            StrategyParameter("rsi_window", "RSI 周期", "int", 14, 2, 100, 1),
            StrategyParameter("oversold_rsi", "超卖 RSI", "float", 30.0, 1.0, 50.0, 1.0),
            StrategyParameter("exit_rsi", "退出 RSI", "float", 50.0, 30.0, 90.0, 1.0),
            StrategyParameter("target_weight", "目标仓位", "float", 0.20, 0.0, 1.0, 0.01),
        ],
    ),
    "bollinger_rsi": StrategyDefinition(
        id="bollinger_rsi",
        name="Bollinger + RSI 反转",
        description="价格触及布林下轨且 RSI 偏低时建仓，回到均线或 RSI 修复后退出。",
        market_types=["spot", "perpetual-long-only"],
        public_basis="公开指标组合模板：Bollinger Bands + RSI confirmation。",
        parameters=[
            StrategyParameter("lookback", "布林周期", "int", 20, 2, 200, 1),
            StrategyParameter("band_std", "标准差倍数", "float", 2.0, 0.5, 5.0, 0.1),
            StrategyParameter("rsi_window", "RSI 周期", "int", 14, 2, 100, 1),
            StrategyParameter("oversold_rsi", "超卖 RSI", "float", 35.0, 1.0, 60.0, 1.0),
            StrategyParameter("exit_rsi", "退出 RSI", "float", 55.0, 30.0, 95.0, 1.0),
            StrategyParameter("target_weight", "目标仓位", "float", 0.20, 0.0, 1.0, 0.01),
        ],
    ),
    "fixed_weight": StrategyDefinition(
        id="fixed_weight",
        name="固定权重持有",
        description="第一个可交易 Bar 建立固定仓位，用作基准或长线持仓模板。",
        market_types=["spot"],
        parameters=[
            StrategyParameter("target_weight", "目标仓位", "float", 0.20, 0.0, 1.0, 0.01),
        ],
    ),
}


def list_strategy_definitions() -> list[dict[str, Any]]:
    return [definition.as_dict() for definition in STRATEGY_DEFINITIONS.values()]


def default_parameters(strategy_type: str) -> dict[str, Any]:
    definition = _definition(strategy_type)
    return definition.defaults()


def validate_parameters(strategy_type: str, parameters: dict[str, Any] | None) -> dict[str, Any]:
    definition = _definition(strategy_type)
    incoming = parameters or {}
    validated: dict[str, Any] = definition.defaults()

    for parameter in definition.parameters:
        if parameter.key not in incoming:
            continue
        value = _coerce_value(incoming[parameter.key], parameter.type)
        if parameter.min is not None and value < parameter.min:
            raise ValueError(f"{parameter.key} must be >= {parameter.min}")
        if parameter.max is not None and value > parameter.max:
            raise ValueError(f"{parameter.key} must be <= {parameter.max}")
        validated[parameter.key] = value

    _validate_cross_parameter_rules(strategy_type, validated)
    return validated


def create_strategy(strategy_type: str, parameters: dict[str, Any] | None = None) -> Strategy:
    params = validate_parameters(strategy_type, parameters)
    if strategy_type == "statistical_event":
        return StatisticalEventStrategy(StatisticalEventConfig(**params))
    if strategy_type == "event_score":
        return EventScoreStrategy(EventScoreConfig(**params))
    if strategy_type == "ma_crossover":
        return MovingAverageCrossoverStrategy(MovingAverageCrossoverConfig(**params))
    if strategy_type == "channel_breakout":
        return ChannelBreakoutStrategy(ChannelBreakoutConfig(**params))
    if strategy_type == "rsi_mean_reversion":
        return RsiMeanReversionStrategy(RsiMeanReversionConfig(**params))
    if strategy_type == "bollinger_rsi":
        return BollingerRsiStrategy(BollingerRsiConfig(**params))
    if strategy_type == "fixed_weight":
        return FixedWeightStrategy(FixedWeightConfig(**params))
    raise ValueError(f"unknown strategy type: {strategy_type}")


def _definition(strategy_type: str) -> StrategyDefinition:
    if strategy_type not in STRATEGY_DEFINITIONS:
        raise ValueError(f"unknown strategy type: {strategy_type}")
    return STRATEGY_DEFINITIONS[strategy_type]


def _coerce_value(value: Any, value_type: str) -> Any:
    if value_type == "int":
        return int(value)
    if value_type == "float":
        return float(value)
    if value_type == "bool":
        return bool(value)
    return str(value)


def _validate_cross_parameter_rules(strategy_type: str, params: dict[str, Any]) -> None:
    if strategy_type == "ma_crossover" and params["fast_window"] >= params["slow_window"]:
        raise ValueError("fast_window must be < slow_window")
    if strategy_type == "channel_breakout" and params["exit_window"] > params["breakout_window"]:
        raise ValueError("exit_window must be <= breakout_window")
    if strategy_type in {"rsi_mean_reversion", "bollinger_rsi"} and params["oversold_rsi"] >= params["exit_rsi"]:
        raise ValueError("oversold_rsi must be < exit_rsi")
