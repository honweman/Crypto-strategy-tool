from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class QuoteOrder:
    side: str
    price: float
    quantity: float
    notional: float
    level: int
    status: str
    reason: str = ""
    post_only: bool = True

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_pmm_quote_plan(
    symbol: str,
    mid_price: float,
    base_balance: float = 0.0,
    quote_balance: float = 0.0,
    order_amount: float = 0.0,
    order_notional: float = 100.0,
    bid_spread_bps: float = 10.0,
    ask_spread_bps: float = 10.0,
    minimum_spread_bps: float = 1.0,
    order_levels: int = 1,
    order_level_spread_bps: float = 0.0,
    order_level_amount: float = 0.0,
    inventory_skew_enabled: bool = True,
    inventory_target_base_pct: float = 50.0,
    inventory_range_multiplier: float = 50.0,
    price_floor: float = 0.0,
    price_ceiling: float = 0.0,
    price_tick: float = 0.0,
    quantity_step: float = 0.0,
    min_notional: float = 0.0,
    max_notional: float = 0.0,
    order_refresh_time: float = 60.0,
    filled_order_delay: float = 60.0,
    allow_short: bool = False,
) -> dict[str, Any]:
    if mid_price <= 0:
        raise ValueError("mid_price_must_be_positive")
    levels = max(1, min(int(order_levels), 20))
    base_value = max(base_balance, 0.0) * mid_price
    total_value = base_value + max(quote_balance, 0.0)
    current_base_pct = (base_value / total_value * 100.0) if total_value > 0 else 0.0
    target_base_pct = _clamp(inventory_target_base_pct, 0.0, 100.0)
    inventory_gap_pct = target_base_pct - current_base_pct
    skew_unit = max(abs(inventory_range_multiplier), 1.0)
    skew_bps = inventory_gap_pct / skew_unit * max(minimum_spread_bps, 0.0) if inventory_skew_enabled else 0.0

    bid_base_spread = max(float(minimum_spread_bps), float(bid_spread_bps) - skew_bps)
    ask_base_spread = max(float(minimum_spread_bps), float(ask_spread_bps) + skew_bps)
    suppress_bid = price_ceiling > 0 and mid_price > price_ceiling
    suppress_ask = price_floor > 0 and mid_price < price_floor

    orders: list[QuoteOrder] = []
    warnings: list[str] = []
    reserved_quote = 0.0
    reserved_base = 0.0
    if suppress_bid:
        warnings.append("price_above_ceiling_only_sell_orders")
    if suppress_ask:
        warnings.append("price_below_floor_only_buy_orders")

    for level in range(1, levels + 1):
        extra_spread = (level - 1) * max(float(order_level_spread_bps), 0.0)
        quantity_seed = order_amount + (level - 1) * max(float(order_level_amount), 0.0)
        bid_price = _round_price(mid_price * (1 - (bid_base_spread + extra_spread) / 10_000), price_tick, "floor")
        ask_price = _round_price(mid_price * (1 + (ask_base_spread + extra_spread) / 10_000), price_tick, "ceil")
        bid_quantity = _round_quantity(_quantity_for_quote(quantity_seed, order_notional, bid_price), quantity_step)
        ask_quantity = _round_quantity(_quantity_for_quote(quantity_seed, order_notional, ask_price), quantity_step)
        if not suppress_bid:
            order = _quote_order("buy", bid_price, bid_quantity, level, min_notional, max_notional, quantity_step)
            order, reserved_base, reserved_quote = _apply_balance_limit(order, base_balance, quote_balance, reserved_base, reserved_quote, allow_short)
            orders.append(order)
        if not suppress_ask:
            order = _quote_order("sell", ask_price, ask_quantity, level, min_notional, max_notional, quantity_step)
            order, reserved_base, reserved_quote = _apply_balance_limit(order, base_balance, quote_balance, reserved_base, reserved_quote, allow_short)
            orders.append(order)

    return {
        "strategy": "pure_market_making",
        "symbol": symbol.strip().upper(),
        "mid_price": mid_price,
        "current_base_pct": current_base_pct,
        "target_base_pct": target_base_pct,
        "inventory_gap_pct": inventory_gap_pct,
        "inventory_skew_bps": skew_bps,
        "bid_spread_bps_effective": bid_base_spread,
        "ask_spread_bps_effective": ask_base_spread,
        "order_refresh_time": order_refresh_time,
        "filled_order_delay": filled_order_delay,
        "reserved_base": reserved_base,
        "reserved_quote": reserved_quote,
        "allow_short": allow_short,
        "orders": [order.as_dict() for order in orders],
        "warnings": warnings,
    }


