from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

import httpx


@dataclass(frozen=True)
class MarketQuoteSourceDefinition:
    id: str
    name: str
    exchange: str
    market_type: str
    description: str
    symbol_hint: str
    supports_funding: bool = False
    notes: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MarketQuote:
    provider: str
    exchange: str
    market_type: str
    symbol: str
    bid: float
    ask: float
    last: float
    mid: float
    spread_bps: float
    funding_rate_bps_8h: float | None = None
    next_funding_time: str | None = None
    source_timestamp: str | None = None
    raw: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


QUOTE_SOURCE_DEFINITIONS: dict[str, MarketQuoteSourceDefinition] = {
    "binance_spot": MarketQuoteSourceDefinition(
        id="binance_spot",
        name="Binance Spot Quote",
        exchange="binance",
        market_type="spot",
        description="Binance 现货公开 best bid/ask 与 last price。",
        symbol_hint="BTCUSDT",
    ),
    "binance_usdm": MarketQuoteSourceDefinition(
        id="binance_usdm",
        name="Binance USD-M Futures Quote",
        exchange="binance",
        market_type="perpetual-long-only",
        description="Binance U 本位合约 best bid/ask、last price 与资金费率。",
        symbol_hint="BTCUSDT",
        supports_funding=True,
    ),
    "okx_spot": MarketQuoteSourceDefinition(
        id="okx_spot",
        name="OKX Spot Quote",
        exchange="okx",
        market_type="spot",
        description="OKX 现货 ticker bid/ask/last。",
        symbol_hint="BTC-USDT",
    ),
    "okx_swap": MarketQuoteSourceDefinition(
        id="okx_swap",
        name="OKX Swap Quote",
        exchange="okx",
        market_type="perpetual-long-only",
        description="OKX Swap ticker 与 public funding-rate。",
        symbol_hint="BTC-USDT-SWAP",
        supports_funding=True,
    ),
    "bybit_spot": MarketQuoteSourceDefinition(
        id="bybit_spot",
        name="Bybit Spot Quote",
        exchange="bybit",
        market_type="spot",
        description="Bybit 现货 ticker bid/ask/last。",
        symbol_hint="BTCUSDT",
    ),
    "bybit_linear": MarketQuoteSourceDefinition(
        id="bybit_linear",
        name="Bybit Linear Quote",
        exchange="bybit",
        market_type="perpetual-long-only",
        description="Bybit 线性合约 ticker 与 fundingRate。",
        symbol_hint="BTCUSDT",
        supports_funding=True,
    ),
}


def list_market_quote_sources() -> list[dict[str, Any]]:
    return [definition.as_dict() for definition in QUOTE_SOURCE_DEFINITIONS.values()]


async def fetch_market_quote(provider: str, symbol: str, client: httpx.AsyncClient | None = None) -> MarketQuote:
    if provider not in QUOTE_SOURCE_DEFINITIONS:
        raise ValueError(f"unknown quote provider: {provider}")
    definition = QUOTE_SOURCE_DEFINITIONS[provider]
    close_client = client is None
    http_client = client or httpx.AsyncClient(timeout=15)
    try:
        if provider == "binance_spot":
            quote = await _fetch_binance_quote(provider, definition, symbol, http_client, futures=False)
        elif provider == "binance_usdm":
            quote = await _fetch_binance_quote(provider, definition, symbol, http_client, futures=True)
        elif provider in {"okx_spot", "okx_swap"}:
            quote = await _fetch_okx_quote(provider, definition, symbol, http_client)
        elif provider in {"bybit_spot", "bybit_linear"}:
            category = "spot" if provider == "bybit_spot" else "linear"
            quote = await _fetch_bybit_quote(provider, definition, symbol, http_client, category)
        else:
            raise ValueError(f"unknown quote provider: {provider}")
    finally:
        if close_client:
            await http_client.aclose()
    return quote


async def _fetch_binance_quote(
    provider: str,
    definition: MarketQuoteSourceDefinition,
    symbol: str,
    client: httpx.AsyncClient,
    futures: bool,
) -> MarketQuote:
    normalized = symbol.strip().upper().replace("-", "")
    base_url = "https://fapi.binance.com/fapi/v1" if futures else "https://api.binance.com/api/v3"
    book_response = await client.get(f"{base_url}/ticker/bookTicker", params={"symbol": normalized})
    book_response.raise_for_status()
    price_response = await client.get(f"{base_url}/ticker/price", params={"symbol": normalized})
    price_response.raise_for_status()
    book = book_response.json()
    price = price_response.json()
    funding_rate_bps: float | None = None
    next_funding_time: str | None = None
    funding_raw: dict[str, Any] | None = None
    if futures:
        funding_response = await client.get(f"{base_url}/premiumIndex", params={"symbol": normalized})
        funding_response.raise_for_status()
        funding_raw = funding_response.json()
        funding_rate_bps = _to_float(funding_raw.get("lastFundingRate")) * 10_000
        next_funding_time = _ms_to_iso(funding_raw.get("nextFundingTime"))
    return _build_quote(
        provider=provider,
        definition=definition,
        symbol=normalized,
        bid=_to_float(book.get("bidPrice")),
        ask=_to_float(book.get("askPrice")),
        last=_to_float(price.get("price")),
        funding_rate_bps_8h=funding_rate_bps,
        next_funding_time=next_funding_time,
        source_timestamp=_ms_to_iso(funding_raw.get("time")) if funding_raw else None,
        raw={"book": book, "price": price, "funding": funding_raw or {}},
    )


