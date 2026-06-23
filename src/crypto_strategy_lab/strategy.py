from __future__ import annotations

import math
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime

from .models import Bar, ExternalEvent, TargetSignal, clamp


class Strategy:
    def on_bar(self, bar: Bar) -> list[TargetSignal]:
        return []

    def on_external_event(self, event: ExternalEvent) -> list[TargetSignal]:
        return []


@dataclass(frozen=True)
class FixedWeightConfig:
    target_weight: float = 0.20


class FixedWeightStrategy(Strategy):
    """Targets a fixed spot allocation on the first available bar."""

    def __init__(self, config: FixedWeightConfig | None = None) -> None:
        self.config = config or FixedWeightConfig()
        self.seen_symbols: set[str] = set()

    def on_bar(self, bar: Bar) -> list[TargetSignal]:
        if bar.symbol in self.seen_symbols:
            return []
        self.seen_symbols.add(bar.symbol)
        return [
            TargetSignal(
                timestamp=bar.timestamp,
                symbol=bar.symbol,
                target_weight=clamp(self.config.target_weight, 0.0, 1.0),
                confidence=1.0,
                reason="fixed_weight_entry",
            )
        ]


@dataclass(frozen=True)
class EventScoreConfig:
    positive_threshold: float = 0.60
    negative_threshold: float = -0.60
    target_weight: float = 0.20
    event_ttl_bars: int = 4


class EventScoreStrategy(Strategy):
    """Moves exposure based only on external event scores."""

    def __init__(self, config: EventScoreConfig | None = None) -> None:
        self.config = config or EventScoreConfig()
        self.active_targets: dict[str, tuple[float, int, str]] = {}
        self.current_targets: dict[str, float] = defaultdict(float)

    def on_external_event(self, event: ExternalEvent) -> list[TargetSignal]:
        if event.score >= self.config.positive_threshold:
            target = clamp(self.config.target_weight, 0.0, 1.0)
            self.active_targets[event.symbol] = (target, self.config.event_ttl_bars, event.event_type)
            self.current_targets[event.symbol] = target
            return [
                TargetSignal(
                    timestamp=event.timestamp,
                    symbol=event.symbol,
                    target_weight=target,
                    confidence=abs(event.score),
                    reason=f"event_long:{event.event_type}",
                )
            ]

        if event.score <= self.config.negative_threshold:
            self.active_targets[event.symbol] = (0.0, self.config.event_ttl_bars, event.event_type)
            self.current_targets[event.symbol] = 0.0
            return [
                TargetSignal(
                    timestamp=event.timestamp,
                    symbol=event.symbol,
                    target_weight=0.0,
                    confidence=abs(event.score),
                    reason=f"event_exit:{event.event_type}",
                )
            ]

        return []

    def on_bar(self, bar: Bar) -> list[TargetSignal]:
        if bar.symbol not in self.active_targets:
            return []

        target, ttl, event_type = self.active_targets[bar.symbol]
        ttl -= 1
        if ttl > 0:
            self.active_targets[bar.symbol] = (target, ttl, event_type)
            return []

        self.active_targets.pop(bar.symbol, None)
        if self.current_targets[bar.symbol] == 0.0:
            return []
        self.current_targets[bar.symbol] = 0.0
        return [
            TargetSignal(
                timestamp=bar.timestamp,
                symbol=bar.symbol,
                target_weight=0.0,
                confidence=1.0,
                reason=f"event_ttl_expired:{event_type}",
            )
        ]


@dataclass(frozen=True)
class StatisticalEventConfig:
    lookback: int = 20
    entry_z: float = -1.2
    exit_z: float = 0.2
    base_weight: float = 0.20
    event_weight: float = 0.25
    event_threshold: float = 0.60
    event_ttl_bars: int = 4


