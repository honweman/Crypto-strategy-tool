from __future__ import annotations

from dataclasses import dataclass

from .models import Fill, Order, Side


@dataclass
class SimulatedBroker:
    fee_bps: float = 5.0
    slippage_bps: float = 2.0

    def execute_market_order(self, order: Order, reference_price: float) -> Fill:
        slippage = self.slippage_bps / 10_000
        if order.side is Side.BUY:
            price = reference_price * (1 + slippage)
        else:
            price = reference_price * (1 - slippage)
        notional = order.quantity * price
        fee = notional * self.fee_bps / 10_000
        return Fill(
            timestamp=order.timestamp,
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            price=price,
            fee=fee,
            order_id=order.id,
            reason=order.reason,
        )
