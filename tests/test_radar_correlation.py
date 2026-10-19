import copy
import math
import statistics
import time
import unittest

from test_radar import evidence
from test_radar_factors import candles
from crypto_strategy_lab.radar import RadarSettings, evaluate
from crypto_strategy_lab.radar_correlation import correlation_report, complete_link, merge_checks, spearman, stable_pair
from crypto_strategy_lab.radar_factors import calculate_technicals, closed_daily_bars, technical_bundle


def pair(a, b, rho=.95, n=180, early=None, late=None):
    return {"left":a, "right":b, "rho":rho, "early_rho":rho if early is None else early,
            "late_rho":rho if late is None else late, "n":n, "status":"eligible"}


class CorrelationTests(unittest.TestCase):
    def test_spearman_handles_ties_constant_and_negative(self):
        self.assertAlmostEqual(spearman([1,1,2],[1,2,2]), .5)
        self.assertEqual(spearman([1,2,3],[3,2,1]), -1)
        self.assertIsNone(spearman([1,1,1],[1,2,3]))
        self.assertIsNone(spearman([1e-16,2e-16,3e-16],[1,2,3]))
        self.assertIsNone(spearman([1],[1,2]))

    def test_dated_samples_are_prior_contiguous_and_same_group(self):
        rows=[{"at":d*86400,"a":d,"b":d*d,"c":-d} for d in range(200)]
        report=correlation_report(rows,{"a":"momentum","b":"momentum","c":"volatility"},190*86400)
        self.assertEqual(report["days"],180)
        self.assertEqual(report["end"],189*86400)
        self.assertEqual(len(report["pairs"]),1)
        self.assertEqual(report["pairs"][0]["n"],180)
        self.assertTrue(stable_pair(report["pairs"][0],.85))
        rows[100]["b"]=None
        report=correlation_report(rows,{"a":"x","b":"x"},190*86400)
        self.assertEqual(report["pairs"][0]["status"],"gapped")
        with self.assertRaisesRegex(ValueError,"duplicate"):
            correlation_report(rows+[rows[120]],{"a":"x","b":"x"},190*86400)

    def test_small_or_unstable_samples_do_not_merge(self):
        for p in (pair("a","b",n=89),pair("a","b",early=.5),pair("a","b",late=-.95),
                  {**pair("a","b"),"status":"constant"}):
            self.assertFalse(stable_pair(p,.85))
            self.assertEqual(len(complete_link(["a","b"],[p],.85)),2)

    def test_complete_link_prevents_transitive_chain(self):
        pairs=[pair("a","b",.99),pair("b","c",.98),pair("a","c",.2)]
        self.assertEqual(complete_link(["c","b","a"],pairs,.85),[["a","b"],["c"]])

    def test_opposite_scoring_directions_do_not_merge_positive_raw_correlation(self):
        self.assertEqual(len(complete_link(["a","b"],[pair("a","b")],.85,{"a":1,"b":-1})),2)
        self.assertEqual(complete_link(["a","b"],[pair("a","b",-.95)],.85,{"a":1,"b":-1}),[["a","b"]])

    def test_structural_and_empirical_dedupe_preserve_max_member_not_sum(self):
        checks=[{"key":k,"points":w,"max_points":w} for k,w in (("a",2),("b",1),("c",1))]
        groups=merge_checks(checks,[],.85,[("a","b")],{"a":1,"b":1,"c":1})
        self.assertEqual(sum(g["points"] for g in groups),3)
        self.assertEqual(groups[0]["reason"],"structural")
        pairs=[pair(a,b) for a,b in (("a","b"),("b","c"),("a","c"))]
        groups=merge_checks(checks,pairs,.85,[("a","b")],{"a":1,"b":1,"c":1})
        self.assertEqual(len(groups),1)
        self.assertEqual(groups[0]["points"],2)
        self.assertEqual(groups[0]["removed_budget"],2)
        checks[0]["points"]=0
        groups=merge_checks(checks,pairs,.85,[("a","b")],{"a":1,"b":1,"c":1})
        self.assertEqual(groups[0]["points"],1)

    def test_structural_cluster_cannot_chain_to_unverified_member(self):
        checks=[{"key":k,"points":1,"max_points":1} for k in "abc"]
        groups=merge_checks(checks,[pair("a","c")],.85,[("a","b")],dict.fromkeys("abc",1))
        self.assertEqual(len(groups),2)


