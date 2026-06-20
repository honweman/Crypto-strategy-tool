from __future__ import annotations

from dataclasses import dataclass, field

from .models import Fill, Side


@dataclass
class Position:
    symbol: str
    quantity: float = 0.0
    average_price: float = 0.0
    realized_pnl: float = 0.0

    def market_value(self, price: float) -> float:
        return self.quantity * price


@dataclass
class Portfolio:
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)
    total_fees: float = 0.0
    total_funding: float = 0.0

    def position(self, symbol: str) -> Position:
        symbol = symbol.upper()
        if symbol not in self.positions:
            self.positions[symbol] = Position(symbol=symbol)
        return self.positions[symbol]

    def apply_fill(self, fill: Fill) -> None:
        position = self.position(fill.symbol)
        notional = fill.quantity * fill.price
        self.total_fees += fill.fee

        if fill.side is Side.BUY:
            new_quantity = position.quantity + fill.quantity
            if new_quantity <= 0:
                position.quantity = 0.0
                position.average_price = 0.0
            else:
                old_cost = position.quantity * position.average_price
                position.average_price = (old_cost + notional) / new_quantity
                position.quantity = new_quantity
            self.cash -= notional + fill.fee
            return

        sell_quantity = min(fill.quantity, position.quantity)
        position.realized_pnl += (fill.price - position.average_price) * sell_quantity - fill.fee
        position.quantity -= sell_quantity
        if position.quantity <= 1e-12:
            position.quantity = 0.0
            position.average_price = 0.0
        self.cash += notional - fill.fee

    def apply_funding(self, amount: float) -> None:
        self.cash -= amount
        self.total_funding += amount

    def equity(self, latest_prices: dict[str, float]) -> float:
        value = self.cash
        for symbol, position in self.positions.items():
            value += position.market_value(latest_prices.get(symbol, position.average_price))
        return value

    def gross_exposure(self, latest_prices: dict[str, float]) -> float:
        return sum(
            abs(position.market_value(latest_prices.get(position.symbol, position.average_price)))
            for position in self.positions.values()
        )
