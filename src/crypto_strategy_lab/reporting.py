from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from .models import Fill


@dataclass(frozen=True)
class EquityPoint:
    timestamp: datetime
    equity: float
    cash: float
    gross_exposure: float


@dataclass(frozen=True)
class BacktestMetrics:
    starting_equity: float
    ending_equity: float
    total_return: float
    max_drawdown: float
    sharpe: float
    trade_count: int
    total_fees: float
    total_funding: float = 0.0
    liquidation_count: int = 0
    forced_exit_count: int = 0


def calculate_metrics(
    equity_curve: list[EquityPoint],
    fills: list[Fill],
    starting_equity: float,
    total_fees: float,
    total_funding: float = 0.0,
    liquidation_count: int = 0,
    forced_exit_count: int = 0,
    periods_per_year: int = 365,
) -> BacktestMetrics:
    if not equity_curve:
        return BacktestMetrics(
            starting_equity=starting_equity,
            ending_equity=starting_equity,
            total_return=0.0,
            max_drawdown=0.0,
            sharpe=0.0,
            trade_count=0,
            total_fees=total_fees,
            total_funding=total_funding,
            liquidation_count=liquidation_count,
            forced_exit_count=forced_exit_count,
        )

    ending_equity = equity_curve[-1].equity
    total_return = ending_equity / starting_equity - 1 if starting_equity else 0.0
    max_drawdown = _max_drawdown([point.equity for point in equity_curve])
    sharpe = _sharpe([point.equity for point in equity_curve], periods_per_year)
    return BacktestMetrics(
        starting_equity=starting_equity,
        ending_equity=ending_equity,
        total_return=total_return,
        max_drawdown=max_drawdown,
        sharpe=sharpe,
        trade_count=len(fills),
        total_fees=total_fees,
        total_funding=total_funding,
        liquidation_count=liquidation_count,
        forced_exit_count=forced_exit_count,
    )


def _max_drawdown(values: list[float]) -> float:
    peak = values[0]
    max_dd = 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            max_dd = min(max_dd, value / peak - 1)
    return max_dd


def _sharpe(values: list[float], periods_per_year: int) -> float:
    returns: list[float] = []
    for previous, current in zip(values, values[1:]):
        if previous > 0:
            returns.append(current / previous - 1)
    if len(returns) < 2:
        return 0.0

    mean = sum(returns) / len(returns)
    variance = sum((item - mean) ** 2 for item in returns) / (len(returns) - 1)
    std = math.sqrt(variance)
    if std <= 1e-12:
        return 0.0
    return mean / std * math.sqrt(periods_per_year)
