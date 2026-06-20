import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from crypto_strategy_lab.broker import SimulatedBroker
from crypto_strategy_lab.data import CsvBarDataSource, CsvExternalEventSource, MergedEventFeed
from crypto_strategy_lab.engine import BacktestConfig, BacktestEngine
from crypto_strategy_lab.models import Bar, Order, Side, parse_timestamp
from crypto_strategy_lab.portfolio import Portfolio
from crypto_strategy_lab.risk import RiskConfig, RiskManager
from crypto_strategy_lab.strategy import FixedWeightConfig, FixedWeightStrategy, StatisticalEventConfig, StatisticalEventStrategy


ROOT = Path(__file__).resolve().parents[1]


class EngineTests(unittest.TestCase):
    def test_feed_merges_by_timestamp(self) -> None:
        feed = list(
            MergedEventFeed(
                [
                    CsvBarDataSource(ROOT / "data/sample_bars.csv"),
                    CsvExternalEventSource(ROOT / "data/sample_events.csv"),
                ]
            )
        )
        timestamps = [event.timestamp for event in feed]
        self.assertEqual(timestamps, sorted(timestamps))

    def test_backtest_runs_and_produces_fills(self) -> None:
        strategy = StatisticalEventStrategy(
            StatisticalEventConfig(lookback=5, entry_z=-1.0, exit_z=0.1, event_ttl_bars=3)
        )
        engine = BacktestEngine(
            strategy=strategy,
            broker=SimulatedBroker(fee_bps=5, slippage_bps=1),
            risk_manager=RiskManager(RiskConfig(max_order_notional=5_000)),
            config=BacktestConfig(initial_cash=10_000),
        )
        result = engine.run(
            MergedEventFeed(
                [
                    CsvBarDataSource(ROOT / "data/sample_bars.csv"),
                    CsvExternalEventSource(ROOT / "data/sample_events.csv"),
                ]
            )
        )
        self.assertGreater(len(result.equity_curve), 0)
        self.assertGreater(len(result.fills), 0)
        self.assertGreater(result.metrics.ending_equity, 0)

    def test_risk_prevents_long_only_oversell(self) -> None:
        manager = RiskManager(RiskConfig(long_only=True))
        portfolio = Portfolio(cash=1_000)
        order = Order(
            timestamp=parse_timestamp("2026-01-01T00:00:00+00:00"),
            symbol="BTCUSDT",
            side=Side.SELL,
            quantity=1.0,
        )
        decision = manager.approve(order, portfolio, {"BTCUSDT": 100.0})
        self.assertFalse(decision.approved)
        self.assertEqual(decision.reason, "long_only_no_inventory")

    def test_stop_loss_creates_forced_exit(self) -> None:
        result = self._run_fixed_weight(
            [
                self._bar("2026-01-01T00:00:00+00:00", close=100, high=101, low=99),
                self._bar("2026-01-02T00:00:00+00:00", close=90, high=101, low=85),
            ],
            BacktestConfig(initial_cash=10_000, stop_loss_pct=0.10),
        )

        self.assertEqual(result.metrics.forced_exit_count, 1)
        self.assertIn("stop_loss", result.fills[-1].reason)

    def test_perpetual_funding_is_charged(self) -> None:
        result = self._run_fixed_weight(
            [
                self._bar("2026-01-01T00:00:00+00:00", close=100, high=101, low=99),
                self._bar("2026-01-02T00:00:00+00:00", close=100, high=101, low=99),
            ],
            BacktestConfig(initial_cash=10_000, market_type="perpetual-long-only", funding_bps_per_bar=10),
        )

        self.assertGreater(result.metrics.total_funding, 0)
        self.assertLess(result.metrics.ending_equity, 10_000)

    def test_perpetual_liquidation_is_recorded(self) -> None:
        result = self._run_fixed_weight(
            [
                self._bar("2026-01-01T00:00:00+00:00", close=100, high=101, low=99),
                self._bar("2026-01-02T00:00:00+00:00", close=76, high=101, low=75),
            ],
            BacktestConfig(
                initial_cash=10_000,
                market_type="perpetual-long-only",
                leverage=5,
                maintenance_margin_ratio=0.005,
            ),
            target_weight=1.0,
        )

        self.assertEqual(result.metrics.liquidation_count, 1)
        self.assertIn("liquidation", result.fills[-1].reason)

    def test_execution_delay_uses_later_bar_price_and_timestamp(self) -> None:
        result = self._run_fixed_weight(
            [
                self._bar("2026-01-01T00:00:00+00:00", close=100, high=101, low=99),
                self._bar("2026-01-02T00:00:00+00:00", close=110, high=111, low=109),
            ],
            BacktestConfig(initial_cash=10_000, execution_delay_bars=1),
        )

        self.assertEqual(len(result.fills), 1)
        self.assertEqual(result.fills[0].price, 110)
        self.assertEqual(result.fills[0].timestamp, parse_timestamp("2026-01-02T00:00:00+00:00"))

    def _run_fixed_weight(
        self,
        bars: list[Bar],
        config: BacktestConfig,
        target_weight: float = 0.5,
    ):
        engine = BacktestEngine(
            strategy=FixedWeightStrategy(FixedWeightConfig(target_weight=target_weight)),
            broker=SimulatedBroker(fee_bps=0, slippage_bps=0),
            risk_manager=RiskManager(
                RiskConfig(max_order_notional=100_000, max_symbol_weight=1.0, max_gross_exposure=1.0)
            ),
            config=config,
        )
        return engine.run(bars)

    def _bar(self, timestamp: str, close: float, high: float, low: float) -> Bar:
        return Bar(
            timestamp=parse_timestamp(timestamp),
            symbol="BTCUSDT",
            open=close,
            high=high,
            low=low,
            close=close,
            volume=1_000,
        )


if __name__ == "__main__":
    unittest.main()
