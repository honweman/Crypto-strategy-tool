import asyncio
import csv
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx

from crypto_strategy_lab.market_data import (
    MarketDataRequest,
    fetch_ohlcv,
    list_market_data_sources,
    write_bars_csv,
)


class MarketDataTests(unittest.TestCase):
    def test_catalog_contains_common_sources(self) -> None:
        ids = {source["id"] for source in list_market_data_sources()}
        self.assertIn("binance_spot", ids)
        self.assertIn("binance_usdm", ids)
        self.assertIn("okx", ids)
        self.assertIn("bybit_spot", ids)
        self.assertIn("coinbase", ids)
        self.assertIn("kucoin_spot", ids)
        self.assertIn("kraken", ids)
        self.assertIn("bitget_usdt_futures", ids)
        self.assertIn("coingecko_ohlc", ids)

    def test_binance_kline_parse_and_write_csv(self) -> None:
        async def run() -> None:
            def handler(request: httpx.Request) -> httpx.Response:
                self.assertEqual(request.url.path, "/api/v3/klines")
                return httpx.Response(
                    200,
                    json=[
                        [
                            1767225600000,
                            "100.0",
                            "110.0",
                            "95.0",
                            "105.0",
                            "123.45",
                            1767229199999,
                        ]
                    ],
                )

            transport = httpx.MockTransport(handler)
            async with httpx.AsyncClient(transport=transport) as client:
                bars = await fetch_ohlcv(
                    MarketDataRequest(provider="binance_spot", symbol="BTCUSDT", interval="1h", limit=1),
                    client=client,
                )

            self.assertEqual(len(bars), 1)
            self.assertEqual(bars[0].symbol, "BTCUSDT")
            self.assertEqual(bars[0].open, 100.0)
            self.assertEqual(bars[0].high, 110.0)
            self.assertEqual(bars[0].low, 95.0)
            self.assertEqual(bars[0].close, 105.0)
            self.assertEqual(bars[0].volume, 123.45)

            with tempfile.TemporaryDirectory() as tmp:
                output = Path(tmp) / "bars.csv"
                write_bars_csv(output, bars)
                with output.open() as file:
                    rows = list(csv.DictReader(file))
                self.assertEqual(rows[0]["symbol"], "BTCUSDT")
                self.assertEqual(float(rows[0]["close"]), 105.0)

        asyncio.run(run())

    def test_rejects_unknown_provider(self) -> None:
        async def run() -> None:
            with self.assertRaises(ValueError):
                await fetch_ohlcv(MarketDataRequest(provider="unknown", symbol="BTCUSDT"))

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
