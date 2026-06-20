from __future__ import annotations

import csv
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx

from .models import Bar, parse_timestamp


@dataclass(frozen=True)
class MarketDataSourceDefinition:
    id: str
    name: str
    description: str
    symbol_hint: str
    intervals: list[str]
    max_limit: int
    requires_api_key: bool = False
    notes: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MarketDataRequest:
    provider: str
    symbol: str
    interval: str = "1h"
    start: str | None = None
    end: str | None = None
    limit: int = 200


COMMON_INTERVALS = ["1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w"]

DATA_SOURCE_DEFINITIONS: dict[str, MarketDataSourceDefinition] = {
    "binance_spot": MarketDataSourceDefinition(
        id="binance_spot",
        name="Binance Spot",
        description="Binance 现货公开 K 线。",
        symbol_hint="BTCUSDT",
        intervals=COMMON_INTERVALS,
        max_limit=1000,
        notes="公开 market data，无需 API key。",
    ),
    "binance_usdm": MarketDataSourceDefinition(
        id="binance_usdm",
        name="Binance USD-M Futures",
        description="Binance U 本位合约公开 K 线。",
        symbol_hint="BTCUSDT",
        intervals=COMMON_INTERVALS,
        max_limit=1000,
        notes="合约行情源，不代表已启用真实交易。",
    ),
    "okx": MarketDataSourceDefinition(
        id="okx",
        name="OKX",
        description="OKX 现货、Swap、合约 K 线。",
        symbol_hint="BTC-USDT",
        intervals=COMMON_INTERVALS,
        max_limit=300,
        notes="Swap 示例：BTC-USDT-SWAP。",
    ),
    "bybit_spot": MarketDataSourceDefinition(
        id="bybit_spot",
        name="Bybit Spot",
        description="Bybit 现货公开 K 线。",
        symbol_hint="BTCUSDT",
        intervals=COMMON_INTERVALS,
        max_limit=1000,
    ),
    "bybit_linear": MarketDataSourceDefinition(
        id="bybit_linear",
        name="Bybit Linear",
        description="Bybit USDT/USDC 线性合约 K 线。",
        symbol_hint="BTCUSDT",
        intervals=COMMON_INTERVALS,
        max_limit=1000,
    ),
    "coinbase": MarketDataSourceDefinition(
        id="coinbase",
        name="Coinbase Exchange",
        description="Coinbase Exchange public candles。",
        symbol_hint="BTC-USDT",
        intervals=["1m", "5m", "15m", "1h", "6h", "1d"],
        max_limit=300,
    ),
    "kucoin_spot": MarketDataSourceDefinition(
        id="kucoin_spot",
        name="KuCoin Spot",
        description="KuCoin 现货公开 K 线。",
        symbol_hint="BTC-USDT",
        intervals=COMMON_INTERVALS,
        max_limit=1500,
    ),
    "kraken": MarketDataSourceDefinition(
        id="kraken",
        name="Kraken",
        description="Kraken public OHLC。",
        symbol_hint="XBTUSD",
        intervals=COMMON_INTERVALS,
        max_limit=720,
        notes="Kraken 每次最多返回约 720 根，symbol 使用 Kraken pair。",
    ),
    "bitget_usdt_futures": MarketDataSourceDefinition(
        id="bitget_usdt_futures",
        name="Bitget USDT Futures",
        description="Bitget USDT 合约历史 K 线。",
        symbol_hint="BTCUSDT",
        intervals=COMMON_INTERVALS,
        max_limit=200,
    ),
    "coingecko_ohlc": MarketDataSourceDefinition(
        id="coingecko_ohlc",
        name="CoinGecko OHLC",
        description="CoinGecko coin OHLC，适合补充非交易所价格。",
        symbol_hint="bitcoin",
        intervals=["1d"],
        max_limit=365,
        notes="CoinGecko 免费 OHLC 不含成交量，CSV volume 会写 0。",
    ),
}


def list_market_data_sources() -> list[dict[str, Any]]:
    return [definition.as_dict() for definition in DATA_SOURCE_DEFINITIONS.values()]


