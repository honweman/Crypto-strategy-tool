import asyncio
import copy
import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from fastapi.testclient import TestClient

from crypto_strategy_lab.radar_discovery import (DiscoveryCollector, DiscoverySettings, DiscoveryStore,
    binance_markets, discovery_view, map_assets, upbit_markets)
from crypto_strategy_lab.radar_sources import DataUnavailable
from crypto_strategy_lab.web import create_app


def futures(base="ARPA", **kwargs):
    return {"symbol":base+"USDT", "baseAsset":base, "status":"TRADING", "contractType":"PERPETUAL",
            "quoteAsset":"USDT", "marginAsset":"USDT", "underlyingType":"COIN", **kwargs}


def spot(market="KRW-ARPA", warning=False):
    return {"market":market, "english_name":"ARPA", "market_event":{"warning":warning,"caution":{"PRICE_FLUCTUATIONS":True}}}


def sample(now=None):
    now = now or time.time()
    return {"collected_at":now, "sources":{k:{"status":"ok", "observed_at":now} for k in ("binance","upbit","mapping","caps","catalog")},
            "rows":[{"symbol":"ARPA", "name":"ARPA", "binance_symbol":"ARPAUSDT", "coingecko_id":"arpa",
                     "mapping":"upbit_pair", "upbit_markets":upbit_markets([spot()])["ARPA"], "market_cap":20_000_000,
                     "fdv":20_000_000_000, "volume_24h":5_000_000, "cap_observed_at":now}], "unresolved":[]}


class UniverseRulesTests(unittest.TestCase):
    def test_only_active_crypto_usdt_perpetuals_included(self):
        rows = [futures(), futures("BTC", status="SETTLING"), futures("ETH", contractType="CURRENT_QUARTER"),
                futures("SOL", quoteAsset="USDC"), futures("XRP", marginAsset="USDC"),
                futures("XAU", underlyingType="COMMODITY"), futures("BAD", symbol="BTCUSDT")]
        self.assertEqual(binance_markets({"symbols":rows}), {"ARPA":"ARPAUSDT"})

    def test_upbit_quotes_warnings_and_unknown_are_preserved(self):
        rows = upbit_markets([spot(), spot("BTC-ARPA",True), {"market":"USDT-ARPA","market_warning":"NONE"},
                              {"market":"KRW-MISSING"}, {"market":"SGD-ARPA"}])
        self.assertEqual(len(rows["ARPA"]),3)
        self.assertEqual([m["warning"] for m in rows["ARPA"]], [False, True, False])
        self.assertIsNone(rows["MISSING"][0]["warning"])
        self.assertEqual(rows["ARPA"][0]["cautions"],["PRICE_FLUCTUATIONS"])

    def test_pair_mapping_resolves_duplicate_tickers_but_conflicts_do_not(self):
        catalog=[{"id":"arpa","symbol":"arpa","name":"ARPA"},{"id":"fake-arpa","symbol":"arpa","name":"Other"}]
        tickers=[{"base":"ARPA","target":"KRW","coin_id":"arpa","market":{"identifier":"upbit"}}]
        ub=upbit_markets([spot()])
        rows, missing=map_assets({"ARPA":"ARPAUSDT"},ub,catalog,tickers)
        self.assertEqual(rows[0]["coingecko_id"],"arpa")
        self.assertFalse(missing)
        tickers.append({**tickers[0],"coin_id":"fake-arpa"})
        rows, missing=map_assets({"ARPA":"ARPAUSDT"},ub,catalog,tickers)
        self.assertFalse(rows)
        self.assertEqual(missing[0]["reason"],"ambiguous_identity")

    def test_binance_ambiguous_codes_and_multipliers_are_not_guessed(self):
        catalog=[{"id":"a","symbol":"ABC"},{"id":"b","symbol":"ABC"},{"id":"pepe","symbol":"PEPE"}]
        rows, missing=map_assets({"ABC":"ABCUSDT","1000PEPE":"1000PEPEUSDT"},{},catalog,[])
        self.assertFalse(rows)
        self.assertEqual(len(missing),2)
        self.assertEqual({x["reason"] for x in missing},{"unmapped_identity","ambiguous_identity"})

    def test_exact_english_name_and_unique_code_are_labeled_not_contract_verified(self):
        rows,_=map_assets({"ARPA":"ARPAUSDT"},upbit_markets([spot()]),
                         [{"id":"arpa","symbol":"ARPA","name":"ARPA"},{"id":"b","symbol":"ARPA","name":"Fake"}],[])
        self.assertEqual(rows[0]["mapping"],"name_match")
        rows,_=map_assets({"ARPA":"ARPAUSDT"},{},[{"id":"arpa","symbol":"arpa"}],[])
        self.assertEqual(rows[0]["mapping"],"unique_symbol")

    def test_empty_listing_is_unknown_not_no_coins(self):
        for fn, value in ((binance_markets,{}),(binance_markets,{"symbols":[]}),(upbit_markets,[])):
            with self.assertRaises(DataUnavailable):
                fn(value)