def calculate_xemm_opportunity(
    symbol: str,
    maker_exchange: str,
    taker_exchange: str,
    maker_bid: float,
    maker_ask: float,
    taker_bid: float,
    taker_ask: float,
    order_amount: float = 1.0,
    min_profitability_bps: float = 10.0,
    maker_fee_bps: float = 2.0,
    taker_fee_bps: float = 5.0,
    slippage_buffer_bps: float = 5.0,
    top_depth_tolerance: float = 0.0,
) -> dict[str, Any]:
    _require_positive_prices(maker_bid, maker_ask, taker_bid, taker_ask)
    order_amount = max(float(order_amount), 0.0)
    cost_bps = float(maker_fee_bps) + float(taker_fee_bps) + float(slippage_buffer_bps)

    max_profitable_bid = taker_bid * (1 - (min_profitability_bps + cost_bps) / 10_000)
    min_profitable_ask = taker_ask * (1 + (min_profitability_bps + cost_bps) / 10_000)
    bid_net_bps = ((taker_bid * (1 - (taker_fee_bps + slippage_buffer_bps) / 10_000)) - (maker_bid * (1 + maker_fee_bps / 10_000))) / maker_bid * 10_000
    ask_net_bps = ((maker_ask * (1 - maker_fee_bps / 10_000)) - (taker_ask * (1 + (taker_fee_bps + slippage_buffer_bps) / 10_000))) / taker_ask * 10_000

    opportunities = [
        {
            "side": "maker_buy_taker_sell",
            "maker_order_side": "buy",
            "maker_price": maker_bid,
            "recommended_max_maker_bid": max_profitable_bid,
            "hedge_price": taker_bid,
            "order_amount": order_amount,
            "gross_spread_bps": (taker_bid - maker_bid) / maker_bid * 10_000,
            "net_profit_bps": bid_net_bps,
            "profitable": bid_net_bps >= min_profitability_bps,
        },
        {
            "side": "maker_sell_taker_buy",
            "maker_order_side": "sell",
            "maker_price": maker_ask,
            "recommended_min_maker_ask": min_profitable_ask,
            "hedge_price": taker_ask,
            "order_amount": order_amount,
            "gross_spread_bps": (maker_ask - taker_ask) / taker_ask * 10_000,
            "net_profit_bps": ask_net_bps,
            "profitable": ask_net_bps >= min_profitability_bps,
        },
    ]
    return {
        "strategy": "cross_exchange_market_making",
        "symbol": symbol.strip().upper(),
        "maker_exchange": maker_exchange.strip().lower(),
        "taker_exchange": taker_exchange.strip().lower(),
        "min_profitability_bps": min_profitability_bps,
        "cost_bps": cost_bps,
        "top_depth_tolerance": top_depth_tolerance,
        "opportunities": opportunities,
        "approved": any(item["profitable"] for item in opportunities),
    }


def calculate_spot_perp_arbitrage(
    symbol: str,
    spot_exchange: str,
    perp_exchange: str,
    spot_price: float,
    perp_price: float,
    notional: float = 1_000.0,
    funding_bps_per_8h: float = 1.0,
    holding_hours: float = 24.0,
    funding_interval_hours: float = 8.0,
    spot_fee_bps: float = 5.0,
    perp_fee_bps: float = 5.0,
    slippage_bps: float = 5.0,
    borrow_bps_per_day: float = 0.0,
    leverage: float = 3.0,
    maintenance_margin_ratio: float = 0.005,
    min_net_edge_bps: float = 5.0,
    include_basis_edge: bool = True,
) -> dict[str, Any]:
    _require_positive_prices(spot_price, perp_price)
    notional = max(float(notional), 0.0)
    leverage = max(float(leverage), 1e-9)
    funding_periods = max(float(holding_hours), 0.0) / max(float(funding_interval_hours), 1e-9)
    basis_bps = (perp_price / spot_price - 1.0) * 10_000
    funding_income_bps = float(funding_bps_per_8h) * funding_periods
    borrow_cost_bps = float(borrow_bps_per_day) * max(float(holding_hours), 0.0) / 24.0
    trading_cost_bps = float(spot_fee_bps) + float(perp_fee_bps) + 2 * float(slippage_bps)
    basis_edge_bps = max(basis_bps, 0.0) if include_basis_edge else 0.0
    net_edge_bps = funding_income_bps + basis_edge_bps - borrow_cost_bps - trading_cost_bps
    net_edge_value = notional * net_edge_bps / 10_000
    initial_margin_required = notional / leverage
    maintenance_margin_required = notional * max(float(maintenance_margin_ratio), 0.0)
    liquidation_buffer_pct = max((1 / leverage) - max(float(maintenance_margin_ratio), 0.0), 0.0)
    base_quantity = notional / spot_price
    return {
        "strategy": "spot_perp_funding_arbitrage",
        "symbol": symbol.strip().upper(),
        "spot_exchange": spot_exchange.strip().lower(),
        "perp_exchange": perp_exchange.strip().lower(),
        "basis_bps": basis_bps,
        "funding_income_bps": funding_income_bps,
        "basis_edge_bps": basis_edge_bps,
        "borrow_cost_bps": borrow_cost_bps,
        "trading_cost_bps": trading_cost_bps,
        "net_edge_bps": net_edge_bps,
        "net_edge_value": net_edge_value,
        "funding_periods": funding_periods,
        "notional": notional,
        "base_quantity": base_quantity,
        "initial_margin_required": initial_margin_required,
        "maintenance_margin_required": maintenance_margin_required,
        "liquidation_buffer_pct": liquidation_buffer_pct,
        "legs": [
            {"venue": "spot", "exchange": spot_exchange.strip().lower(), "side": "buy", "quantity": base_quantity, "price": spot_price},
            {"venue": "perpetual", "exchange": perp_exchange.strip().lower(), "side": "sell", "quantity": base_quantity, "price": perp_price, "reduce_only": False},
        ],
        "approved": net_edge_bps >= min_net_edge_bps,
        "reason": "approved" if net_edge_bps >= min_net_edge_bps else "net_edge_below_threshold",
    }


