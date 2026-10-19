import asyncio
import copy
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from crypto_strategy_lab.radar import DEFAULT_ASSETS, SOURCE_TTLS, RadarAsset, RadarSettings, evaluate
from crypto_strategy_lab.radar_sources import DataUnavailable, PublicData
from crypto_strategy_lab.radar_store import RadarStore
from crypto_strategy_lab.web import create_app


def evidence(now=None):
    now = now or time.time()
    sources = {key: {"status": "ok", "observed_at": now, "data": {}} for key in SOURCE_TTLS}
    sources["market"]["data"] = {"identity_verified": True, "price": 1, "market_cap": 20_000_000,
        "volume_24h": 5_000_000, "fdv": 25_000_000, "float_ratio": .8, "change_24h_pct": 2, "change_7d_pct": -15}
    sources["spot"]["data"] = {"price": 1, "ask": 1.001, "bid": .999, "drawdown_30d_pct": -35,
                               "rsi14": 35, "depth_1pct_usdt": 30000, "spread_bps": 20, "quote_volume_24h": 1_000_000}
    sources["derivatives"]["data"] = {"funding_bps": 1, "oi_change_24h_pct": 3, "basis_bps": 10, "open_interest_usdt": 2_000_000}
    sources["technicals"]["data"] = {"as_of": now-3600, "rs_btc_7d_pct": 2, "rs_btc_30d_pct": 3,
        "rs_eth_7d_pct": 1, "sma20_slope_5d_pct": 1, "volume_ratio_7_30": 1.5, "taker_buy_share_7d_pct": 55,
        "amihud_30d_bps": 50, "volatility_30d_pct": 100, "atr14_pct": 5, "max_drawdown_90d_pct": -40,
        "btc_sma60_distance_pct": 5, "btc_return_7d_pct": 2, "beta_btc_30d": 1.2}
    sources["governance"]["data"] = {"control_verified": True, "owner_kind": "multisig", "safes": [{"threshold": 2}], "events": []}
    sources["holders"]["data"] = {"holders": [{"address": "0x%040x" % i, "balance": "102"} for i in range(1,16)], "top10_share": .2}
    sources["news"]["data"] = {"items": []}
    sources["transfers"]["data"] = {"items": []}
    baseline = copy.deepcopy(sources["holders"])
    baseline["observed_at"] = now - 86400
    for holder in baseline["data"]["holders"]:
        holder["balance"] = "100"
    return sources, baseline


class RulesTests(unittest.TestCase):
    def setUp(self):
        self.now = time.time()
        self.sources, self.baseline = evidence(self.now)
        self.settings = RadarSettings().model_dump()

    def evaluate(self):
        return evaluate(self.sources, self.settings, self.baseline, self.now)

    def test_complete_observation_is_candidate_not_probability(self):
        output = self.evaluate()
        self.assertEqual(output["status"], "candidate")
        self.assertEqual(output["score"], 100)
        self.assertAlmostEqual(output["holder_delta"]["pct"], 2)
        self.assertTrue(output["limitations"])

    def test_each_critical_source_missing_stale_partial_future_blocks(self):
        for key in ("market", "spot", "holders", "transfers", "governance"):
            original = copy.deepcopy(self.sources[key])
            for update in ({"status": "error"}, {"status": "partial"}, {"observed_at": self.now-SOURCE_TTLS[key]-1}, {"observed_at": self.now+120}):
                self.sources[key] = {**original, **update}
                with self.subTest(key=key, update=update):
                    self.assertEqual(self.evaluate()["status"], "incomplete")
            self.sources[key] = original

    def test_new_holders_are_not_treated_as_accumulation(self):
        for i, holder in enumerate(self.sources["holders"]["data"]["holders"]):
            holder["address"] = "0x%040x" % (i+100)
        self.assertEqual(self.evaluate()["status"], "warming")
        self.assertIsNone(self.evaluate()["holder_delta"])

    def test_baseline_requires_elapsed_time(self):
        self.baseline["observed_at"] = self.now-3599
        self.assertEqual(self.evaluate()["status"], "warming")

    def test_multisig_is_not_positive_signal_and_executed_change_blocks(self):
        before = self.evaluate()["score"]
        self.sources["governance"]["data"]["safes"][0]["threshold"] = 5
        self.assertEqual(self.evaluate()["score"], before)
        self.sources["governance"]["data"]["events"] = [{"timestamp": self.now-600, "event": "ChangedThreshold"}]
        self.assertEqual(self.evaluate()["status"], "avoid")

    def test_exchange_transfer_risk_only_recent_labeled_not_buy_signal(self):
        row = {"value_usd": 200000, "to_kind": "exchange", "timestamp": self.now-100, "tx_hash": "tx"}
        self.sources["transfers"]["data"]["items"] = [row]
        self.assertEqual(self.evaluate()["risk_score"], 20)
        row["timestamp"] = self.now-90000
        self.assertEqual(self.evaluate()["risk_score"], 0)
        row.update(timestamp=self.now-100, to_kind="unknown")
        self.assertEqual(self.evaluate()["risk_score"], 0)

    def test_news_risk_requires_review(self):
        self.sources["news"]["data"]["items"] = [{"risk_terms": ["exploit"], "title": "reported exploit", "published_at": self.now-1}]
        self.assertEqual(self.evaluate()["status"], "avoid")

    def test_price_divergence_and_paused_token_block(self):
        self.sources["spot"]["data"]["price"] = 2
        self.assertIn("price_divergence", self.evaluate()["blockers"])
        self.sources["governance"]["data"]["paused"] = True
        self.assertEqual(self.evaluate()["risk_score"], 100)

    def test_asset_identity_and_nan_rejected(self):
        self.sources["market"]["data"]["identity_verified"] = False
        self.assertEqual(self.evaluate()["status"], "incomplete")
        with self.assertRaises(ValueError):
            RadarSettings(min_market_cap=float("nan"))
        with self.assertRaises(ValueError):
            RadarAsset(**{**DEFAULT_ASSETS[1], "exchange_symbol": "BTCUSDT"})


class StoreApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app(Path(self.tmp.name)/"test.db", "test-secret")
        self.db = self.app.state.db
        self.store = self.app.state.radar.store
        self.admin = self.db.list_users()[0]
        self.store.ensure_user(self.admin["id"])
        self.viewer = self.db.create_user("viewer@example.com", "test-password", "viewer")
        self.other = self.db.create_user("other@example.com", "test-password", "analyst")
        self.store.ensure_user(self.other["id"])
        self.client = TestClient(self.app)
        self.client.post("/api/auth/login",json={"email":"admin@example.com","password":"change-me-now"})

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def payload(self, job, now=None):
        sources, baseline = evidence(now)
        return {"asset": job["asset"], "sources":sources, "holder_baseline":baseline,
                "evaluation": evaluate(sources,job["settings"],baseline,now)}

    def test_permissions_and_cross_user_isolation(self):
        foreign = self.store.watches(self.other["id"])[0]
        for method, url in [("get",f"/api/radar/watches/{foreign['id']}/history"), ("delete",f"/api/radar/watches/{foreign['id']}"), ("post",f"/api/radar/refresh?watch_id={foreign['id']}")]:
            self.assertEqual(getattr(self.client,method)(url).status_code,404)
        self.assertEqual(self.client.put(f"/api/radar/watches/{foreign['id']}",json=foreign["asset"]).status_code,404)
        self.client.post("/api/auth/logout")
        self.assertEqual(self.client.get("/api/radar").status_code,401)
        self.client.post("/api/auth/login",json={"email":self.viewer["email"],"password":"test-password"})
        self.assertEqual(self.client.get("/api/radar").status_code,200)
        self.assertEqual(self.client.put("/api/radar/settings",json={}).status_code,403)
        self.assertEqual(self.client.post("/api/radar/watches",json=DEFAULT_ASSETS[0]).status_code,403)

    def test_configuration_validation_and_duplicate(self):
        self.assertEqual(self.client.put("/api/radar/settings",json={"min_market_cap":100,"max_market_cap":99}).status_code,422)
        self.assertEqual(self.client.post("/api/radar/watches",json=DEFAULT_ASSETS[0]).status_code,409)
        self.assertEqual(self.client.post("/api/radar/refresh").status_code,503)

    def test_factor_config_is_user_scoped_and_validated(self):
        config = RadarSettings(enabled_factor_groups=["momentum"], max_atr_pct=7).model_dump()
        response = self.client.put("/api/radar/settings", json=config)
        self.assertEqual(response.status_code, 200)
        overview = self.client.get("/api/radar").json()
        self.assertEqual(overview["settings"]["enabled_factor_groups"], ["momentum"])
        self.assertEqual(len(overview["factor_groups"]), 4)
        self.assertTrue(overview["deferred_factors"])
        self.assertEqual(len(self.store.settings(self.other["id"])["enabled_factor_groups"]), 4)
        for bad in (["flow", "flow"], ["unknown"]):
            self.assertEqual(self.client.put("/api/radar/settings", json={"enabled_factor_groups": bad}).status_code, 422)

    def test_legacy_settings_default_and_signal_parameters_are_frozen(self):
        with self.db.connect() as c:
            c.execute("UPDATE radar_settings SET config=? WHERE user_id=?", (json.dumps({"enabled": True}), self.admin["id"]))
        job = self.store.claim([self.admin["id"]])
        self.assertEqual(len(job["settings"]["enabled_factor_groups"]), 4)
        self.store.finish(job, self.payload(job))
        before = self.store.signals(self.admin["id"])[0]
        self.store.update_settings(self.admin["id"], RadarSettings(enabled_factor_groups=[]).model_dump())
        after = self.store.signals(self.admin["id"])[0]
        self.assertEqual(after["evidence"], before["evidence"])
        self.assertEqual(after["evidence"]["evaluation"]["rule_version"], "altcoin-observer-v2")
        self.assertEqual(len(after["evidence"]["settings"]["enabled_factor_groups"]), 4)

    def test_exclusive_claim_and_revised_config_discards_inflight_result(self):
        job = self.store.claim([self.admin["id"]])
        second = self.store.claim([self.admin["id"]])
        self.assertNotEqual(job["id"],second["id"])
        self.store.update_settings(self.admin["id"],RadarSettings(min_score=70).model_dump())
        self.assertEqual(self.store.finish(job,self.payload(job)),(False,None))
        self.store.release(job)
        self.assertEqual(self.store.watch(job["id"],self.admin["id"])["next_due"],0)

    def test_lease_expiration_and_disabled_user_settings(self):
        now = time.time()
        jobs = [self.store.claim([self.admin["id"]],now) for _ in range(4)]
        self.assertIsNone(self.store.claim([self.admin["id"]],now+100))
        self.assertIsNotNone(self.store.claim([self.admin["id"]],now+241))
        self.store.update_settings(self.admin["id"],RadarSettings(enabled=False).model_dump())
        self.assertIsNone(self.store.claim([self.admin["id"]],now+500))

    def test_signal_evidence_deduplicated_and_user_scoped(self):
        job = self.store.claim([self.admin["id"]])
        payload = self.payload(job)
        saved, alert = self.store.finish(job,payload)
        self.assertTrue(saved)
        self.assertEqual(alert["status"],"candidate")
        with self.db.connect() as c:
            c.execute("UPDATE radar_watches SET next_due=0,last_started=0 WHERE id=?",(job["id"],))
        retry = self.store.claim([self.admin["id"]])
        self.assertEqual(retry["id"],job["id"])
        self.assertIsNone(self.store.finish(retry,payload)[1])
        self.assertEqual(len(self.store.signals(self.admin["id"])),1)
        self.assertEqual(self.store.signals(self.other["id"]),[])

    def test_stale_reads_and_edited_identity_never_show_candidate(self):
        job = self.store.claim([self.admin["id"]])
        self.store.finish(job,self.payload(job,time.time()-10000))
        watch = self.client.get("/api/radar").json()["watches"][0]
        self.assertEqual(watch["snapshot"]["evaluation"]["status"],"incomplete")
        self.store.save_watch(self.admin["id"],{**watch["asset"],"name":"Updated name"},watch["id"])
        self.assertIn("watch_configuration_changed",self.client.get("/api/radar").json()["watches"][0]["snapshot"]["evaluation"]["blockers"])

    def test_forward_returns_use_costs_and_no_future_backfill(self):
        job = self.store.claim([self.admin["id"]])
        self.store.finish(job,self.payload(job))
        signal = self.store.signals(self.admin["id"])[0]
        now = signal["created_at"]+3600+10
        payload = self.payload(job,now)
        payload["sources"]["spot"]["data"]["bid"] = 1.1
        with self.db.connect() as c:
            self.store._mark_outcomes(c,job["id"],payload,now)
        outcome = self.store.signals(self.admin["id"])[0]["outcomes"]["1h"]
        expected = (1.1*.999*.999/(1.001*1.001*1.001)-1)*100
        self.assertAlmostEqual(outcome["net_return_pct"],expected)
        future = signal["created_at"]+86400+3600
        with self.db.connect() as c:
            self.store._mark_outcomes(c,job["id"],self.payload(job,future),future)
        self.assertEqual(self.store.signals(self.admin["id"])[0]["outcomes"]["24h"]["status"],"missed")

    def test_slow_polling_cannot_label_next_day_quote_as_one_hour_return(self):
        job=self.store.claim([self.admin["id"]])
        job["settings"]["interval_seconds"]=86400
        self.store.finish(job,self.payload(job))
        signal=self.store.signals(self.admin["id"])[0]
        now=signal["created_at"]+86410
        with self.db.connect() as c:
            self.store._mark_outcomes(c,job["id"],self.payload(job,now),now)
        self.assertEqual(self.store.signals(self.admin["id"])[0]["outcomes"]["1h"]["status"],"missed")

    def test_snapshot_retention_is_bounded(self):
        job=self.store.claim([self.admin["id"]])
        with self.db.connect() as c:
            c.executemany("INSERT INTO radar_snapshots(watch_id,created_at,payload) VALUES (?,?,?)",
                          [(job["id"],time.time(),"{}")]*3001)
        self.store.finish(job,self.payload(job))
        with self.db.connect() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM radar_snapshots WHERE watch_id=?",(job["id"],)).fetchone()[0],3000)


