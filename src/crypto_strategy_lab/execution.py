from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .broker import SimulatedBroker
from .models import Fill, Order, OrderStatus


@dataclass(frozen=True)
class ConnectorResult:
    status: OrderStatus
    exchange_order_id: str | None = None
    fill: Fill | None = None
    raw_response: dict | None = None
    error_message: str | None = None


class ExchangeConnector(Protocol):
    name: str

    def submit_market_order(self, order: Order, reference_price: float) -> ConnectorResult:
        ...

    def cancel_order(self, exchange_order_id: str) -> ConnectorResult:
        ...


class PaperExchangeConnector:
    name = "paper"

    def __init__(self, fee_bps: float = 5.0, slippage_bps: float = 2.0) -> None:
        self.broker = SimulatedBroker(fee_bps=fee_bps, slippage_bps=slippage_bps)

    def submit_market_order(self, order: Order, reference_price: float) -> ConnectorResult:
        fill = self.broker.execute_market_order(order, reference_price)
        return ConnectorResult(
            status=OrderStatus.FILLED,
            exchange_order_id=f"paper-{order.id}",
            fill=fill,
            raw_response={
                "connector": self.name,
                "status": OrderStatus.FILLED.value,
                "simulated": True,
                "reference_price": reference_price,
            },
        )

    def cancel_order(self, exchange_order_id: str) -> ConnectorResult:
        return ConnectorResult(
            status=OrderStatus.CANCELED,
            exchange_order_id=exchange_order_id,
            raw_response={"connector": self.name, "status": OrderStatus.CANCELED.value},
        )


class DisabledLiveConnector:
    name = "live-disabled"

    def submit_market_order(self, order: Order, reference_price: float) -> ConnectorResult:
        return ConnectorResult(
            status=OrderStatus.REJECTED,
            error_message="exchange_connector_not_configured",
            raw_response={"connector": self.name, "status": OrderStatus.REJECTED.value},
        )

    def cancel_order(self, exchange_order_id: str) -> ConnectorResult:
        return ConnectorResult(
            status=OrderStatus.REJECTED,
            exchange_order_id=exchange_order_id,
            error_message="exchange_connector_not_configured",
            raw_response={"connector": self.name, "status": OrderStatus.REJECTED.value},
        )