class StatisticalEventStrategy(Strategy):
    """Rolling z-score mean reversion with event-score overlay."""

    def __init__(self, config: StatisticalEventConfig | None = None) -> None:
        self.config = config or StatisticalEventConfig()
        self.closes: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=self.config.lookback))
        self.current_targets: dict[str, float] = defaultdict(float)
        self.event_scores: dict[str, tuple[float, int, str]] = {}

    def on_external_event(self, event: ExternalEvent) -> list[TargetSignal]:
        score = clamp(event.score, -1.0, 1.0)
        self.event_scores[event.symbol] = (score, self.config.event_ttl_bars, event.event_type)

        if score <= -self.config.event_threshold:
            self.current_targets[event.symbol] = 0.0
            return [
                TargetSignal(
                    timestamp=event.timestamp,
                    symbol=event.symbol,
                    target_weight=0.0,
                    confidence=abs(score),
                    reason=f"negative_event:{event.event_type}",
                )
            ]

        if score >= self.config.event_threshold:
            target = min(self.config.event_weight, self.config.base_weight + 0.05 * abs(score))
            self.current_targets[event.symbol] = max(self.current_targets[event.symbol], target)
            return [
                TargetSignal(
                    timestamp=event.timestamp,
                    symbol=event.symbol,
                    target_weight=self.current_targets[event.symbol],
                    confidence=score,
                    reason=f"positive_event:{event.event_type}",
                )
            ]

        return []

    def on_bar(self, bar: Bar) -> list[TargetSignal]:
        prices = self.closes[bar.symbol]
        prices.append(bar.close)

        signals: list[TargetSignal] = []
        event_target = self._event_target(bar.symbol)
        stat_target, z_score = self._statistical_target(bar.symbol)

        if stat_target is None and event_target is None:
            return signals

        target = max(stat_target or 0.0, event_target or 0.0)
        previous = self.current_targets[bar.symbol]
        if abs(target - previous) < 1e-9:
            return signals

        self.current_targets[bar.symbol] = target
        reason_parts = []
        if z_score is not None:
            reason_parts.append(f"z={z_score:.2f}")
        if event_target is not None:
            reason_parts.append("event_overlay")

        signals.append(
            TargetSignal(
                timestamp=bar.timestamp,
                symbol=bar.symbol,
                target_weight=target,
                confidence=min(1.0, abs(target) / max(self.config.base_weight, 1e-9)),
                reason=";".join(reason_parts) if reason_parts else "target_change",
            )
        )
        return signals

    def _statistical_target(self, symbol: str) -> tuple[float | None, float | None]:
        prices = self.closes[symbol]
        if len(prices) < self.config.lookback:
            return None, None

        latest = prices[-1]
        mean = sum(prices) / len(prices)
        variance = sum((price - mean) ** 2 for price in prices) / len(prices)
        std = math.sqrt(variance)
        if std <= 1e-12:
            return self.current_targets[symbol], 0.0

        z_score = (latest - mean) / std
        if z_score <= self.config.entry_z:
            return self.config.base_weight, z_score
        if z_score >= self.config.exit_z:
            return 0.0, z_score
        return self.current_targets[symbol], z_score

    def _event_target(self, symbol: str) -> float | None:
        if symbol not in self.event_scores:
            return None

        score, ttl, event_type = self.event_scores[symbol]
        ttl -= 1
        if ttl <= 0:
            self.event_scores.pop(symbol, None)
        else:
            self.event_scores[symbol] = (score, ttl, event_type)

        if score >= self.config.event_threshold:
            return min(self.config.event_weight, self.config.base_weight + 0.05 * abs(score))
        if score <= -self.config.event_threshold:
            return 0.0
        return None


@dataclass(frozen=True)
class MovingAverageCrossoverConfig:
    fast_window: int = 8
    slow_window: int = 21
    target_weight: float = 0.20
    signal_buffer: float = 0.0


class MovingAverageCrossoverStrategy(Strategy):
    """Trend-following strategy based on fast/slow moving average crossovers."""

    def __init__(self, config: MovingAverageCrossoverConfig | None = None) -> None:
        self.config = config or MovingAverageCrossoverConfig()
        self.closes: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=self.config.slow_window))
        self.current_targets: dict[str, float] = defaultdict(float)

    def on_bar(self, bar: Bar) -> list[TargetSignal]:
        prices = self.closes[bar.symbol]
        prices.append(bar.close)
        if len(prices) < self.config.slow_window:
            return []

        fast_window = min(self.config.fast_window, len(prices))
        fast_ma = sum(list(prices)[-fast_window:]) / fast_window
        slow_ma = sum(prices) / len(prices)
        buffer = max(self.config.signal_buffer, 0.0)
        target = self.current_targets[bar.symbol]

        if fast_ma > slow_ma * (1.0 + buffer):
            target = clamp(self.config.target_weight, 0.0, 1.0)
        elif fast_ma < slow_ma * (1.0 - buffer):
            target = 0.0

        previous = self.current_targets[bar.symbol]
        if abs(target - previous) < 1e-9:
            return []

        self.current_targets[bar.symbol] = target
        return [
            TargetSignal(
                timestamp=bar.timestamp,
                symbol=bar.symbol,
                target_weight=target,
                confidence=min(1.0, abs(fast_ma - slow_ma) / max(slow_ma, 1e-9) * 10),
                reason=f"ma_cross:fast={fast_ma:.4f};slow={slow_ma:.4f}",
            )
        ]