def _quote_order(
    side: str,
    price: float,
    quantity: float,
    level: int,
    min_notional: float,
    max_notional: float,
    quantity_step: float = 0.0,
) -> QuoteOrder:
    if max_notional > 0 and price > 0 and price * quantity > max_notional:
        quantity = _round_quantity(max_notional / price, quantity_step)
    notional = price * quantity
    if quantity <= 0 or price <= 0:
        return QuoteOrder(side=side, price=price, quantity=quantity, notional=notional, level=level, status="blocked", reason="non_positive_price_or_quantity")
    if min_notional > 0 and notional < min_notional:
        return QuoteOrder(side=side, price=price, quantity=quantity, notional=notional, level=level, status="blocked", reason="below_min_notional")
    if max_notional > 0 and notional > max_notional:
        return QuoteOrder(side=side, price=price, quantity=quantity, notional=notional, level=level, status="blocked", reason="above_max_notional")
    return QuoteOrder(side=side, price=price, quantity=quantity, notional=notional, level=level, status="ready")


def _apply_balance_limit(
    order: QuoteOrder,
    base_balance: float,
    quote_balance: float,
    reserved_base: float,
    reserved_quote: float,
    allow_short: bool,
) -> tuple[QuoteOrder, float, float]:
    if order.status != "ready":
        return order, reserved_base, reserved_quote
    if order.side == "buy":
        required_quote = reserved_quote + order.notional
        if required_quote > max(quote_balance, 0.0) + 1e-9:
            return _with_status(order, "blocked", "insufficient_quote_balance"), reserved_base, reserved_quote
        return order, reserved_base, required_quote
    if not allow_short:
        required_base = reserved_base + order.quantity
        if required_base > max(base_balance, 0.0) + 1e-9:
            return _with_status(order, "blocked", "insufficient_base_balance"), reserved_base, reserved_quote
        return order, required_base, reserved_quote
    return order, reserved_base, reserved_quote


def _with_status(order: QuoteOrder, status: str, reason: str) -> QuoteOrder:
    return QuoteOrder(
        side=order.side,
        price=order.price,
        quantity=order.quantity,
        notional=order.notional,
        level=order.level,
        status=status,
        reason=reason,
        post_only=order.post_only,
    )


def _quantity_for_quote(order_amount: float, order_notional: float, price: float) -> float:
    if order_amount > 0:
        return float(order_amount)
    return max(float(order_notional), 0.0) / price if price > 0 else 0.0


def _round_price(value: float, tick: float, mode: str) -> float:
    return _round_to_step(value, tick, mode)


def _round_quantity(value: float, step: float) -> float:
    return _round_to_step(value, step, "floor")


def _round_to_step(value: float, step: float, mode: str) -> float:
    if step <= 0:
        return value
    ratio = value / step
    if mode == "ceil":
        units = int(-(-ratio // 1))
    elif mode == "round":
        units = round(ratio)
    else:
        units = int(ratio // 1)
    return max(units * step, 0.0)


def _require_positive_prices(*values: float) -> None:
    if any(float(value) <= 0 for value in values):
        raise ValueError("prices_must_be_positive")


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(float(value), high))
