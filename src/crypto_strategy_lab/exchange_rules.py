from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import httpx


@dataclass(frozen=True)
class ExchangeRuleSource:
    id: str
    name: str
    exchange: str
    market_type: str
    description: str
    symbol_hint: str
    max_limit: int = 500

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ExchangeSymbolRule:
    exchange: str
    market_type: str
    symbol: str
    min_quantity: float = 0.0
    quantity_step: float = 0.0
    price_tick: float = 0.0
    min_notional: float = 0.0
    max_notional: float = 0.0
    max_leverage: float = 1.0
    source: str = "exchange_public_api"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


RULE_SOURCE_DEFINITIONS: dict[str, ExchangeRuleSource] = {
    "binance_spot": ExchangeRuleSource(
        id="binance_spot",
        name="Binance Spot exchangeInfo",
        exchange="binance",
        market_type="spot",
        description="Binance 现货公开交易规则，解析 PRICE_FILTER、LOT_SIZE、MIN_NOTIONAL/NOTIONAL。",
        symbol_hint="BTCUSDT",
        max_limit=2000,
    ),
    "binance_usdm": ExchangeRuleSource(
        id="binance_usdm",
        name="Binance USD-M Futures exchangeInfo",
        exchange="binance",
        market_type="perpetual-long-only",
        description="Binance U 本位永续公开交易规则，解析 PRICE_FILTER、LOT_SIZE、MIN_NOTIONAL/NOTIONAL。",
        symbol_hint="BTCUSDT",
        max_limit=2000,
    ),
    "okx_spot": ExchangeRuleSource(
        id="okx_spot",
        name="OKX Spot instruments",
        exchange="okx",
        market_type="spot",
        description="OKX SPOT instruments，解析 tickSz、lotSz、minSz。",
        symbol_hint="BTC-USDT",
        max_limit=2000,
    ),
    "okx_swap": ExchangeRuleSource(
        id="okx_swap",
        name="OKX Swap instruments",
        exchange="okx",
        market_type="perpetual-long-only",
        description="OKX SWAP instruments，解析 tickSz、lotSz、minSz、lever。",
        symbol_hint="BTC-USDT-SWAP",
        max_limit=2000,
    ),
    "bybit_spot": ExchangeRuleSource(
        id="bybit_spot",
        name="Bybit Spot instruments",
        exchange="bybit",
        market_type="spot",
        description="Bybit Spot instruments-info，解析 priceFilter 与 lotSizeFilter。",
        symbol_hint="BTCUSDT",
        max_limit=500,
    ),
    "bybit_linear": ExchangeRuleSource(
        id="bybit_linear",
        name="Bybit Linear instruments",
        exchange="bybit",
        market_type="perpetual-long-only",
        description="Bybit Linear instruments-info，解析 priceFilter、lotSizeFilter、leverageFilter。",
        symbol_hint="BTCUSDT",
        max_limit=500,
    ),
}


def list_rule_sources() -> list[dict[str, Any]]:
    return [definition.as_dict() for definition in RULE_SOURCE_DEFINITIONS.values()]


async def fetch_exchange_symbol_rules(
    provider: str,
    symbol: str | None = None,
    limit: int = 500,
    client: httpx.AsyncClient | None = None,
) -> list[ExchangeSymbolRule]:
    if provider not in RULE_SOURCE_DEFINITIONS:
        raise ValueError(f"unknown rule provider: {provider}")

    definition = RULE_SOURCE_DEFINITIONS[provider]
    bounded_limit = max(1, min(int(limit), definition.max_limit))
    close_client = client is None
    http_client = client or httpx.AsyncClient(timeout=20)
    try:
        if provider == "binance_spot":
            rules = await _fetch_binance_rules(http_client, definition, "https://api.binance.com/api/v3/exchangeInfo", symbol)
        elif provider == "binance_usdm":
            rules = await _fetch_binance_rules(http_client, definition, "https://fapi.binance.com/fapi/v1/exchangeInfo", symbol)
        elif provider in {"okx_spot", "okx_swap"}:
            inst_type = "SPOT" if provider == "okx_spot" else "SWAP"
            rules = await _fetch_okx_rules(http_client, definition, inst_type, symbol)
        elif provider in {"bybit_spot", "bybit_linear"}:
            category = "spot" if provider == "bybit_spot" else "linear"
            rules = await _fetch_bybit_rules(http_client, definition, category, symbol, bounded_limit)
        else:
            raise ValueError(f"unknown rule provider: {provider}")
    finally:
        if close_client:
            await http_client.aclose()

    return rules[:bounded_limit]