@dataclass(frozen=True)
class ChannelBreakoutConfig:
    breakout_window: int = 20
    exit_window: int = 10
    target_weight: float = 0.20


class ChannelBreakoutStrategy(Strategy):
    """Donchian-style breakout strategy for trending markets."""

    def __init__(self, config: ChannelBreakoutConfig | None = None) -> None:
        self.config = config or ChannelBreakoutConfig()
        self.highs: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=self.config.breakout_window))
        self.lows: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=max(self.config.breakout_window, self.config.exit_window)))
        self.current_targets: dict[str, float] = defaultdict(float)

    def on_bar(self, bar: Bar) -> list[TargetSignal]:
        highs = self.highs[bar.symbol]
        lows = self.lows[bar.symbol]
        signal: TargetSignal | None = None

        if len(highs) >= self.config.breakout_window and len(lows) >= self.config.exit_window:
            channel_high = max(highs)
            exit_low = min(list(lows)[-self.config.exit_window :])
            target = self.current_targets[bar.symbol]

            if bar.close > channel_high:
                target = clamp(self.config.target_weight, 0.0, 1.0)
            elif bar.close < exit_low:
                target = 0.0

            previous = self.current_targets[bar.symbol]
            if abs(target - previous) >= 1e-9:
                self.current_targets[bar.symbol] = target
                signal = TargetSignal(
                    timestamp=bar.timestamp,
                    symbol=bar.symbol,
                    target_weight=target,
                    confidence=1.0,
                    reason=f"channel_breakout:high={channel_high:.4f};exit_low={exit_low:.4f}",
                )

        highs.append(bar.high)
        lows.append(bar.low)
        return [signal] if signal else []


@dataclass(frozen=True)
class RsiMeanReversionConfig:
    rsi_window: int = 14
    oversold_rsi: float = 30.0
    exit_rsi: float = 50.0
    target_weight: float = 0.20


class RsiMeanReversionStrategy(Strategy):
    """Long-only RSI mean-reversion strategy for range-bound markets."""

    def __init__(self, config: RsiMeanReversionConfig | None = None) -> None:
        self.config = config or RsiMeanReversionConfig()
        self.closes: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=self.config.rsi_window + 1))
        self.current_targets: dict[str, float] = defaultdict(float)

    def on_bar(self, bar: Bar) -> list[TargetSignal]:
        prices = self.closes[bar.symbol]
        prices.append(bar.close)
        rsi = _rsi(prices)
        if rsi is None:
            return []

        target = self.current_targets[bar.symbol]
        if rsi <= self.config.oversold_rsi:
            target = clamp(self.config.target_weight, 0.0, 1.0)
        elif rsi >= self.config.exit_rsi:
            target = 0.0

        previous = self.current_targets[bar.symbol]
        if abs(target - previous) < 1e-9:
            return []

        self.current_targets[bar.symbol] = target
        return [
            TargetSignal(
                timestamp=bar.timestamp,
                symbol=bar.symbol,
                target_weight=target,
                confidence=min(1.0, abs(self.config.exit_rsi - rsi) / 50.0),
                reason=f"rsi_mean_reversion:rsi={rsi:.2f}",
            )
        ]


@dataclass(frozen=True)
class BollingerRsiConfig:
    lookback: int = 20
    band_std: float = 2.0
    rsi_window: int = 14
    oversold_rsi: float = 35.0
    exit_rsi: float = 55.0
    target_weight: float = 0.20