async def fetch_ohlcv(request: MarketDataRequest, client: httpx.AsyncClient | None = None) -> list[Bar]:
    if request.provider not in DATA_SOURCE_DEFINITIONS:
        raise ValueError(f"unknown data provider: {request.provider}")
    definition = DATA_SOURCE_DEFINITIONS[request.provider]
    if request.interval not in definition.intervals:
        raise ValueError(f"unsupported interval for {request.provider}: {request.interval}")

    close_client = client is None
    http_client = client or httpx.AsyncClient(timeout=20)
    try:
        bars = await _PROVIDER_FETCHERS[request.provider](request, definition, http_client)
    finally:
        if close_client:
            await http_client.aclose()

    return sorted(bars, key=lambda bar: bar.timestamp)


def write_bars_csv(path: str | Path, bars: list[Bar]) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=["timestamp", "symbol", "open", "high", "low", "close", "volume"])
        writer.writeheader()
        for bar in bars:
            writer.writerow(
                {
                    "timestamp": bar.timestamp.astimezone(timezone.utc).isoformat(),
                    "symbol": bar.symbol,
                    "open": bar.open,
                    "high": bar.high,
                    "low": bar.low,
                    "close": bar.close,
                    "volume": bar.volume,
                }
            )


def default_output_path(provider: str, symbol: str, interval: str) -> str:
    safe_symbol = re.sub(r"[^A-Za-z0-9_-]+", "_", symbol.strip().upper()).strip("_") or "SYMBOL"
    safe_interval = re.sub(r"[^A-Za-z0-9_-]+", "_", interval.strip()).strip("_") or "1h"
    return f"data/{provider}_{safe_symbol}_{safe_interval}.csv"


async def _fetch_binance(
    request: MarketDataRequest,
    definition: MarketDataSourceDefinition,
    client: httpx.AsyncClient,
    futures: bool = False,
) -> list[Bar]:
    url = "https://fapi.binance.com/fapi/v1/klines" if futures else "https://api.binance.com/api/v3/klines"
    params: dict[str, Any] = {
        "symbol": request.symbol.strip().upper(),
        "interval": _map_interval(request.interval, _BINANCE_INTERVALS),
        "limit": _limit(request.limit, definition.max_limit),
    }
    _add_ms_range(params, request.start, request.end, "startTime", "endTime")
    response = await client.get(url, params=params)
    response.raise_for_status()
    rows = response.json()
    return [
        Bar(
            timestamp=_dt_from_ms(item[0]),
            symbol=params["symbol"],
            open=float(item[1]),
            high=float(item[2]),
            low=float(item[3]),
            close=float(item[4]),
            volume=float(item[5]),
        )
        for item in rows
    ]


async def _fetch_okx(request: MarketDataRequest, definition: MarketDataSourceDefinition, client: httpx.AsyncClient) -> list[Bar]:
    params: dict[str, Any] = {
        "instId": request.symbol.strip().upper(),
        "bar": _map_interval(request.interval, _OKX_INTERVALS),
        "limit": _limit(request.limit, definition.max_limit),
    }
    _add_ms_range(params, request.start, request.end, "after", "before")
    response = await client.get("https://www.okx.com/api/v5/market/history-candles", params=params)
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") not in {"0", 0}:
        raise ValueError(payload.get("msg") or "okx_request_failed")
    return [
        Bar(
            timestamp=_dt_from_ms(item[0]),
            symbol=params["instId"],
            open=float(item[1]),
            high=float(item[2]),
            low=float(item[3]),
            close=float(item[4]),
            volume=float(item[5]),
        )
        for item in payload.get("data", [])
    ]


async def _fetch_bybit(
    request: MarketDataRequest,
    definition: MarketDataSourceDefinition,
    client: httpx.AsyncClient,
    category: str,
) -> list[Bar]:
    params: dict[str, Any] = {
        "category": category,
        "symbol": request.symbol.strip().upper(),
        "interval": _map_interval(request.interval, _BYBIT_INTERVALS),
        "limit": _limit(request.limit, definition.max_limit),
    }
    _add_ms_range(params, request.start, request.end, "start", "end")
    response = await client.get("https://api.bybit.com/v5/market/kline", params=params)
    response.raise_for_status()
    payload = response.json()
    if str(payload.get("retCode")) != "0":
        raise ValueError(payload.get("retMsg") or "bybit_request_failed")
    return [
        Bar(
            timestamp=_dt_from_ms(item[0]),
            symbol=params["symbol"],
            open=float(item[1]),
            high=float(item[2]),
            low=float(item[3]),
            close=float(item[4]),
            volume=float(item[5]),
        )
        for item in payload.get("result", {}).get("list", [])
    ]


