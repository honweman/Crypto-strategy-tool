import asyncio
import sys
import unittest
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from crypto_strategy_lab.exchange_rules import fetch_exchange_symbol_rules, list_rule_sources


class ExchangeRulesTests(unittest.TestCase):
    def test_source_catalog(self) -> None:
        ids = {item["id"] for item in list_rule_sources()}
        self.assertIn("binance_spot", ids)
        self.assertIn("okx_swap", ids)
        self.assertIn("bybit_linear", ids)

    def test_parse_binance_exchange_info(self) -> None:
        async def run() -> None:
            def handler(request: httpx.Request) -> httpx.Response:
                return httpx.Response(
                    200,
                    json={
                        "symbols": [
                            {
                                "symbol": "BTCUSDT",
                                "status": "TRADING",
                                "filters": [
                                    {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                                    {"filterType": "LOT_SIZE", "minQty": "0.001", "stepSize": "0.001"},
                                    {"filterType": "MIN_NOTIONAL", "minNotional": "5"},
                                ],
                            }
                        ]
                    },
                )

            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                rules = await fetch_exchange_symbol_rules("binance_spot", symbol="BTCUSDT", client=client)
            self.assertEqual(len(rules), 1)
            self.assertEqual(rules[0].symbol, "BTCUSDT")
            self.assertEqual(rules[0].price_tick, 0.1)
            self.assertEqual(rules[0].quantity_step, 0.001)
            self.assertEqual(rules[0].min_notional, 5.0)

        asyncio.run(run())

    def test_parse_bybit_instruments_info(self) -> None:
        async def run() -> None:
            def handler(request: httpx.Request) -> httpx.Response:
                return httpx.Response(
                    200,
                    json={
                        "retCode": 0,
                        "result": {
                            "list": [
                                {
                                    "symbol": "ETHUSDT",
                                    "status": "Trading",
                                    "priceFilter": {"tickSize": "0.01"},
                                    "lotSizeFilter": {
                                        "minOrderQty": "0.01",
                                        "qtyStep": "0.01",
                                        "minNotionalValue": "5",
                                    },
                                    "leverageFilter": {"maxLeverage": "50"},
                                }
                            ],
                            "nextPageCursor": "",
                        },
                    },
                )

            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                rules = await fetch_exchange_symbol_rules("bybit_linear", symbol="ETHUSDT", client=client)
            self.assertEqual(len(rules), 1)
            self.assertEqual(rules[0].exchange, "bybit")
            self.assertEqual(rules[0].market_type, "perpetual-long-only")
            self.assertEqual(rules[0].max_leverage, 50.0)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
