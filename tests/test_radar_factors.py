import asyncio
import copy
import math
import statistics
import time
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from test_radar import evidence
from crypto_strategy_lab.radar import DEFAULT_ASSETS, RadarSettings, evaluate
from crypto_strategy_lab.radar_factors import calculate_technicals, closed_daily_bars
from crypto_strategy_lab.radar_sources import DataUnavailable, PublicData


def candles(prices, quote=None, end_day=None):
    end_day = end_day or int(time.time() // 86400)
    start_day = end_day - len(prices)
    quote = quote if quote is not None else [1_000_000] * len(prices)
    return [[(start_day+i)*86400000, p, p*1.02, p*.98, p, q/p,
             (start_day+i+1)*86400000-1, q, 100, q/p*.55, q*.55, 0]
            for i, (p, q) in enumerate(zip(prices, quote))]


class FactorMathTests(unittest.TestCase):
    def setUp(self):
        self.now = 20120*86400+3600
        self.raw = candles([100+i for i in range(120)], end_day=20120)

    def parse(self, raw=None):
        return closed_daily_bars(self.raw if raw is None else raw, self.now)

    def test_unclosed_extreme_candle_is_ignored(self):
        current = candles([1e20], end_day=20121)[0]
        self.assertEqual(self.parse(self.raw + [current]), self.parse())

    def test_missing_duplicate_and_stale_sessions_rejected(self):
        for rows in (self.raw[:40]+self.raw[41:], self.raw+[self.raw[-1]], self.raw[:30]):
            with self.assertRaisesRegex(ValueError, "insufficient_or_gapped"):
                self.parse(rows)
        with self.assertRaisesRegex(ValueError, "stale_daily"):
            closed_daily_bars(self.raw, self.now+86400)

    def test_nonfinite_negative_and_inconsistent_candles_rejected(self):
        for index, value in ((4, "NaN"), (4, "inf"), (1, 0), (7, -1), (10, 2_000_000), (2, 1), (3, 1000)):
            rows = copy.deepcopy(self.raw)
            rows[-1][index] = value
            with self.subTest(index=index, value=value), self.assertRaises(ValueError):
                self.parse(rows)
        rows = copy.deepcopy(self.raw)
        rows[-1][6] -= 1
        with self.assertRaisesRegex(ValueError, "interval"):
            self.parse(rows)

    def test_returns_alignment_and_nonoverlapping_volume_ratio(self):
        raw = candles([100+i for i in range(120)], [1_000_000]*113+[2_000_000]*7, 20120)
        bars = self.parse(raw)
        result = calculate_technicals(bars, bars, bars)
        self.assertAlmostEqual(result["return_30d_pct"], (219/189-1)*100)
        self.assertAlmostEqual(result["return_90d_pct"], (219/129-1)*100)
        self.assertAlmostEqual(result["rs_btc_7d_pct"], 0)
        self.assertAlmostEqual(result["rs_eth_7d_pct"], 0)
        self.assertAlmostEqual(result["volume_ratio_7_30"], 2)
        self.assertAlmostEqual(result["taker_buy_share_7d_pct"], 55)
        self.assertAlmostEqual(result["beta_btc_30d"], 1)
        self.assertAlmostEqual(result["correlation_btc_30d"], 1)
        self.assertEqual(result["as_of"], raw[-1][6]/1000)

    def test_volatility_atr_amihud_and_zscore_formulas(self):
        bars = self.parse()
        result = calculate_technicals(bars, bars, bars)
        returns = [math.log((i+1)/i) for i in range(189, 219)]
        self.assertAlmostEqual(result["volatility_30d_pct"], statistics.stdev(returns)*math.sqrt(365)*100)
        self.assertAlmostEqual(result["amihud_30d_bps"], statistics.mean(1/i for i in range(189, 219))*10000)
        self.assertAlmostEqual(result["atr14_pct"], statistics.mean(i*.04 for i in range(206, 220))/219*100)
        self.assertAlmostEqual(result["price_z20"], (219-209.5)/statistics.stdev(range(200, 220)))
        self.assertEqual(result["max_drawdown_90d_pct"], 0)

    def test_drawdown_is_path_dependent_not_only_last_close(self):
        prices = [100]*40+[200]*40+[100]*20+[300]*20
        bars = self.parse(candles(prices, end_day=20120))
        self.assertEqual(calculate_technicals(bars, bars, bars)["max_drawdown_90d_pct"], -50)

    def test_beta_uses_matching_daily_log_returns(self):
        benchmark = [100*math.exp(.01*i+.02*math.sin(i)) for i in range(120)]
        asset = [p*p for p in benchmark]
        b = self.parse(candles(benchmark, end_day=20120))
        a = self.parse(candles(asset, end_day=20120))
        result = calculate_technicals(a, b, b)
        self.assertAlmostEqual(result["beta_btc_30d"], 2)
        self.assertAlmostEqual(result["correlation_btc_30d"], 1)
        self.assertAlmostEqual(result["rs_btc_30d_pct"], (benchmark[-1]/benchmark[-31]-1)*100)

    def test_constant_market_and_zero_volume_are_not_fabricated(self):
        bars = self.parse(candles([100]*120, [0]*120, 20120))
        result = calculate_technicals(bars, bars, bars)
        for key in ("beta_btc_30d", "correlation_btc_30d", "price_z20", "volume_ratio_7_30", "taker_buy_share_7d_pct", "amihud_30d_bps"):
            self.assertIsNone(result[key], key)
        self.assertEqual(result["volatility_30d_pct"], 0)
        self.assertEqual(result["zero_volume_days_30"], 30)

    def test_short_history_leaves_long_window_metrics_missing(self):
        bars = self.parse(self.raw[-31:])
        result = calculate_technicals(bars, bars, bars)
        for key in ("return_90d_pct", "max_drawdown_90d_pct", "sma20_sma60_pct", "volume_ratio_7_30", "btc_sma60_distance_pct"):
            self.assertIsNone(result[key], key)
        self.assertIsNotNone(result["return_30d_pct"])

    def test_benchmark_date_mismatch_rejected(self):
        bars = self.parse()
        with self.assertRaisesRegex(ValueError, "unaligned_benchmark_close"):
            calculate_technicals(bars, bars[:-1], bars)
        with self.assertRaisesRegex(ValueError, "unaligned_benchmark_history"):
            calculate_technicals(bars, bars[-30:], bars)

    def test_overflow_never_reaches_saved_json(self):
        asset, benchmark = self.parse(), self.parse()
        asset[-91]["close"] = 1e-308
        with self.assertRaisesRegex(ValueError, "nonfinite_factor_calculation"):
            calculate_technicals(asset, benchmark, benchmark)


class FactorScoringTests(unittest.TestCase):
    def setUp(self):
        self.now = time.time()
        self.sources, self.baseline = evidence(self.now)
        self.settings = RadarSettings().model_dump()

    def evaluate(self):
        return evaluate(self.sources, self.settings, self.baseline, self.now)

    def test_group_caps_and_score_accounting(self):
        result = self.evaluate()
        self.assertEqual(result["factor_model"]["points"], 20)
        self.assertEqual(result["factor_model"]["coverage"], 100)
        self.assertEqual(len(result["factor_model"]["metrics"]), 24)
        self.assertEqual(sum(f["points"] for f in result["factors"]), result["score"])
        for group in result["factor_model"]["groups"]:
            self.assertLessEqual(group["points"], 5)
            self.assertTrue(all(check["condition"] for check in group["checks"]))

    def test_missing_stale_or_error_technicals_never_score_as_zero_risk(self):
        for change in ({"data": {}}, {"status": "error"}, {"observed_at": self.now-3601}):
            original = copy.deepcopy(self.sources["technicals"])
            self.sources["technicals"].update(change)
            result = self.evaluate()
            self.assertEqual(result["status"], "incomplete")
            self.assertIn("factor_data_incomplete", result["blockers"])
            self.assertEqual(result["factor_model"]["points"], 2)
            self.assertEqual(result["score"], 82)
            self.sources["technicals"] = original

    def test_all_disabled_restores_base_weights_not_core_safety_bypass(self):
        self.settings["enabled_factor_groups"] = []
        self.sources.pop("technicals")
        result = self.evaluate()
        self.assertEqual(result["score"], 100)
        self.assertEqual(result["coverage"], 100)
        self.assertEqual(result["factor_model"]["max_points"], 0)
        self.sources["governance"]["status"] = "error"
        self.assertEqual(self.evaluate()["status"], "incomplete")

    def test_single_group_missing_does_not_redistribute_points(self):
        self.settings["enabled_factor_groups"] = ["flow"]
        self.sources["technicals"]["data"].pop("taker_buy_share_7d_pct")
        result = self.evaluate()
        self.assertEqual(result["factor_model"]["coverage"], 60)
        self.assertEqual(result["score"], 98)
        self.assertEqual(result["status"], "incomplete")

    def test_nonfinite_factor_and_zero_spot_volume_marked_missing(self):
        self.sources["technicals"]["data"]["amihud_30d_bps"] = float("nan")
        self.sources["spot"]["data"]["quote_volume_24h"] = 0
        values = {x["key"]: x["value"] for x in self.evaluate()["factor_model"]["metrics"]}
        self.assertIsNone(values["amihud_30d_bps"])
        self.assertIsNone(values["oi_to_spot_volume"])

    def test_extreme_risks_and_personal_thresholds(self):
        self.sources["technicals"]["data"].update(volatility_30d_pct=151, beta_btc_30d=3,
            volume_ratio_7_30=6, return_7d_pct=-1, amihud_30d_bps=110, btc_return_7d_pct=-11)
        self.sources["derivatives"]["data"]["basis_bps"] = -101
        result = self.evaluate()
        codes = {r["code"] for r in result["risks"]}
        self.assertTrue({"high_volatility", "high_market_beta", "volume_price_anomaly", "illiquidity", "market_stress", "derivative_crowding"} <= codes)
        self.assertEqual(result["status"], "avoid")
        self.settings["max_volatility_pct"] = 200
        self.settings["max_basis_bps"] = 150
        codes = {r["code"] for r in self.evaluate()["risks"]}
        self.assertNotIn("high_volatility", codes)
        self.assertNotIn("derivative_crowding", codes)

    def test_disabled_groups_do_not_add_optional_risk(self):
        self.settings["enabled_factor_groups"] = ["momentum"]
        self.sources["technicals"]["data"].update(volatility_30d_pct=900, beta_btc_30d=8, btc_return_7d_pct=-30)
        self.assertEqual(self.evaluate()["risk_score"], 0)


class FactorSourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_spot_and_factor_collection_share_asset_candles_and_cache_timestamp(self):
        requests = []
        rows = candles([100+i for i in range(120)])

        def respond(request):
            requests.append(request)
            if request.url.path.endswith("klines"):
                self.assertEqual(request.url.params["limit"], "120")
                return httpx.Response(200, json=rows)
            if request.url.path.endswith("depth"):
                return httpx.Response(200, json={"bids": [["218.9", "1000"]], "asks": [["219.1", "1000"]]})
            return httpx.Response(200, json={"symbol": "ARPAUSDT", "closeTime": time.time()*1000,
                                           "lastPrice": "219", "quoteVolume": "1234567"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            data = PublicData(client)
            with patch("crypto_strategy_lab.radar_sources.asyncio.sleep", new=AsyncMock()):
                spot, technical = await asyncio.gather(data.spot(DEFAULT_ASSETS[1]), data.technicals(DEFAULT_ASSETS[1]))
                again = await data.technicals(DEFAULT_ASSETS[1])
            self.assertEqual(len(requests), 5)
            self.assertEqual(spot["data"]["quote_volume_24h"], 1234567)
            self.assertEqual(len(spot["data"]["daily_closes"]), 30)
            self.assertEqual(technical["observed_at"], again["observed_at"])
            self.assertEqual(technical["data"]["as_of"], rows[-1][6]/1000)

    async def test_derivative_basis_uses_mark_and_index_not_last_trade(self):
        data = PublicData()
        now = int(time.time()*1000)
        data.fetch = AsyncMock(side_effect=[
            {"symbol": "ARPAUSDT", "markPrice": "101", "indexPrice": "100", "lastFundingRate": ".0001", "time": now, "nextFundingTime": now+10000},
            {"symbol": "ARPAUSDT", "openInterest": "200", "time": now},
            [{"timestamp": now-i*3600000, "sumOpenInterest": "200"} for i in range(25)], []])
        try:
            result = await data.derivatives(DEFAULT_ASSETS[1])
            self.assertAlmostEqual(result["data"]["basis_bps"], 100)
            self.assertEqual(result["data"]["open_interest_usdt"], 20200)
        finally:
            await data.close()

    async def test_invalid_benchmark_propagates_missing_not_substitute(self):
        data = PublicData()
        rows = candles([100+i for i in range(120)])
        data.fetch = AsyncMock(side_effect=[rows, rows[:-2], rows])
        try:
            with self.assertRaises(DataUnavailable):
                await data.technicals(DEFAULT_ASSETS[1])
        finally:
            await data.close()