async def _fetch_coinbase(request: MarketDataRequest, definition: MarketDataSourceDefinition, client: httpx.AsyncClient) -> list[Bar]:
    params: dict[str, Any] = {"granularity": _map_interval(request.interval, _COINBASE_INTERVALS)}
    _add_iso_range(params, request.start, request.end, "start", "end")
    symbol = request.symbol.strip().upper()
    response = await client.get(f"https://api.exchange.coinbase.com/products/{symbol}/candles", params=params)
    response.raise_for_status()
    rows = response.json()[: _limit(request.limit, definition.max_limit)]
    return [
        Bar(
            timestamp=datetime.fromtimestamp(int(item[0]), tz=timezone.utc),
            symbol=symbol,
            low=float(item[1]),
            high=float(item[2]),
            open=float(item[3]),
            close=float(item[4]),
            volume=float(item[5]) if len(item) > 5 else 0.0,
        )
        for item in rows
    ]


async def _fetch_kucoin(request: MarketDataRequest, definition: MarketDataSourceDefinition, client: httpx.AsyncClient) -> list[Bar]:
    params: dict[str, Any] = {
        "symbol": request.symbol.strip().upper(),
        "type": _map_interval(request.interval, _KUCOIN_INTERVALS),
    }
    _add_second_range(params, request.start, request.end, "startAt", "endAt")
    response = await client.get("https://api.kucoin.com/api/v1/market/candles", params=params)
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") != "200000":
        raise ValueError(payload.get("msg") or "kucoin_request_failed")
    rows = payload.get("data", [])[: _limit(request.limit, definition.max_limit)]
    return [
        Bar(
            timestamp=datetime.fromtimestamp(int(item[0]), tz=timezone.utc),
            symbol=params["symbol"],
            open=float(item[1]),
            close=float(item[2]),
            high=float(item[3]),
            low=float(item[4]),
            volume=float(item[5]),
        )
        for item in rows
    ]


async def _fetch_kraken(request: MarketDataRequest, definition: MarketDataSourceDefinition, client: httpx.AsyncClient) -> list[Bar]:
    params: dict[str, Any] = {
        "pair": request.symbol.strip().upper(),
        "interval": _map_interval(request.interval, _KRAKEN_INTERVALS),
    }
    if request.start:
        params["since"] = int(_timestamp(request.start).timestamp())
    response = await client.get("https://api.kraken.com/0/public/OHLC", params=params)
    response.raise_for_status()
    payload = response.json()
    if payload.get("error"):
        raise ValueError(";".join(payload["error"]))
    result = payload.get("result", {})
    pair_key = next((key for key in result if key != "last"), None)
    rows = result.get(pair_key, [])[: _limit(request.limit, definition.max_limit)]
    return [
        Bar(
            timestamp=datetime.fromtimestamp(float(item[0]), tz=timezone.utc),
            symbol=request.symbol.strip().upper(),
            open=float(item[1]),
            high=float(item[2]),
            low=float(item[3]),
            close=float(item[4]),
            volume=float(item[6]),
        )
        for item in rows
    ]


async def _fetch_bitget(request: MarketDataRequest, definition: MarketDataSourceDefinition, client: httpx.AsyncClient) -> list[Bar]:
    params: dict[str, Any] = {
        "symbol": request.symbol.strip().upper(),
        "productType": "usdt-futures",
        "granularity": _map_interval(request.interval, _BITGET_INTERVALS),
        "limit": _limit(request.limit, definition.max_limit),
    }
    _add_ms_range(params, request.start, request.end, "startTime", "endTime")
    response = await client.get("https://api.bitget.com/api/v2/mix/market/history-candles", params=params)
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") not in {"00000", 0, "0"}:
        raise ValueError(payload.get("msg") or "bitget_request_failed")
    return [
        Bar(
            timestamp=_dt_from_ms(item[0]),
            symbol=params["symbol"],
            open=float(item[1]),
            high=float(item[2]),
            low=float(item[3]),
            close=float(item[4]),
            volume=float(item[5]) if len(item) > 5 else 0.0,
        )
        for item in payload.get("data", [])
    ]