class DiscoveryFiltersTests(unittest.TestCase):
    def setUp(self):
        self.now=time.time()
        self.data=sample(self.now)
        self.cfg=DiscoverySettings().model_dump()

    def view(self):
        return discovery_view(self.data,self.cfg,{},self.now)

    def test_filters_circulating_cap_not_fdv_or_price(self):
        self.assertEqual(self.view()["matched"],1)
        self.data["rows"][0]["market_cap"]=301_000_000
        self.assertEqual(self.view()["matched"],0)
        self.assertEqual(self.view()["excluded"]["outside_cap_range"],1)

    def test_inclusive_boundaries_and_invalid_settings(self):
        for cap in (self.cfg["min_market_cap"],self.cfg["max_market_cap"]):
            self.data["rows"][0]["market_cap"]=cap
            self.assertEqual(self.view()["matched"],1)
        for kwargs in ({"min_market_cap":10,"max_market_cap":9},{"max_market_cap":float("nan")},{"interval_seconds":1},{"mode":"invalid"}):
            with self.assertRaises(ValueError):
                DiscoverySettings(**kwargs)

    def test_all_four_listing_modes(self):
        self.data["rows"].extend([{**self.data["rows"][0],"symbol":"ONLYB","coingecko_id":"onlyb","upbit_markets":[]},
                                  {**self.data["rows"][0],"symbol":"ONLYU","coingecko_id":"onlyu","binance_symbol":None}])
        for mode, expected in (("intersection",1),("union",3),("binance",2),("upbit",2)):
            self.cfg["mode"]=mode
            self.assertEqual(self.view()["matched"],expected)

    def test_quote_specific_warning_filter(self):
        self.data["rows"][0]["upbit_markets"]=upbit_markets([spot(),spot("BTC-ARPA",True)])["ARPA"]
        self.assertEqual(self.view()["matched"],0)
        self.cfg["upbit_quote"]="KRW"
        self.assertEqual(self.view()["matched"],1)
        self.cfg["upbit_quote"]="USDT"
        self.assertEqual(self.view()["mapped_in_scope"],0)
        self.cfg.update(upbit_quote="all",exclude_upbit_warning=False)
        self.assertEqual(self.view()["matched"],1)

    def test_missing_negative_zero_stale_and_future_caps_do_not_match(self):
        for change in ({"market_cap":None},{"market_cap":0},{"market_cap":-1},{"market_cap":float("nan")},
                       {"cap_observed_at":self.now-3601},{"cap_observed_at":self.now+61},{"cap_error":"identity"}):
            original=copy.deepcopy(self.data["rows"][0])
            self.data["rows"][0].update(change)
            self.assertEqual(self.view()["matched"],0,change)
            self.data["rows"][0]=original

    def test_one_exchange_failure_cannot_prove_intersection(self):
        self.data["sources"]["binance"]["status"]="error"
        self.assertEqual(self.view()["matched"],0)
        self.assertFalse(self.view()["scope_complete"])
        self.cfg["mode"]="union"
        self.assertEqual(self.view()["matched"],1)
        self.assertFalse(self.view()["listing_fresh"]["binance"])

    def test_timeout_disables_previous_results_even_when_recent(self):
        result=discovery_view(self.data,self.cfg,{"last_error":"discovery_failed"},self.now)
        self.assertEqual(result["matched"],0)

    def test_volume_stablecoin_and_unknown_warning_exclusions(self):
        for change, expected in (({"volume_24h":None},"insufficient_volume"),({"coingecko_id":"dai"},"stablecoin")):
            original=copy.deepcopy(self.data["rows"][0])
            self.cfg["min_volume"]=1
            self.data["rows"][0].update(change)
            self.assertIn(expected,self.view()["excluded"])
            self.data["rows"][0]=original
        self.data["rows"][0]["upbit_markets"][0]["warning"]=None
        self.assertIn("upbit_warning_or_unknown",self.view()["excluded"])