async def _fetch_okx_quote(
    provider: str,
    definition: MarketQuoteSourceDefinition,
    symbol: str,
    client: httpx.AsyncClient,
) -> MarketQuote:
    normalized = _normalize_okx_symbol(provider, symbol)
    ticker_response = await client.get("https://www.okx.com/api/v5/market/ticker", params={"instId": normalized})
    ticker_response.raise_for_status()
    ticker_payload = ticker_response.json()
    if str(ticker_payload.get("code")) != "0":
        raise ValueError(ticker_payload.get("msg") or "okx_ticker_request_failed")
    rows = ticker_payload.get("data", [])
    if not rows:
        raise ValueError("okx_ticker_not_found")
    ticker = rows[0]
    funding_rate_bps: float | None = None
    next_funding_time: str | None = None
    funding_payload: dict[str, Any] | None = None
    if definition.supports_funding:
        funding_response = await client.get("https://www.okx.com/api/v5/public/funding-rate", params={"instId": normalized})
        funding_response.raise_for_status()
        funding_payload = funding_response.json()
        if str(funding_payload.get("code")) != "0":
            raise ValueError(funding_payload.get("msg") or "okx_funding_request_failed")
        funding_rows = funding_payload.get("data", [])
        if funding_rows:
            funding_rate_bps = _to_float(funding_rows[0].get("fundingRate")) * 10_000
            next_funding_time = _ms_to_iso(funding_rows[0].get("nextFundingTime"))
    return _build_quote(
        provider=provider,
        definition=definition,
        symbol=normalized,
        bid=_to_float(ticker.get("bidPx")),
        ask=_to_float(ticker.get("askPx")),
        last=_to_float(ticker.get("last")),
        funding_rate_bps_8h=funding_rate_bps,
        next_funding_time=next_funding_time,
        source_timestamp=_ms_to_iso(ticker.get("ts")),
        raw={"ticker": ticker_payload, "funding": funding_payload or {}},
    )


async def _fetch_bybit_quote(
    provider: str,
    definition: MarketQuoteSourceDefinition,
    symbol: str,
    client: httpx.AsyncClient,
    category: str,
) -> MarketQuote:
    normalized = symbol.strip().upper().replace("-", "").replace("SWAP", "")
    response = await client.get("https://api.bybit.com/v5/market/tickers", params={"category": category, "symbol": normalized})
    response.raise_for_status()
    payload = response.json()
    if str(payload.get("retCode")) != "0":
        raise ValueError(payload.get("retMsg") or "bybit_ticker_request_failed")
    rows = (payload.get("result") or {}).get("list", [])
    if not rows:
        raise ValueError("bybit_ticker_not_found")
    ticker = rows[0]
    funding_rate_bps = _to_float(ticker.get("fundingRate")) * 10_000 if definition.supports_funding else None
    return _build_quote(
        provider=provider,
        definition=definition,
        symbol=normalized,
        bid=_to_float(ticker.get("bid1Price")),
        ask=_to_float(ticker.get("ask1Price")),
        last=_to_float(ticker.get("lastPrice")),
        funding_rate_bps_8h=funding_rate_bps,
        next_funding_time=_ms_to_iso(ticker.get("nextFundingTime")),
        source_timestamp=_ms_to_iso((payload.get("time") or ticker.get("ts"))),
        raw={"ticker": payload},
    )


def _build_quote(
    provider: str,
    definition: MarketQuoteSourceDefinition,
    symbol: str,
    bid: float,
    ask: float,
    last: float,
    funding_rate_bps_8h: float | None,
    next_funding_time: str | None,
    source_timestamp: str | None,
    raw: dict[str, Any],
) -> MarketQuote:
    if bid <= 0 and ask <= 0 and last <= 0:
        raise ValueError("quote_has_no_positive_price")
    mid = (bid + ask) / 2 if bid > 0 and ask > 0 else last
    spread_bps = ((ask - bid) / mid * 10_000) if bid > 0 and ask > 0 and mid > 0 else 0.0
    return MarketQuote(
        provider=provider,
        exchange=definition.exchange,
        market_type=definition.market_type,
        symbol=symbol,
        bid=bid,
        ask=ask,
        last=last,
        mid=mid,
        spread_bps=spread_bps,
        funding_rate_bps_8h=funding_rate_bps_8h,
        next_funding_time=next_funding_time,
        source_timestamp=source_timestamp,
        raw=raw,
    )


def _to_float(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _ms_to_iso(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _normalize_okx_symbol(provider: str, symbol: str) -> str:
    normalized = symbol.strip().upper()
    if "-" in normalized:
        if provider == "okx_swap" and normalized.count("-") == 1:
            return f"{normalized}-SWAP"
        return normalized
    for quote in ("USDT", "USDC", "USD", "BTC", "ETH"):
        if normalized.endswith(quote) and len(normalized) > len(quote):
            inst_id = f"{normalized[: -len(quote)]}-{quote}"
            return f"{inst_id}-SWAP" if provider == "okx_swap" else inst_id
    return normalized
