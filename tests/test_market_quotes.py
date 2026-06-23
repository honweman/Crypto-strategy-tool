import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx

from crypto_strategy_lab.market_quotes import fetch_market_quote, list_market_quote_sources


class MarketQuoteTests(unittest.TestCase):
    def test_catalog_contains_quote_sources(self) -> None:
        ids = {source["id"] for source in list_market_quote_sources()}
        self.assertIn("binance_spot", ids)
        self.assertIn("binance_usdm", ids)
        self.assertIn("okx_swap", ids)
        self.assertIn("bybit_linear", ids)

    def test_binance_spot_quote_parses_bid_ask_and_mid(self) -> None:
        async def run() -> None:
            def handler(request: httpx.Request) -> httpx.Response:
                self.assertEqual(request.url.params.get("symbol"), "BTCUSDT")
                if request.url.path.endswith("/ticker/bookTicker"):
                    return httpx.Response(200, json={"symbol": "BTCUSDT", "bidPrice": "99.0", "askPrice": "101.0"})
                if request.url.path.endswith("/ticker/price"):
                    return httpx.Response(200, json={"symbol": "BTCUSDT", "price": "100.0"})
                return httpx.Response(404)

            transport = httpx.MockTransport(handler)
            async with httpx.AsyncClient(transport=transport) as client:
                quote = await fetch_market_quote("binance_spot", "BTC-USDT", client=client)

            self.assertEqual(quote.symbol, "BTCUSDT")
            self.assertEqual(quote.exchange, "binance")
            self.assertEqual(quote.market_type, "spot")
            self.assertEqual(quote.bid, 99.0)
            self.assertEqual(quote.ask, 101.0)
            self.assertEqual(quote.mid, 100.0)
            self.assertEqual(quote.spread_bps, 200.0)

        asyncio.run(run())

    def test_bybit_linear_quote_parses_funding(self) -> None:
        async def run() -> None:
            def handler(request: httpx.Request) -> httpx.Response:
                self.assertEqual(request.url.path, "/v5/market/tickers")
                self.assertEqual(request.url.params.get("category"), "linear")
                return httpx.Response(
                    200,
                    json={
                        "retCode": 0,
                        "time": 1767225600000,
                        "result": {
                            "list": [
                                {
                                    "symbol": "ETHUSDT",
                                    "bid1Price": "100.0",
                                    "ask1Price": "100.2",
                                    "lastPrice": "100.1",
                                    "fundingRate": "0.00015",
                                    "nextFundingTime": "1767254400000",
                                }
                            ]
                        },
                    },
                )

            transport = httpx.MockTransport(handler)
            async with httpx.AsyncClient(transport=transport) as client:
                quote = await fetch_market_quote("bybit_linear", "ETHUSDT", client=client)

            self.assertEqual(quote.symbol, "ETHUSDT")
            self.assertEqual(quote.market_type, "perpetual-long-only")
            self.assertAlmostEqual(quote.funding_rate_bps_8h or 0.0, 1.5)
            self.assertIsNotNone(quote.next_funding_time)
            self.assertIsNotNone(quote.source_timestamp)

        asyncio.run(run())

    def test_okx_swap_normalizes_plain_symbol(self) -> None:
        async def run() -> None:
            def handler(request: httpx.Request) -> httpx.Response:
                self.assertEqual(request.url.params.get("instId"), "BTC-USDT-SWAP")
                if request.url.path.endswith("/market/ticker"):
                    return httpx.Response(
                        200,
                        json={
                            "code": "0",
                            "data": [
                                {
                                    "instId": "BTC-USDT-SWAP",
                                    "bidPx": "99.5",
                                    "askPx": "100.5",
                                    "last": "100.0",
                                    "ts": "1767225600000",
                                }
                            ],
                        },
                    )
                if request.url.path.endswith("/public/funding-rate"):
                    return httpx.Response(200, json={"code": "0", "data": [{"fundingRate": "0.0002", "nextFundingTime": "1767254400000"}]})
                return httpx.Response(404)

            transport = httpx.MockTransport(handler)
            async with httpx.AsyncClient(transport=transport) as client:
                quote = await fetch_market_quote("okx_swap", "BTCUSDT", client=client)

            self.assertEqual(quote.symbol, "BTC-USDT-SWAP")
            self.assertAlmostEqual(quote.funding_rate_bps_8h or 0.0, 2.0)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