async def _fetch_coingecko(request: MarketDataRequest, definition: MarketDataSourceDefinition, client: httpx.AsyncClient) -> list[Bar]:
    coin_id = request.symbol.strip().lower()
    days = _limit(request.limit, definition.max_limit)
    params = {"vs_currency": "usd", "days": days}
    response = await client.get(f"https://api.coingecko.com/api/v3/coins/{coin_id}/ohlc", params=params)
    response.raise_for_status()
    rows = response.json()
    symbol = coin_id.upper().replace("-", "_")
    return [
        Bar(
            timestamp=_dt_from_ms(item[0]),
            symbol=symbol,
            open=float(item[1]),
            high=float(item[2]),
            low=float(item[3]),
            close=float(item[4]),
            volume=0.0,
        )
        for item in rows
    ]


_BINANCE_INTERVALS = {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "1h", "4h": "4h", "1d": "1d", "1w": "1w"}
_OKX_INTERVALS = {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "1H", "4h": "4H", "1d": "1D", "1w": "1W"}
_BYBIT_INTERVALS = {"1m": "1", "5m": "5", "15m": "15", "30m": "30", "1h": "60", "4h": "240", "1d": "D", "1w": "W"}
_COINBASE_INTERVALS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "6h": 21600, "1d": 86400}
_KUCOIN_INTERVALS = {"1m": "1min", "5m": "5min", "15m": "15min", "30m": "30min", "1h": "1hour", "4h": "4hour", "1d": "1day", "1w": "1week"}
_KRAKEN_INTERVALS = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440, "1w": 10080}
_BITGET_INTERVALS = {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "1H", "4h": "4H", "1d": "1D", "1w": "1W"}

_PROVIDER_FETCHERS: dict[str, Callable[[MarketDataRequest, MarketDataSourceDefinition, httpx.AsyncClient], Any]] = {
    "binance_spot": lambda request, definition, client: _fetch_binance(request, definition, client, futures=False),
    "binance_usdm": lambda request, definition, client: _fetch_binance(request, definition, client, futures=True),
    "okx": _fetch_okx,
    "bybit_spot": lambda request, definition, client: _fetch_bybit(request, definition, client, category="spot"),
    "bybit_linear": lambda request, definition, client: _fetch_bybit(request, definition, client, category="linear"),
    "coinbase": _fetch_coinbase,
    "kucoin_spot": _fetch_kucoin,
    "kraken": _fetch_kraken,
    "bitget_usdt_futures": _fetch_bitget,
    "coingecko_ohlc": _fetch_coingecko,
}


def _map_interval(interval: str, mapping: dict[str, Any]) -> Any:
    if interval not in mapping:
        raise ValueError(f"unsupported interval: {interval}")
    return mapping[interval]


def _limit(value: int, max_limit: int) -> int:
    return max(1, min(int(value), max_limit))


def _timestamp(value: str) -> datetime:
    return parse_timestamp(value)


def _dt_from_ms(value: str | int | float) -> datetime:
    return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)


def _add_ms_range(params: dict[str, Any], start: str | None, end: str | None, start_key: str, end_key: str) -> None:
    if start:
        params[start_key] = int(_timestamp(start).timestamp() * 1000)
    if end:
        params[end_key] = int(_timestamp(end).timestamp() * 1000)


def _add_second_range(params: dict[str, Any], start: str | None, end: str | None, start_key: str, end_key: str) -> None:
    if start:
        params[start_key] = int(_timestamp(start).timestamp())
    if end:
        params[end_key] = int(_timestamp(end).timestamp())


def _add_iso_range(params: dict[str, Any], start: str | None, end: str | None, start_key: str, end_key: str) -> None:
    if start:
        params[start_key] = _timestamp(start).astimezone(timezone.utc).isoformat()
    if end:
        params[end_key] = _timestamp(end).astimezone(timezone.utc).isoformat()