class BollingerRsiStrategy(Strategy):
    """Bollinger Band + RSI mean-reversion confirmation strategy."""

    def __init__(self, config: BollingerRsiConfig | None = None) -> None:
        self.config = config or BollingerRsiConfig()
        window = max(self.config.lookback, self.config.rsi_window + 1)
        self.closes: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=window))
        self.current_targets: dict[str, float] = defaultdict(float)

    def on_bar(self, bar: Bar) -> list[TargetSignal]:
        prices = self.closes[bar.symbol]
        prices.append(bar.close)
        if len(prices) < max(self.config.lookback, self.config.rsi_window + 1):
            return []

        band_prices = list(prices)[-self.config.lookback :]
        mean = sum(band_prices) / len(band_prices)
        variance = sum((price - mean) ** 2 for price in band_prices) / len(band_prices)
        std = math.sqrt(variance)
        lower_band = mean - self.config.band_std * std
        rsi = _rsi(deque(list(prices)[-(self.config.rsi_window + 1) :], maxlen=self.config.rsi_window + 1))
        if rsi is None:
            return []

        target = self.current_targets[bar.symbol]
        if bar.close <= lower_band and rsi <= self.config.oversold_rsi:
            target = clamp(self.config.target_weight, 0.0, 1.0)
        elif bar.close >= mean or rsi >= self.config.exit_rsi:
            target = 0.0

        previous = self.current_targets[bar.symbol]
        if abs(target - previous) < 1e-9:
            return []

        self.current_targets[bar.symbol] = target
        return [
            TargetSignal(
                timestamp=bar.timestamp,
                symbol=bar.symbol,
                target_weight=target,
                confidence=min(1.0, abs(mean - bar.close) / max(std, 1e-9) / max(self.config.band_std, 1e-9)),
                reason=f"bollinger_rsi:close={bar.close:.4f};lower={lower_band:.4f};rsi={rsi:.2f}",
            )
        ]


@dataclass(frozen=True)
class FundingRateCarryConfig:
    entry_funding_bps: float = 1.0
    exit_funding_bps: float = 0.2
    target_weight: float = 0.20
    event_ttl_bars: int = 3


class FundingRateCarryStrategy(Strategy):
    """Funding/basis carry template driven by external funding-rate events."""

    def __init__(self, config: FundingRateCarryConfig | None = None) -> None:
        self.config = config or FundingRateCarryConfig()
        self.active_targets: dict[str, tuple[float, int, float]] = {}
        self.current_targets: dict[str, float] = defaultdict(float)

    def on_external_event(self, event: ExternalEvent) -> list[TargetSignal]:
        funding_bps = float(event.score)
        if funding_bps >= self.config.entry_funding_bps:
            target = clamp(self.config.target_weight, 0.0, 1.0)
            self.active_targets[event.symbol] = (target, self.config.event_ttl_bars, funding_bps)
            self.current_targets[event.symbol] = target
            return [
                TargetSignal(
                    timestamp=event.timestamp,
                    symbol=event.symbol,
                    target_weight=target,
                    confidence=min(1.0, abs(funding_bps) / max(abs(self.config.entry_funding_bps), 1e-9)),
                    reason=f"funding_carry:funding_bps={funding_bps:.4f}",
                )
            ]
        if funding_bps <= self.config.exit_funding_bps and self.current_targets[event.symbol] > 0:
            self.active_targets.pop(event.symbol, None)
            self.current_targets[event.symbol] = 0.0
            return [
                TargetSignal(
                    timestamp=event.timestamp,
                    symbol=event.symbol,
                    target_weight=0.0,
                    confidence=1.0,
                    reason=f"funding_carry_exit:funding_bps={funding_bps:.4f}",
                )
            ]
        return []

    def on_bar(self, bar: Bar) -> list[TargetSignal]:
        if bar.symbol not in self.active_targets:
            return []
        target, ttl, funding_bps = self.active_targets[bar.symbol]
        ttl -= 1
        if ttl > 0:
            self.active_targets[bar.symbol] = (target, ttl, funding_bps)
            return []
        self.active_targets.pop(bar.symbol, None)
        if self.current_targets[bar.symbol] <= 0:
            return []
        self.current_targets[bar.symbol] = 0.0
        return [
            TargetSignal(
                timestamp=bar.timestamp,
                symbol=bar.symbol,
                target_weight=0.0,
                confidence=1.0,
                reason=f"funding_carry_ttl_expired:funding_bps={funding_bps:.4f}",
            )
        ]


@dataclass(frozen=True)
class MarketMakingConfig:
    lookback: int = 20
    min_spread_bps: float = 8.0
    max_volatility_bps: float = 120.0
    inventory_target_weight: float = 0.10