class SourcesTests(unittest.IsolatedAsyncioTestCase):
    async def test_cache_keeps_original_observation_time(self):
        seen=[]
        def respond(req):
            seen.append(req)
            return httpx.Response(200,json={"value":1})
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            data=PublicData(client)
            url="https://api.coingecko.com/test"
            await data.fetch(url)
            original=data.fetched_at(url)
            await data.fetch(url)
            self.assertEqual(data.fetched_at(url),original)
            self.assertEqual(len(seen),1)
            self.assertEqual(len(data.locks),64)

    async def test_rate_limit_respected_across_endpoints(self):
        seen=[]
        def respond(req):
            seen.append(req)
            return httpx.Response(429,headers={"Retry-After":"900"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            data=PublicData(client)
            with self.assertRaisesRegex(DataUnavailable,"http_429"):
                await data.fetch("https://example.org/a")
            with self.assertRaisesRegex(DataUnavailable,"provider_cooldown"):
                await data.fetch("https://example.org/b")
            self.assertEqual(len(seen),1)
            self.assertGreater(data.blocked_host["example.org"],time.time()+890)

    async def test_errors_preserve_old_timestamp_but_disable_score(self):
        data=PublicData()
        sources,_=evidence()
        for method in ("market","spot","holders","transfers","derivatives","governance","news","technicals"):
            setattr(data,method,AsyncMock(side_effect=DataUnavailable("http_429")))
        try:
            actual=await data.collect(DEFAULT_ASSETS[0],{"sources":sources})
            self.assertEqual(actual["market"]["observed_at"],sources["market"]["observed_at"])
            self.assertEqual(evaluate(actual,RadarSettings().model_dump())["status"],"incomplete")
        finally:
            await data.close()

    async def test_wrong_contract_identity_rejected(self):
        data=PublicData()
        data.fetch=AsyncMock(return_value={"id":"arpa","symbol":"arpa","platforms":{"ethereum":"0xwrong"}})
        try:
            with self.assertRaisesRegex(DataUnavailable,"asset_identity_mismatch"):
                await data.market(RadarAsset(**DEFAULT_ASSETS[1]).model_dump())
        finally:
            await data.close()

    async def test_crypto_hashing_backend_available(self):
        from eth_utils import keccak
        self.assertEqual(keccak(text="owner()")[:4].hex(),"8da5cb5b")

    async def test_partial_governance_exposes_current_safe_but_blocks_candidate(self):
        from eth_abi import encode
        data=PublicData()
        data.fetch=AsyncMock(return_value={"result":{"number":"0x123", "timestamp":hex(int(time.time()))}})
        safe="0x"+"1"*40
        data.rpc=AsyncMock(side_effect=[
            {"owner":"0x"+encode(["address"],[safe]).hex(),"paused":"0x"+encode(["bool"],[False]).hex()},
            {"owners":"0x"+encode(["address[]"],[["0x"+str(i)*40 for i in (2,3,4)]]).hex(),
             "threshold":"0x"+encode(["uint256"],[2]).hex()}])
        data.events=AsyncMock(side_effect=DataUnavailable("http_403"))
        try:
            actual=await data.governance(RadarAsset(**DEFAULT_ASSETS[1]).model_dump())
            self.assertEqual(actual["status"],"partial")
            self.assertEqual(actual["data"]["safes"][0]["threshold"],2)
            sources,baseline=evidence()
            sources["governance"]=actual
            self.assertEqual(evaluate(sources,RadarSettings().model_dump(),baseline)["status"],"incomplete")
        finally:
            await data.close()

    async def test_gapped_oi_history_is_rejected(self):
        data=PublicData()
        history=[{"timestamp":i*3600000,"sumOpenInterest":"100"} for i in range(25)]
        history[12]["timestamp"]=history[11]["timestamp"]
        data.fetch=AsyncMock(side_effect=[{"symbol":"ARPAUSDT"},{"symbol":"ARPAUSDT"},history,[]])
        try:
            with self.assertRaisesRegex(DataUnavailable,"insufficient_oi_history"):
                await data.derivatives(RadarAsset(**DEFAULT_ASSETS[1]).model_dump())
        finally:
            await data.close()


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.app=create_app(Path(self.tmp.name)/"test.db","test-secret")
        self.service=self.app.state.radar
        self.admin=self.app.state.db.list_users()[0]
        self.service.store.ensure_user(self.admin["id"])
        self.service.collector=Mock(collect=AsyncMock(return_value=evidence()[0]),close=AsyncMock(),
                                    discover=AsyncMock(return_value={"sources":{}, "rows":[], "unresolved":[]}))

    async def asyncTearDown(self):
        await self.service.close()
        self.tmp.cleanup()

    async def test_background_runs_without_browser_and_stops_cleanly(self):
        await self.service.start()
        for _ in range(60):
            if self.service.store.latest(1):
                break
            await asyncio.sleep(.02)
        self.assertIsNotNone(self.service.store.latest(1))
        self.assertTrue(self.service.status()["running"])
        await self.service.close()
        self.assertFalse(self.service.status()["running"])

    async def test_revoked_permission_discards_result(self):
        job=self.service.store.claim([self.admin["id"]])
        self.app.state.db.update_user(self.admin["id"],permission_revokes=["radar:manage_self"])
        await self.service.scan(job)
        self.assertIsNone(self.service.store.latest(job["id"]))
        self.assertFalse(self.service.store.watch(job["id"],self.admin["id"])["updating"])

    async def test_failure_releases_lease_for_retry(self):
        self.service.collector.collect.side_effect=asyncio.TimeoutError()
        job=self.service.store.claim([self.admin["id"]])
        await self.service.scan(job)
        watch=self.service.store.watch(job["id"],self.admin["id"])
        self.assertEqual(watch["last_error"],"scan_timeout")
        self.assertFalse(watch["updating"])
        self.assertGreater(watch["next_due"],time.time())

    async def test_candidate_notification_is_emitted_once(self):
        job=self.service.store.claim([self.admin["id"]])
        sources,baseline=evidence()
        self.service.collector.collect.return_value=sources
        self.service.notify=Mock()
        old={"sources":{"holders":baseline}}
        with self.app.state.db.connect() as c:
            c.execute("INSERT INTO radar_snapshots(watch_id,created_at,payload) VALUES (?,?,?)",
                      (job["id"],baseline["observed_at"],json.dumps(old)))
        await self.service.scan(job)
        self.service.notify.assert_called_once()
        with self.app.state.db.connect() as c:
            c.execute("UPDATE radar_watches SET next_due=0,last_started=0 WHERE id=?",(job["id"],))
        retry=self.service.store.claim([self.admin["id"]])
        await self.service.scan(retry)
        self.service.notify.assert_called_once()


if __name__ == "__main__":
    unittest.main()
