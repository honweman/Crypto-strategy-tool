import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from crypto_strategy_lab.strategy import (
    BollingerRsiStrategy,
    ChannelBreakoutStrategy,
    EventScoreStrategy,
    FixedWeightStrategy,
    FundingRateCarryStrategy,
    MarketMakingStrategy,
    MovingAverageCrossoverStrategy,
    RsiMeanReversionStrategy,
    StatisticalEventStrategy,
)
from crypto_strategy_lab.strategy_registry import create_strategy, list_strategy_definitions, validate_parameters


class StrategyRegistryTests(unittest.TestCase):
    def test_catalog_exposes_builtin_strategies(self) -> None:
        ids = {item["id"] for item in list_strategy_definitions()}
        self.assertIn("statistical_event", ids)
        self.assertIn("event_score", ids)
        self.assertIn("fixed_weight", ids)
        self.assertIn("ma_crossover", ids)
        self.assertIn("channel_breakout", ids)
        self.assertIn("rsi_mean_reversion", ids)
        self.assertIn("bollinger_rsi", ids)
        self.assertIn("funding_rate_carry", ids)
        self.assertIn("market_making", ids)

    def test_strategy_factory(self) -> None:
        self.assertIsInstance(create_strategy("statistical_event", {"lookback": 5}), StatisticalEventStrategy)
        self.assertIsInstance(create_strategy("event_score", {"target_weight": 0.1}), EventScoreStrategy)
        self.assertIsInstance(create_strategy("fixed_weight", {"target_weight": 0.1}), FixedWeightStrategy)
        self.assertIsInstance(create_strategy("ma_crossover", {"fast_window": 4, "slow_window": 8}), MovingAverageCrossoverStrategy)
        self.assertIsInstance(create_strategy("channel_breakout", {"breakout_window": 8, "exit_window": 4}), ChannelBreakoutStrategy)
        self.assertIsInstance(create_strategy("rsi_mean_reversion", {"rsi_window": 5}), RsiMeanReversionStrategy)
        self.assertIsInstance(create_strategy("bollinger_rsi", {"lookback": 6, "rsi_window": 5}), BollingerRsiStrategy)
        self.assertIsInstance(create_strategy("funding_rate_carry", {"entry_funding_bps": 1.0}), FundingRateCarryStrategy)
        self.assertIsInstance(create_strategy("market_making", {"lookback": 5}), MarketMakingStrategy)

    def test_parameter_validation(self) -> None:
        params = validate_parameters("fixed_weight", {"target_weight": "0.25"})
        self.assertEqual(params["target_weight"], 0.25)
        with self.assertRaises(ValueError):
            validate_parameters("fixed_weight", {"target_weight": 1.25})
        with self.assertRaises(ValueError):
            validate_parameters("ma_crossover", {"fast_window": 10, "slow_window": 5})
        with self.assertRaises(ValueError):
            validate_parameters("rsi_mean_reversion", {"oversold_rsi": 60, "exit_rsi": 50})
        with self.assertRaises(ValueError):
            validate_parameters("funding_rate_carry", {"entry_funding_bps": 1, "exit_funding_bps": 1})


if __name__ == "__main__":
    unittest.main()
