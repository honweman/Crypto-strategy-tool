import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from crypto_strategy_lab.strategy_tools import build_pmm_quote_plan, calculate_spot_perp_arbitrage, calculate_xemm_opportunity


class StrategyToolsTests(unittest.TestCase):
    def test_pmm_quote_plan_uses_spreads_and_rules(self) -> None:
        plan = build_pmm_quote_plan(
            symbol="BTCUSDT",
            mid_price=100.0,
            base_balance=2.0,
            quote_balance=10_000,
            order_notional=100,
            bid_spread_bps=10,
            ask_spread_bps=10,
            order_levels=2,
            order_level_spread_bps=5,
            price_tick=0.1,
            quantity_step=0.001,
            min_notional=10,
        )
        self.assertEqual(len(plan["orders"]), 4)
        ready_orders = [order for order in plan["orders"] if order["status"] == "ready"]
        self.assertEqual(len(ready_orders), 4)
        self.assertLess(ready_orders[0]["price"], 100.0)
        self.assertGreater(ready_orders[1]["price"], 100.0)

    def test_pmm_quote_plan_blocks_spot_sell_without_base_inventory(self) -> None:
        plan = build_pmm_quote_plan(
            symbol="BTCUSDT",
            mid_price=100.0,
            base_balance=0.0,
            quote_balance=1_000.0,
            order_notional=100.0,
            bid_spread_bps=10,
            ask_spread_bps=10,
        )
        sell_orders = [order for order in plan["orders"] if order["side"] == "sell"]
        self.assertEqual(sell_orders[0]["status"], "blocked")
        self.assertEqual(sell_orders[0]["reason"], "insufficient_base_balance")

    def test_xemm_opportunity_calculates_net_profit(self) -> None:
        result = calculate_xemm_opportunity(
            symbol="BTCUSDT",
            maker_exchange="binance",
            taker_exchange="okx",
            maker_bid=99.0,
            maker_ask=101.0,
            taker_bid=100.0,
            taker_ask=100.5,
            min_profitability_bps=5,
            maker_fee_bps=1,
            taker_fee_bps=1,
            slippage_buffer_bps=1,
        )
        self.assertTrue(result["approved"])
        self.assertTrue(any(item["profitable"] for item in result["opportunities"]))

    def test_spot_perp_arbitrage_estimates_margin_and_edge(self) -> None:
        result = calculate_spot_perp_arbitrage(
            symbol="ETHUSDT",
            spot_exchange="binance",
            perp_exchange="binance_usdm",
            spot_price=100.0,
            perp_price=101.0,
            notional=1_000,
            funding_bps_per_8h=5,
            holding_hours=24,
            spot_fee_bps=1,
            perp_fee_bps=1,
            slippage_bps=1,
            leverage=5,
            min_net_edge_bps=1,
        )
        self.assertTrue(result["approved"])
        self.assertEqual(len(result["legs"]), 2)
        self.assertAlmostEqual(result["initial_margin_required"], 200.0)


if __name__ == "__main__":
    unittest.main()