class FakePublic:
    def __init__(self):
        self.calls=[]
        self.now=time.time()
        self.fail_caps=False
        self.repeat=False

    async def fetch(self,url,params=None,ttl=60):
        self.calls.append((url,params,ttl))
        if "exchangeInfo" in url:
            return {"symbols":[futures()]}
        if "market/all" in url:
            return [spot()]
        if "coins/list" in url:
            return [{"id":"arpa","symbol":"arpa","name":"ARPA"}]
        if "tickers" in url:
            row={"base":"ARPA","target":"KRW","coin_id":"arpa","market":{"identifier":"upbit"}}
            return {"tickers":[row]*100 if self.repeat else [row]}
        if self.fail_caps:
            raise DataUnavailable("http_429")
        return [{"id":"arpa","symbol":"arpa","name":"ARPA","market_cap":20_000_000,"fully_diluted_valuation":25_000_000,
                 "total_volume":3_000_000,"last_updated":datetime.fromtimestamp(self.now,timezone.utc).isoformat()}]

    def fetched_at(self,*args,**kwargs):
        return self.now


class DiscoveryCollectorTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_snapshot_preserves_provider_time_and_stable_pagination(self):
        public=FakePublic()
        data=await DiscoveryCollector(public).collect()
        self.assertEqual(len(data["rows"]),1)
        self.assertAlmostEqual(data["rows"][0]["cap_observed_at"],public.now,delta=1e-6)
        self.assertEqual(data["sources"]["mapping"]["status"],"ok")
        ticker=[c for c in public.calls if "tickers" in c[0]][0]
        self.assertEqual(ticker[1]["order"],"base_target")
        self.assertEqual(discovery_view(data,{},{} )["matched"],1)

    async def test_rate_limited_caps_are_missing_not_zero(self):
        public=FakePublic()
        public.fail_caps=True
        data=await DiscoveryCollector(public).collect()
        self.assertEqual(data["sources"]["caps"]["status"],"partial")
        self.assertEqual(discovery_view(data,{},{} )["matched"],0)
        self.assertNotIn("market_cap",data["rows"][0])

    async def test_repeated_pages_stop_with_partial_coverage(self):
        public=FakePublic()
        public.repeat=True
        data=await DiscoveryCollector(public).collect()
        self.assertEqual(data["sources"]["mapping"]["status"],"partial")
        self.assertEqual(data["sources"]["mapping"]["error"],"repeated_ticker_page")
        self.assertEqual(len([c for c in public.calls if "tickers" in c[0]]),2)


class DiscoveryApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/"test.db"
        self.app=create_app(self.path,"test-secret")
        self.db=self.app.state.db
        self.store=self.app.state.radar.discovery
        self.admin=self.db.list_users()[0]
        self.other=self.db.create_user("other@example.com","test-password","analyst")
        self.viewer=self.db.create_user("viewer@example.com","test-password","viewer")
        self.client=TestClient(self.app)
        self.client.post("/api/auth/login",json={"email":"admin@example.com","password":"change-me-now"})

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def save_sample(self):
        job=self.store.claim([self.admin["id"]])
        self.assertTrue(self.store.finish(job,sample()))

    def test_per_user_filters_shared_public_data_and_read_only_permissions(self):
        self.save_sample()
        config=DiscoverySettings(max_market_cap=10_000_000).model_dump()
        self.assertEqual(self.client.put("/api/radar/discovery/settings",json=config).status_code,200)
        self.assertEqual(self.client.get("/api/radar/discovery").json()["matched"],0)
        self.assertEqual(self.store.view(self.other["id"])["matched"],1)
        self.client.post("/api/auth/logout")
        self.assertEqual(self.client.get("/api/radar/discovery").status_code,401)
        self.client.post("/api/auth/login",json={"email":self.viewer["email"],"password":"test-password"})
        self.assertEqual(self.client.get("/api/radar/discovery").status_code,200)
        for path in ("refresh","prepare/arpa"):
            self.assertEqual(self.client.post("/api/radar/discovery/"+path).status_code,403)
        self.assertEqual(self.client.put("/api/radar/discovery/settings",json={}).status_code,403)

    def test_leases_cooldown_restart_and_paused_settings(self):
        job=self.store.claim([self.admin["id"]])
        self.assertIsNotNone(job)
        self.assertIsNone(self.store.claim([self.other["id"]]))
        self.assertFalse(self.store.refresh())
        self.assertTrue(self.store.finish(job,sample()))
        self.assertFalse(self.store.finish(job,sample()))
        self.assertEqual(DiscoveryStore(self.db).view(self.admin["id"])["matched"],1)
        self.store.update_settings(self.admin["id"],DiscoverySettings(enabled=False).model_dump())
        self.assertIsNone(self.store.claim([self.admin["id"]],time.time()+10000))
        self.assertEqual(self.client.post("/api/radar/discovery/refresh").status_code,409)

    def test_invalid_settings_and_unknown_prepare_are_rejected(self):
        self.assertEqual(self.client.put("/api/radar/discovery/settings",json={"min_market_cap":10,"max_market_cap":1}).status_code,422)
        self.assertEqual(self.client.post("/api/radar/discovery/prepare/not-in-results").status_code,404)
        self.assertEqual(self.client.post("/api/radar/discovery/refresh").status_code,503)

    def test_prepare_verifies_metadata_and_spot_but_does_not_add_watch(self):
        self.save_sample()
        self.app.state.radar.store.ensure_user(self.admin["id"])
        before=self.app.state.radar.store.watches(self.admin["id"])
        self.app.state.radar.collector=Mock(fetch=AsyncMock(side_effect=[
            {"id":"arpa","symbol":"arpa","name":"ARPA Network","platforms":{"ethereum":"0x"+"1"*40}},
            {"symbols":[{"symbol":"ARPAUSDT","status":"TRADING","isSpotTradingAllowed":True}]}]))
        result=self.client.post("/api/radar/discovery/prepare/arpa")
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(result.json()["asset"]["chain"],"ethereum")
        self.assertEqual(self.app.state.radar.store.watches(self.admin["id"]),before)

    def test_prepare_unsupported_chain_or_identity_does_not_fabricate_address(self):
        self.save_sample()
        for raw in ({"id":"wrong","symbol":"arpa"},{"id":"arpa","symbol":"arpa","platforms":{"solana":"address"}}):
            self.app.state.radar.collector=Mock(fetch=AsyncMock(return_value=raw))
            self.assertEqual(self.client.post("/api/radar/discovery/prepare/arpa").status_code,422)

    def test_prepare_malformed_provider_data_is_rejected(self):
        self.save_sample()
        valid={"id":"arpa","symbol":"arpa","name":"ARPA","platforms":{"ethereum":"0x"+"1"*40}}
        for raw in (None, [], {**valid,"platforms":None}):
            self.app.state.radar.collector=Mock(fetch=AsyncMock(return_value=raw))
            self.assertEqual(self.client.post("/api/radar/discovery/prepare/arpa").status_code,422)
        for listing in (None, [], {"symbols":None}, {"symbols":[None]}):
            self.app.state.radar.collector=Mock(fetch=AsyncMock(side_effect=[valid,listing]))
            self.assertEqual(self.client.post("/api/radar/discovery/prepare/arpa").status_code,422)


class DiscoveryWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_background_scan_runs_without_browser_and_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            app=create_app(Path(directory)/"test.db","test-secret")
            service=app.state.radar
            service.collector=Mock(discover=AsyncMock(return_value=sample()),close=AsyncMock())
            service.discovery_task=asyncio.create_task(service.run_discovery())
            try:
                for _ in range(100):
                    if service.discovery.snapshot()[0]:
                        break
                    await asyncio.sleep(.01)
                self.assertEqual(service.discovery.view(app.state.db.list_users()[0]["id"])["matched"],1)
            finally:
                await service.close()
            self.assertTrue(service.discovery_task.done())

    async def test_revocation_during_scan_discards_result(self):
        with tempfile.TemporaryDirectory() as directory:
            app=create_app(Path(directory)/"test.db","test-secret")
            service=app.state.radar
            admin=app.state.db.list_users()[0]
            async def revoke(**kwargs):
                app.state.db.update_user(admin["id"],permission_revokes=["radar:manage_self"])
                return sample()
            service.collector=Mock(discover=AsyncMock(side_effect=revoke),close=AsyncMock())
            service.discovery_task=asyncio.create_task(service.run_discovery())
            try:
                for _ in range(100):
                    if service.discovery.snapshot()[1]["last_error"]:
                        break
                    await asyncio.sleep(.01)
                self.assertIsNone(service.discovery.snapshot()[0])
                self.assertEqual(service.discovery.snapshot()[1]["last_error"],"discovery_paused")
            finally:
                await service.close()