class AdditionalFactorTests(unittest.TestCase):
    def setUp(self):
        self.now=time.time()
        self.benchmark=[100*math.exp(.001*i+.02*math.sin(i)) for i in range(300)]
        self.asset=[math.exp(.0005*i)*b**2 for i,b in enumerate(self.benchmark)]

    def parse(self,prices,quote=None):
        return closed_daily_bars(candles(prices,quote),self.now)

    def test_residual_momentum_uses_training_window_before_signal_window(self):
        b=self.parse(self.benchmark)
        original=calculate_technicals(self.parse(self.asset),b,b)
        self.assertAlmostEqual(original["residual_momentum_7d_pct"],0,places=8)
        self.assertAlmostEqual(original["residual_volatility_60d_pct"],0,places=8)
        changed=list(self.asset)
        for i in range(1,8):
            changed[-8+i]*=math.exp(.01*i)
        result=calculate_technicals(self.parse(changed),b,b)
        self.assertAlmostEqual(result["residual_momentum_7d_pct"],math.expm1(.07)*100,places=8)

    def test_tail_downside_volume_and_liquidity_formulas(self):
        prices=[100*math.exp(.01*math.sin(i)) for i in range(120)]
        quote=[1e6]*113+[2e6]*6+[8e6]
        a=self.parse(prices,quote)
        b=self.parse(self.benchmark[-120:])
        output=calculate_technicals(a,b,b)
        returns=[math.log(y/x) for x,y in zip(prices[-31:-1],prices[-30:])]
        tail=sorted((y/x-1)*100 for x,y in zip(prices[-91:-1],prices[-90:]))[:5]
        impact=[abs(y/x-1)/q for x,y,q in zip(prices[-38:-1],prices[-37:],quote[-37:])]
        self.assertAlmostEqual(output["tail_loss_90d_pct"],-statistics.mean(tail))
        self.assertAlmostEqual(output["downside_deviation_30d_pct"],math.sqrt(sum(min(x,0)**2 for x in returns)/30*365)*100)
        self.assertAlmostEqual(output["volume_concentration_7d_pct"],40)
        self.assertAlmostEqual(output["amihud_ratio_7_30"],statistics.mean(impact[-7:])/statistics.mean(impact[:30]))
        self.assertAlmostEqual(output["taker_buy_change_7d_pp"],0)
        self.assertAlmostEqual(output["close_location_7d"],0)

    def test_missing_history_flat_benchmark_and_zero_volume(self):
        a=self.parse([100]*31,[0]*31)
        output=calculate_technicals(a,a,a)
        for key in ("residual_momentum_7d_pct","residual_volatility_60d_pct","tail_loss_90d_pct",
                    "volume_concentration_7d_pct","amihud_ratio_7_30","taker_buy_change_7d_pp","close_location_7d"):
            self.assertIsNone(output[key],key)

    def test_latest_signal_candle_cannot_change_correlation_fit(self):
        a,b=self.parse(self.asset),self.parse(self.benchmark)
        before=technical_bundle(a,b,b)
        changed=copy.deepcopy(a)
        changed[-1]["close"]*=1.01
        after=technical_bundle(changed,b,b)
        self.assertEqual(before["correlation"],after["correlation"])
        self.assertEqual(before["correlation"]["days"],180)
        self.assertLess(before["correlation"]["end"],before["as_of"])

    def test_dedupe_never_removes_core_safety_or_increases_score(self):
        sources,baseline=evidence(self.now)
        cfg=RadarSettings().model_dump()
        before=evaluate(sources,cfg,baseline,self.now)
        t=sources["technicals"]["data"]
        t["correlation"]={"version":"daily-spearman-v1","cutoff":t["as_of"],"end":t["as_of"]-86400,
                          "pairs":[pair("btc_sma60_distance_pct","btc_return_7d_pct")]}
        after=evaluate(sources,cfg,baseline,self.now)
        self.assertEqual(after["score"],before["score"]-1)
        self.assertEqual(after["factor_model"]["coverage"],100)
        sources["governance"]["data"]["paused"]=True
        self.assertIn("token_paused",evaluate(sources,cfg,baseline,self.now)["blockers"])
        self.assertEqual(evaluate(sources,cfg,baseline,self.now)["status"],"avoid")

    def test_new_risk_metrics_share_existing_risk_budget(self):
        sources,baseline=evidence(self.now)
        cfg=RadarSettings().model_dump()
        sources["technicals"]["data"].update(tail_loss_90d_pct=20,amihud_ratio_7_30=4,volatility_30d_pct=200,amihud_30d_bps=200)
        risks=evaluate(sources,cfg,baseline,self.now)["risks"]
        self.assertEqual(sum(r["points"] for r in risks if r["code"]=="high_volatility"),15)
        self.assertEqual(sum(r["points"] for r in risks if r["code"]=="illiquidity"),10)

    def test_thresholds_are_validated(self):
        for config in ({"factor_correlation_threshold":.7},{"factor_correlation_threshold":float("nan")},
                       {"max_tail_loss_pct":0},{"max_amihud_ratio":.5}):
            with self.assertRaises(ValueError):
                RadarSettings(**config)
