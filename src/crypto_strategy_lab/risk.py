from __future__ import annotations

from dataclasses import dataclass

from .models import Order, Side
from .portfolio import Portfolio


@dataclass(frozen=True)
class RiskConfig:
    max_order_notional: float = 2_500.0
    max_symbol_weight: float = 0.35
    max_gross_exposure: float = 0.80
    min_order_notional: float = 10.0
    long_only: bool = True


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    order: Order | None
    reason: str


class RiskManager:
    def __init__(self, config: RiskConfig | None = None) -> None:
        self.config = config or RiskConfig()

    def approve(self, order: Order, portfolio: Portfolio, latest_prices: dict[str, float]) -> RiskDecision:
        price = latest_prices.get(order.symbol)
        if price is None or price <= 0:
            return RiskDecision(False, None, "missing_price")

        quantity = order.quantity
        notional = quantity * price
        if notional < self.config.min_order_notional:
            return RiskDecision(False, None, "below_min_order_notional")

        if notional > self.config.max_order_notional:
            quantity = self.config.max_order_notional / price
            notional = self.config.max_order_notional

        equity = portfolio.equity(latest_prices)
        if equity <= 0:
            return RiskDecision(False, None, "non_positive_equity")

        position = portfolio.position(order.symbol)
        if self.config.long_only and order.side is Side.SELL:
            quantity = min(quantity, position.quantity)
            notional = quantity * price
            if quantity <= 1e-12:
                return RiskDecision(False, None, "long_only_no_inventory")

        if order.side is Side.BUY:
            available_notional = max(0.0, portfolio.cash)
            quantity = min(quantity, available_notional / price)
            max_symbol_notional = equity * self.config.max_symbol_weight
            current_symbol_notional = position.quantity * price
            symbol_room = max(0.0, max_symbol_notional - current_symbol_notional)
            quantity = min(quantity, symbol_room / price)

            gross_room = max(0.0, equity * self.config.max_gross_exposure - portfolio.gross_exposure(latest_prices))
            quantity = min(quantity, gross_room / price)

            if quantity * price < self.config.min_order_notional:
                return RiskDecision(False, None, "risk_limits_no_room")

        adjusted = Order(
            timestamp=order.timestamp,
            symbol=order.symbol,
            side=order.side,
            quantity=quantity,
            order_type=order.order_type,
            reason=order.reason,
            id=order.id,
        )
        return RiskDecision(True, adjusted, "approved")
