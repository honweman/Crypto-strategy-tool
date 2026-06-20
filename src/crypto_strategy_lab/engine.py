from __future__ import annotations

from dataclasses import dataclass, field

from .broker import SimulatedBroker
from .data import Event
from .models import Bar, ExternalEvent, Fill, Order, Side, TargetSignal, clamp
from .portfolio import Portfolio
from .reporting import BacktestMetrics, EquityPoint, calculate_metrics
from .risk import RiskDecision, RiskManager
from .strategy import Strategy


@dataclass(frozen=True)
class BacktestConfig:
    initial_cash: float = 10_000.0
    min_rebalance_notional: float = 10.0
    execution_delay_bars: int = 0
    funding_bps_per_bar: float = 0.0
    stop_loss_pct: float = 0.0
    take_profit_pct: float = 0.0
    max_holding_bars: int = 0
    leverage: float = 1.0
    maintenance_margin_ratio: float = 0.005
    market_type: str = "spot"


@dataclass
class RejectedOrder:
    order: Order | None
    reason: str


@dataclass(frozen=True)
class PendingSignal:
    signal: TargetSignal
    delay_remaining: int


@dataclass
class BacktestResult:
    equity_curve: list[EquityPoint]
    fills: list[Fill]
    rejected_orders: list[RejectedOrder]
    metrics: BacktestMetrics
    latest_prices: dict[str, float] = field(default_factory=dict)