class MarketMakingStrategy(Strategy):
    """Passive market-making participation template using spread and volatility filters."""

    def __init__(self, config: MarketMakingConfig | None = None) -> None:
        self.config = config or MarketMakingConfig()
        self.closes: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=self.config.lookback + 1))
        self.current_targets: dict[str, float] = defaultdict(float)

    def on_bar(self, bar: Bar) -> list[TargetSignal]:
        prices = self.closes[bar.symbol]
        prices.append(bar.close)
        if len(prices) < self.config.lookback + 1:
            return []

        spread_bps = ((bar.high - bar.low) / bar.close * 10_000) if bar.close > 0 else 0.0
        volatility_bps = _rolling_volatility_bps(prices)
        target = self.current_targets[bar.symbol]
        if spread_bps >= self.config.min_spread_bps and volatility_bps <= self.config.max_volatility_bps:
            target = clamp(self.config.inventory_target_weight, 0.0, 1.0)
        elif volatility_bps > self.config.max_volatility_bps or spread_bps < self.config.min_spread_bps * 0.5:
            target = 0.0

        previous = self.current_targets[bar.symbol]
        if abs(target - previous) < 1e-9:
            return []
        self.current_targets[bar.symbol] = target
        return [
            TargetSignal(
                timestamp=bar.timestamp,
                symbol=bar.symbol,
                target_weight=target,
                confidence=min(1.0, spread_bps / max(self.config.min_spread_bps, 1e-9)),
                reason=f"market_making:spread_bps={spread_bps:.2f};vol_bps={volatility_bps:.2f}",
            )
        ]


@dataclass(frozen=True)
class CrossExchangeMarketMakingConfig:
    entry_profit_bps: float = 10.0
    exit_profit_bps: float = 2.0
    target_weight: float = 0.15
    event_ttl_bars: int = 3


class CrossExchangeMarketMakingStrategy(Strategy):
    """Research proxy for XEMM, driven by external net-profit-bps events."""

    def __init__(self, config: CrossExchangeMarketMakingConfig | None = None) -> None:
        self.config = config or CrossExchangeMarketMakingConfig()
        self.active_targets: dict[str, tuple[float, int, float]] = {}
        self.current_targets: dict[str, float] = defaultdict(float)

    def on_external_event(self, event: ExternalEvent) -> list[TargetSignal]:
        net_profit_bps = float(event.score)
        if net_profit_bps >= self.config.entry_profit_bps:
            target = clamp(self.config.target_weight, 0.0, 1.0)
            self.active_targets[event.symbol] = (target, self.config.event_ttl_bars, net_profit_bps)
            self.current_targets[event.symbol] = target
            return [
                TargetSignal(
                    timestamp=event.timestamp,
                    symbol=event.symbol,
                    target_weight=target,
                    confidence=min(1.0, net_profit_bps / max(self.config.entry_profit_bps, 1e-9)),
                    reason=f"xemm_entry:net_profit_bps={net_profit_bps:.4f}",
                )
            ]
        if net_profit_bps <= self.config.exit_profit_bps and self.current_targets[event.symbol] > 0:
            self.active_targets.pop(event.symbol, None)
            self.current_targets[event.symbol] = 0.0
            return [
                TargetSignal(
                    timestamp=event.timestamp,
                    symbol=event.symbol,
                    target_weight=0.0,
                    confidence=1.0,
                    reason=f"xemm_exit:net_profit_bps={net_profit_bps:.4f}",
                )
            ]
        return []

    def on_bar(self, bar: Bar) -> list[TargetSignal]:
        if bar.symbol not in self.active_targets:
            return []
        target, ttl, net_profit_bps = self.active_targets[bar.symbol]
        ttl -= 1
        if ttl > 0:
            self.active_targets[bar.symbol] = (target, ttl, net_profit_bps)
            return []
        self.active_targets.pop(bar.symbol, None)
        if self.current_targets[bar.symbol] <= 0:
            return []
        self.current_targets[bar.symbol] = 0.0
        return [
            TargetSignal(
                timestamp=bar.timestamp,
                symbol=bar.symbol,
                target_weight=0.0,
                confidence=1.0,
                reason=f"xemm_ttl_expired:net_profit_bps={net_profit_bps:.4f}",
            )
        ]


def _rsi(prices: deque[float]) -> float | None:
    if len(prices) < 2:
        return None
    gains = 0.0
    losses = 0.0
    price_list = list(prices)
    for previous, current in zip(price_list, price_list[1:]):
        change = current - previous
        if change >= 0:
            gains += change
        else:
            losses += abs(change)

    periods = len(price_list) - 1
    average_gain = gains / periods
    average_loss = losses / periods
    if average_loss <= 1e-12:
        return 100.0
    relative_strength = average_gain / average_loss
    return 100.0 - (100.0 / (1.0 + relative_strength))


def _rolling_volatility_bps(prices: deque[float]) -> float:
    price_list = list(prices)
    returns = [
        (current / previous - 1.0)
        for previous, current in zip(price_list, price_list[1:])
        if previous > 0
    ]
    if not returns:
        return 0.0
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / len(returns)
    return math.sqrt(variance) * 10_000