async def _fetch_binance_rules(
    client: httpx.AsyncClient,
    definition: ExchangeRuleSource,
    url: str,
    symbol: str | None,
) -> list[ExchangeSymbolRule]:
    params: dict[str, Any] = {}
    if symbol:
        params["symbol"] = symbol.strip().upper().replace("-", "")
    response = await client.get(url, params=params)
    response.raise_for_status()
    payload = response.json()
    rows = payload.get("symbols", [])
    rules: list[ExchangeSymbolRule] = []
    for row in rows:
        status_value = str(row.get("status", "")).upper()
        if status_value and status_value != "TRADING":
            continue
        filters = {item.get("filterType"): item for item in row.get("filters", [])}
        price_filter = filters.get("PRICE_FILTER", {})
        lot_filter = filters.get("LOT_SIZE", {}) or filters.get("MARKET_LOT_SIZE", {})
        min_notional_filter = filters.get("MIN_NOTIONAL", {})
        notional_filter = filters.get("NOTIONAL", {})
        rules.append(
            ExchangeSymbolRule(
                exchange=definition.exchange,
                market_type=definition.market_type,
                symbol=str(row["symbol"]).upper(),
                min_quantity=_to_float(lot_filter.get("minQty")),
                quantity_step=_to_float(lot_filter.get("stepSize")),
                price_tick=_to_float(price_filter.get("tickSize")),
                min_notional=_to_float(notional_filter.get("minNotional") or min_notional_filter.get("minNotional")),
                max_notional=_to_float(notional_filter.get("maxNotional")),
                max_leverage=125.0 if definition.market_type != "spot" else 1.0,
                source=f"{definition.id}_exchange_info",
            )
        )
    return rules


async def _fetch_okx_rules(
    client: httpx.AsyncClient,
    definition: ExchangeRuleSource,
    inst_type: str,
    symbol: str | None,
) -> list[ExchangeSymbolRule]:
    params: dict[str, Any] = {"instType": inst_type}
    if symbol:
        params["instId"] = symbol.strip().upper()
    response = await client.get("https://www.okx.com/api/v5/public/instruments", params=params)
    response.raise_for_status()
    payload = response.json()
    if str(payload.get("code")) != "0":
        raise ValueError(payload.get("msg") or "okx_request_failed")
    rules: list[ExchangeSymbolRule] = []
    for row in payload.get("data", []):
        if str(row.get("state", "")).lower() not in {"live", "trading"}:
            continue
        rules.append(
            ExchangeSymbolRule(
                exchange=definition.exchange,
                market_type=definition.market_type,
                symbol=str(row["instId"]).upper(),
                min_quantity=_to_float(row.get("minSz")),
                quantity_step=_to_float(row.get("lotSz")),
                price_tick=_to_float(row.get("tickSz")),
                min_notional=0.0,
                max_notional=0.0,
                max_leverage=max(_to_float(row.get("lever")), 1.0),
                source=f"{definition.id}_instruments",
            )
        )
    return rules


async def _fetch_bybit_rules(
    client: httpx.AsyncClient,
    definition: ExchangeRuleSource,
    category: str,
    symbol: str | None,
    limit: int,
) -> list[ExchangeSymbolRule]:
    params: dict[str, Any] = {"category": category, "limit": min(limit, 1000)}
    if symbol:
        params["symbol"] = symbol.strip().upper().replace("-", "")
    cursor = ""
    rules: list[ExchangeSymbolRule] = []
    while True:
        if cursor:
            params["cursor"] = cursor
        response = await client.get("https://api.bybit.com/v5/market/instruments-info", params=params)
        response.raise_for_status()
        payload = response.json()
        if str(payload.get("retCode")) != "0":
            raise ValueError(payload.get("retMsg") or "bybit_request_failed")
        result = payload.get("result", {})
        for row in result.get("list", []):
            if str(row.get("status", "")).lower() not in {"trading", ""}:
                continue
            price_filter = row.get("priceFilter", {}) or {}
            lot_filter = row.get("lotSizeFilter", {}) or {}
            leverage_filter = row.get("leverageFilter", {}) or {}
            rules.append(
                ExchangeSymbolRule(
                    exchange=definition.exchange,
                    market_type=definition.market_type,
                    symbol=str(row["symbol"]).upper(),
                    min_quantity=_to_float(lot_filter.get("minOrderQty") or lot_filter.get("basePrecision")),
                    quantity_step=_to_float(lot_filter.get("qtyStep") or lot_filter.get("basePrecision")),
                    price_tick=_to_float(price_filter.get("tickSize")),
                    min_notional=_to_float(lot_filter.get("minNotionalValue") or lot_filter.get("minOrderAmt")),
                    max_notional=0.0,
                    max_leverage=max(_to_float(leverage_filter.get("maxLeverage")), 1.0),
                    source=f"{definition.id}_instruments_info",
                )
            )
        if len(rules) >= limit or symbol:
            break
        cursor = str(result.get("nextPageCursor") or "")
        if not cursor:
            break
    return rules[:limit]


def _to_float(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