class BacktestEngine:
    def __init__(
        self,
        strategy: Strategy,
        broker: SimulatedBroker | None = None,
        risk_manager: RiskManager | None = None,
        config: BacktestConfig | None = None,
    ) -> None:
        self.strategy = strategy
        self.broker = broker or SimulatedBroker()
        self.risk_manager = risk_manager or RiskManager()
        self.config = config or BacktestConfig()

    def run(self, feed: list[Event] | tuple[Event, ...] | object) -> BacktestResult:
        portfolio = Portfolio(cash=self.config.initial_cash)
        latest_prices: dict[str, float] = {}
        equity_curve: list[EquityPoint] = []
        fills: list[Fill] = []
        rejected_orders: list[RejectedOrder] = []
        pending_signals: list[PendingSignal] = []
        holding_bars: dict[str, int] = {}
        liquidation_count = 0
        forced_exit_count = 0

        for event in feed:
            if isinstance(event, Bar):
                latest_prices[event.symbol] = event.close
                liquidation_count += self._apply_liquidation_checks(event, portfolio, latest_prices, fills, holding_bars)
                forced_exit_count += self._apply_forced_exit_checks(event, portfolio, latest_prices, fills, holding_bars)
                self._apply_funding(event, portfolio, latest_prices)

                still_pending = self._process_pending_signals(
                    pending_signals,
                    portfolio,
                    latest_prices,
                    fills,
                    rejected_orders,
                    holding_bars,
                    execution_timestamp=event.timestamp,
                )
                pending_signals = still_pending

                signals = self.strategy.on_bar(event)
                pending_signals.extend(
                    self._process_new_signals(
                        signals,
                        portfolio,
                        latest_prices,
                        fills,
                        rejected_orders,
                        holding_bars,
                        execution_timestamp=event.timestamp,
                    )
                )
                self._increment_holding_bars(portfolio, holding_bars)
                equity_curve.append(
                    EquityPoint(
                        timestamp=event.timestamp,
                        equity=portfolio.equity(latest_prices),
                        cash=portfolio.cash,
                        gross_exposure=portfolio.gross_exposure(latest_prices),
                    )
                )
                continue

            if isinstance(event, ExternalEvent):
                signals = self.strategy.on_external_event(event)
                pending_signals.extend(
                    self._process_new_signals(signals, portfolio, latest_prices, fills, rejected_orders, holding_bars)
                )

        metrics = calculate_metrics(
            equity_curve=equity_curve,
            fills=fills,
            starting_equity=self.config.initial_cash,
            total_fees=portfolio.total_fees,
            total_funding=portfolio.total_funding,
            liquidation_count=liquidation_count,
            forced_exit_count=forced_exit_count,
        )
        return BacktestResult(
            equity_curve=equity_curve,
            fills=fills,
            rejected_orders=rejected_orders,
            metrics=metrics,
            latest_prices=latest_prices,
        )

    def _process_new_signals(
        self,
        signals: list[TargetSignal],
        portfolio: Portfolio,
        latest_prices: dict[str, float],
        fills: list[Fill],
        rejected_orders: list[RejectedOrder],
        holding_bars: dict[str, int],
        execution_timestamp=None,
    ) -> list[PendingSignal]:
        pending = [
            PendingSignal(signal=signal, delay_remaining=max(0, int(self.config.execution_delay_bars)))
            for signal in signals
        ]
        return self._process_pending_signals(
            pending,
            portfolio,
            latest_prices,
            fills,
            rejected_orders,
            holding_bars,
            newly_created=True,
            execution_timestamp=execution_timestamp,
        )

    def _process_pending_signals(
        self,
        signals: list[PendingSignal],
        portfolio: Portfolio,
        latest_prices: dict[str, float],
        fills: list[Fill],
        rejected_orders: list[RejectedOrder],
        holding_bars: dict[str, int],
        newly_created: bool = False,
        execution_timestamp=None,
    ) -> list[PendingSignal]:
        pending: list[PendingSignal] = []
        for signal in signals:
            delay_remaining = signal.delay_remaining
            if delay_remaining > 0:
                if newly_created:
                    pending.append(signal)
                    continue
                delay_remaining -= 1
                if delay_remaining > 0:
                    pending.append(PendingSignal(signal=signal.signal, delay_remaining=delay_remaining))
                    continue

            order = self._order_from_signal(signal.signal, portfolio, latest_prices)
            if order is None:
                if signal.signal.symbol not in latest_prices:
                    pending.append(signal)
                continue
            if execution_timestamp is not None and order.timestamp != execution_timestamp:
                order = Order(
                    timestamp=execution_timestamp,
                    symbol=order.symbol,
                    side=order.side,
                    quantity=order.quantity,
                    order_type=order.order_type,
                    reason=order.reason,
                    id=order.id,
                )

            decision = self.risk_manager.approve(order, portfolio, latest_prices)
            if not decision.approved or decision.order is None:
                rejected_orders.append(RejectedOrder(order=order, reason=decision.reason))
                continue

            self._execute_order(decision.order, latest_prices[decision.order.symbol], portfolio, fills, holding_bars)
        return pending

    def _order_from_signal(
        self,
        signal: TargetSignal,
        portfolio: Portfolio,
        latest_prices: dict[str, float],
    ) -> Order | None:
        price = latest_prices.get(signal.symbol)
        if price is None or price <= 0:
            return None

        target_weight = clamp(signal.target_weight, 0.0, 1.0)
        equity = portfolio.equity(latest_prices)
        target_notional = equity * target_weight
        current_notional = portfolio.position(signal.symbol).quantity * price
        delta = target_notional - current_notional
        if abs(delta) < self.config.min_rebalance_notional:
            return None

        side = Side.BUY if delta > 0 else Side.SELL
        quantity = abs(delta) / price
        return Order(
            timestamp=signal.timestamp,
            symbol=signal.symbol,
            side=side,
            quantity=quantity,
            reason=signal.reason,
        )

    def _execute_order(
        self,
        order: Order,
        reference_price: float,
        portfolio: Portfolio,
        fills: list[Fill],
        holding_bars: dict[str, int],
    ) -> None:
        before_quantity = portfolio.position(order.symbol).quantity
        fill = self.broker.execute_market_order(order, reference_price)
        portfolio.apply_fill(fill)
        after_quantity = portfolio.position(order.symbol).quantity
        if before_quantity <= 1e-12 and after_quantity > 1e-12:
            holding_bars[order.symbol] = 0
        elif after_quantity <= 1e-12:
            holding_bars.pop(order.symbol, None)
        fills.append(fill)

    def _apply_funding(self, bar: Bar, portfolio: Portfolio, latest_prices: dict[str, float]) -> None:
        if self.config.funding_bps_per_bar == 0 or self.config.market_type == "spot":
            return
        position = portfolio.position(bar.symbol)
        if position.quantity <= 1e-12:
            return
        notional = position.quantity * latest_prices[bar.symbol]
        portfolio.apply_funding(notional * self.config.funding_bps_per_bar / 10_000)

    def _apply_liquidation_checks(
        self,
        bar: Bar,
        portfolio: Portfolio,
        latest_prices: dict[str, float],
        fills: list[Fill],
        holding_bars: dict[str, int],
    ) -> int:
        if self.config.market_type == "spot" or self.config.leverage <= 1:
            return 0
        position = portfolio.position(bar.symbol)
        if position.quantity <= 1e-12 or position.average_price <= 0:
            return 0
        liquidation_price = position.average_price * (1 - 1 / self.config.leverage + self.config.maintenance_margin_ratio)
        if bar.low > liquidation_price:
            return 0
        order = Order(
            timestamp=bar.timestamp,
            symbol=bar.symbol,
            side=Side.SELL,
            quantity=position.quantity,
            reason=f"liquidation:price={liquidation_price:.4f}",
        )
        self._execute_order(order, liquidation_price, portfolio, fills, holding_bars)
        latest_prices[bar.symbol] = liquidation_price
        return 1

    def _apply_forced_exit_checks(
        self,
        bar: Bar,
        portfolio: Portfolio,
        latest_prices: dict[str, float],
        fills: list[Fill],
        holding_bars: dict[str, int],
    ) -> int:
        position = portfolio.position(bar.symbol)
        if position.quantity <= 1e-12 or position.average_price <= 0:
            return 0

        exit_price: float | None = None
        reason = ""
        if self.config.stop_loss_pct > 0:
            stop_price = position.average_price * (1 - self.config.stop_loss_pct)
            if bar.low <= stop_price:
                exit_price = stop_price
                reason = f"stop_loss:{self.config.stop_loss_pct:.4f}"

        if exit_price is None and self.config.take_profit_pct > 0:
            take_profit_price = position.average_price * (1 + self.config.take_profit_pct)
            if bar.high >= take_profit_price:
                exit_price = take_profit_price
                reason = f"take_profit:{self.config.take_profit_pct:.4f}"

        if exit_price is None and self.config.max_holding_bars > 0:
            if holding_bars.get(bar.symbol, 0) >= self.config.max_holding_bars:
                exit_price = bar.close
                reason = f"max_holding_bars:{self.config.max_holding_bars}"

        if exit_price is None:
            return 0

        order = Order(
            timestamp=bar.timestamp,
            symbol=bar.symbol,
            side=Side.SELL,
            quantity=position.quantity,
            reason=reason,
        )
        self._execute_order(order, exit_price, portfolio, fills, holding_bars)
        latest_prices[bar.symbol] = exit_price
        return 1

    def _increment_holding_bars(self, portfolio: Portfolio, holding_bars: dict[str, int]) -> None:
        for symbol, position in portfolio.positions.items():
            if position.quantity > 1e-12:
                holding_bars[symbol] = holding_bars.get(symbol, 0) + 1
