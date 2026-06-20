from __future__ import annotations

from dataclasses import dataclass

from .models import Bar


@dataclass(frozen=True)
class IndicatorPoint:
    timestamp: str
    symbol: str
    close: float
    sma_fast: float | None
    sma_slow: float | None
    ema_fast: float | None
    ema_slow: float | None
    rsi: float | None
    bollinger_mid: float | None
    bollinger_upper: float | None
    bollinger_lower: float | None
    atr: float | None
    macd: float | None
    macd_signal: float | None
    macd_histogram: float | None


def indicator_catalog() -> list[dict]:
    return [
        {"id": "sma", "name": "SMA", "description": "简单移动平均，常用于趋势过滤和均线交叉。"},
        {"id": "ema", "name": "EMA", "description": "指数移动平均，对近期价格变化更敏感。"},
        {"id": "rsi", "name": "RSI", "description": "相对强弱指标，常用于超买/超卖和均值回归。"},
        {"id": "bollinger", "name": "Bollinger Bands", "description": "布林带，用移动均值和标准差刻画波动区间。"},
        {"id": "atr", "name": "ATR", "description": "平均真实波幅，常用于止损距离和仓位风险控制。"},
        {"id": "macd", "name": "MACD", "description": "快慢 EMA 差值及信号线，常用于趋势动量确认。"},
    ]


def calculate_indicator_points(
    bars: list[Bar],
    fast_window: int = 12,
    slow_window: int = 26,
    rsi_window: int = 14,
    bollinger_window: int = 20,
    atr_window: int = 14,
    macd_signal_window: int = 9,
) -> list[IndicatorPoint]:
    if not bars:
        return []

    bars = sorted(bars, key=lambda item: item.timestamp)
    closes = [bar.close for bar in bars]
    highs = [bar.high for bar in bars]
    lows = [bar.low for bar in bars]

    sma_fast = _rolling_mean(closes, fast_window)
    sma_slow = _rolling_mean(closes, slow_window)
    ema_fast = _ema(closes, fast_window)
    ema_slow = _ema(closes, slow_window)
    rsi = _rsi(closes, rsi_window)
    bollinger_mid = _rolling_mean(closes, bollinger_window)
    bollinger_std = _rolling_std(closes, bollinger_window)
    true_ranges = _true_ranges(highs, lows, closes)
    atr = _rolling_mean(true_ranges, atr_window)
    macd = [
        fast - slow if fast is not None and slow is not None else None
        for fast, slow in zip(ema_fast, ema_slow)
    ]
    macd_signal = _ema_nullable(macd, macd_signal_window)

    points: list[IndicatorPoint] = []
    for index, bar in enumerate(bars):
        mid = bollinger_mid[index]
        std = bollinger_std[index]
        upper = mid + 2 * std if mid is not None and std is not None else None
        lower = mid - 2 * std if mid is not None and std is not None else None
        signal = macd_signal[index]
        macd_value = macd[index]
        histogram = macd_value - signal if macd_value is not None and signal is not None else None
        points.append(
            IndicatorPoint(
                timestamp=bar.timestamp.isoformat(),
                symbol=bar.symbol,
                close=bar.close,
                sma_fast=sma_fast[index],
                sma_slow=sma_slow[index],
                ema_fast=ema_fast[index],
                ema_slow=ema_slow[index],
                rsi=rsi[index],
                bollinger_mid=mid,
                bollinger_upper=upper,
                bollinger_lower=lower,
                atr=atr[index],
                macd=macd_value,
                macd_signal=signal,
                macd_histogram=histogram,
            )
        )
    return points


def _rolling_mean(values: list[float], window: int) -> list[float | None]:
    window = max(1, int(window))
    result: list[float | None] = []
    running = 0.0
    for index, value in enumerate(values):
        running += value
        if index >= window:
            running -= values[index - window]
        result.append(running / window if index + 1 >= window else None)
    return result


def _rolling_std(values: list[float], window: int) -> list[float | None]:
    window = max(1, int(window))
    result: list[float | None] = []
    for index in range(len(values)):
        if index + 1 < window:
            result.append(None)
            continue
        sample = values[index + 1 - window : index + 1]
        mean = sum(sample) / window
        variance = sum((value - mean) ** 2 for value in sample) / window
        result.append(variance ** 0.5)
    return result


def _ema(values: list[float], window: int) -> list[float | None]:
    window = max(1, int(window))
    result: list[float | None] = []
    alpha = 2 / (window + 1)
    current: float | None = None
    for index, value in enumerate(values):
        if index + 1 < window:
            result.append(None)
            continue
        if current is None:
            current = sum(values[index + 1 - window : index + 1]) / window
        else:
            current = value * alpha + current * (1 - alpha)
        result.append(current)
    return result


def _ema_nullable(values: list[float | None], window: int) -> list[float | None]:
    numeric_values: list[float] = []
    result: list[float | None] = []
    alpha = 2 / (max(1, int(window)) + 1)
    current: float | None = None
    for value in values:
        if value is None:
            result.append(None)
            continue
        numeric_values.append(value)
        if len(numeric_values) < window:
            result.append(None)
            continue
        if current is None:
            current = sum(numeric_values[-window:]) / window
        else:
            current = value * alpha + current * (1 - alpha)
        result.append(current)
    return result


def _rsi(values: list[float], window: int) -> list[float | None]:
    window = max(1, int(window))
    result: list[float | None] = [None]
    gains: list[float] = []
    losses: list[float] = []
    for previous, current in zip(values, values[1:]):
        delta = current - previous
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
        if len(gains) < window:
            result.append(None)
            continue
        recent_gains = gains[-window:]
        recent_losses = losses[-window:]
        average_gain = sum(recent_gains) / window
        average_loss = sum(recent_losses) / window
        if average_loss <= 1e-12:
            result.append(100.0)
        else:
            relative_strength = average_gain / average_loss
            result.append(100 - 100 / (1 + relative_strength))
    return result


def _true_ranges(highs: list[float], lows: list[float], closes: list[float]) -> list[float]:
    result: list[float] = []
    for index, high in enumerate(highs):
        low = lows[index]
        if index == 0:
            result.append(high - low)
            continue
        previous_close = closes[index - 1]
        result.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    return result
