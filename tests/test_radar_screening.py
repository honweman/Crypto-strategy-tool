import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from crypto_strategy_lab.radar_discovery import DiscoveryCollector, DiscoverySettings, DiscoveryStore, discovery_view
from crypto_strategy_lab.radar_screening import (BOOK_LIMIT, BOOK_TTL, book_cost, book_queue,
    collect_books, collect_tickers, parse_book)
from crypto_strategy_lab.radar_sources import DataUnavailable
from crypto_strategy_lab.web import create_app


def universe(count=6, now=None):
    now = now or time.time()
    rows, tickers, books = [], {}, {}
    for i in range(count):
        symbol = "COIN" + str(i)
        rows.append({"symbol": symbol, "name": symbol, "coingecko_id": "coin-"+str(i), "mapping": "upbit_pair",
                     "binance_symbol": symbol+"USDT", "upbit_markets": [{"market": "KRW-"+symbol,
                     "quote": "KRW", "name": symbol, "warning": False, "cautions": []}],
                     "market_cap": 20_000_000, "fdv": 40_000_000, "volume_24h": 2_000_000,
                     "change_7d_pct": i, "cap_observed_at": now})
        for key, volume in (("binance:"+symbol+"USDT", (i+1)*2_000_000), ("upbit:KRW-"+symbol, (i+1)*2_000_000_000)):
            tickers[key] = {"quote_volume_24h": volume, "observed_at": now, "received_at": now}
            books[key] = {"status": "ok", "observed_at": now, "received_at": now,
                          "bids": [[9.999, 10000000]], "asks": [[10.001, 10000000]], "market": key}
    return {"collected_at": now, "sources": {k: {"status": "ok", "observed_at": now} for k in ("binance", "upbit", "caps", "mapping")},
            "rows": rows, "unresolved": [], "venue_tickers": tickers, "books": books}


class RankingTests(unittest.TestCase):
    def setUp(self):
        self.now = time.time()
        self.data = universe(now=self.now)
        self.cfg = DiscoverySettings().model_dump()

    def view(self, now=None):
        return discovery_view(self.data, self.cfg, {}, now or self.now)

    def test_fixed_budget_average_ranks_and_constant_component(self):
        result = self.view()
        self.assertEqual(result["screening"]["ranked"], 6)
        self.assertEqual(result["items"][0]["symbol"], "COIN5")
        self.assertEqual(result["items"][0]["screen"]["score"], 90)
        self.assertEqual(result["items"][-1]["screen"]["score"], 10)
        self.assertEqual(result["items"][0]["screen"]["components"], {"momentum": 40, "liquidity": 40, "supply": 10})

    def test_equal_values_tie_and_extreme_momentum_is_capped(self):
        for row in self.data["rows"]:
            row["change_7d_pct"] = 200
            self.data["venue_tickers"]["binance:"+row["binance_symbol"]]["quote_volume_24h"] = 2_000_000
        self.data["rows"][0]["change_7d_pct"] = 15
        self.assertEqual({x["screen"]["score"] for x in self.view()["items"]}, {50})
        self.assertEqual({x["screen"]["rank"] for x in self.view()["items"]}, {1})

    def test_missing_is_not_zero_or_reweighted_and_small_cohorts_unranked(self):
        self.data["rows"][0]["fdv"] = None
        result = self.view()
        missing = next(r for r in result["items"] if r["symbol"] == "COIN0")
        self.assertIsNone(missing["screen"]["score"])
        self.assertEqual(missing["screen"]["missing"], ["supply"])
        self.assertAlmostEqual(missing["screen"]["coverage"], 200/3)
        self.assertEqual(result["screening"]["ranked"], 5)
        self.data["rows"][1]["fdv"] = 0
        self.assertEqual(self.view()["screening"]["ranked"], 0)

    def test_unavailable_stale_and_future_tickers_not_ranked(self):
        for at in (self.now-1201, self.now+6, None):
            self.data["venue_tickers"]["binance:COIN0USDT"]["observed_at"] = at
            row = next(r for r in self.view()["items"] if r["symbol"] == "COIN0")
            self.assertIsNone(row["screen"]["rank"])
            self.assertEqual(row["liquidity"]["status"], "unknown")

    def test_target_native_currency_and_no_venue_fallback(self):
        self.cfg.update(target_market="upbit_krw", order_notional=1_000_000)
        row = self.view()["items"][0]
        self.assertEqual(row["liquidity"]["quote"], "KRW")
        self.assertEqual(row["venue_ticker"]["quote_volume_24h"], 12_000_000_000)
        self.cfg["target_market"] = "upbit_btc"
        self.assertEqual({r["liquidity"]["status"] for r in self.view()["items"]}, {"unsupported"})
        self.assertEqual(self.view()["screening"]["ranked"], 0)

    def test_failed_target_listing_cannot_borrow_other_venue_freshness(self):
        self.cfg["mode"] = "union"
        self.data["sources"]["binance"]["status"] = "error"
        self.assertEqual(self.view()["matched"], 6)
        self.assertEqual(self.view()["screening"]["ranked"], 0)

    def test_every_rejected_row_keeps_reason_and_inputs(self):
        self.data["rows"][0]["market_cap"] = 500_000_000
        self.data["rows"][1]["binance_symbol"] = None
        result = self.view()
        self.assertEqual(len(result["rejected"]), 2)
        self.assertEqual(result["rejected"][0]["exclusion_reasons"], ["outside_cap_range"])
        self.assertEqual(result["rejected"][1]["exclusion_reasons"], ["outside_listing_scope"])

    def test_invalid_liquidity_parameters_rejected(self):
        for cfg in ({"order_notional": 0}, {"order_notional": float("inf")}, {"max_spread_bps": -1},
                    {"min_depth_multiple": .5}, {"target_market": "fake"}):
            with self.assertRaises(ValueError):
                DiscoverySettings(**cfg)


class BookTests(unittest.TestCase):
    def setUp(self):
        self.now = time.time()
        self.market = {"venue": "binance", "symbol": "COINUSDT", "key": "binance:COINUSDT"}
        self.raw = {"bids": [["9.9", "1"], ["9.8", "10"]], "asks": [["10.1", "1"], ["10.2", "10"]], "T": self.now*1000}

    def test_walks_each_level_and_sells_actual_acquired_quantity(self):
        cost = book_cost(parse_book(self.raw, self.market, self.now), 20.3)
        self.assertTrue(cost["full_size"])
        self.assertAlmostEqual(cost["buy_vwap"], 10.15)
        self.assertAlmostEqual(cost["sell_vwap"], 9.85)
        self.assertAlmostEqual(cost["buy_slippage_bps"], 150)
        self.assertAlmostEqual(cost["sell_slippage_bps"], 150)
        self.assertAlmostEqual(cost["round_trip_loss_bps"], (1-19.7/20.3)*10000)

    def test_truncated_book_does_not_extrapolate_or_claim_fill(self):
        book = parse_book(self.raw, self.market, self.now)
        cost = book_cost(book, 1000)
        self.assertFalse(cost["full_size"])
        self.assertIsNone(cost["buy_slippage_bps"])
        book["bids"] = [[9.9, .01]]
        cost = book_cost(book, 20)
        self.assertFalse(cost["full_size"])
        self.assertIsNone(cost["sell_vwap"])

    def test_rejects_bad_levels_and_event_times(self):
        changes = [{"bids": []}, {"bids": [[9.8, 1], [9.9, 2]]}, {"bids": [[9.9, 1], [9.9, 2]]},
                   {"bids": [[11, 1]]}, {"asks": [[10.1, 0]]}, {"asks": [["NaN", 2]]},
                   {"asks": [[1e-308, 1e308], [2e-308, 1e308]]},
                   {"T": None}, {"T": (self.now-121)*1000}, {"T": (self.now+6)*1000}]
        for change in changes:
            with self.assertRaises(ValueError, msg=str(change)):
                parse_book({**self.raw, **change}, self.market, self.now)

    def test_upbit_pair_and_no_aggregation_are_verified(self):
        market = {"venue": "upbit", "symbol": "KRW-COIN", "key": "upbit:KRW-COIN"}
        raw = [{"market": "KRW-COIN", "timestamp": self.now*1000, "level": 0,
                "orderbook_units": [{"bid_price": 999, "ask_price": 1001, "bid_size": 100, "ask_size": 100}]}]
        self.assertEqual(parse_book(raw, market, self.now)["bids"], [[999, 100]])
        for change in ({"market": "BTC-COIN"}, {"level": 1000}):
            with self.assertRaises(ValueError):
                parse_book([{**raw[0], **change}], market, self.now)

    def test_passes_expire_and_risk_gates_are_independent_of_rank(self):
        data = universe(now=self.now)
        self.assertEqual(discovery_view(data, {}, {}, self.now)["screening"]["liquidity_passed"], 6)
        self.assertEqual(discovery_view(data, {}, {}, self.now+BOOK_TTL+1)["screening"]["liquidity_passed"], 0)
        data["books"]["binance:COIN5USDT"]["bids"] = [[9, 1000000]]
        row = discovery_view(data, {}, {}, self.now)["items"][0]
        self.assertEqual(row["screen"]["rank"], 1)
        self.assertEqual(row["liquidity"]["status"], "failed")
        self.assertIn("spread_too_wide", row["liquidity"]["reasons"])

    def test_global_volume_cannot_satisfy_venue_volume_gate(self):
        data = universe(now=self.now)
        data["rows"][0]["volume_24h"] = 1e12
        data["venue_tickers"]["binance:COIN0USDT"]["quote_volume_24h"] = 1
        row = next(r for r in discovery_view(data, {}, {}, self.now)["items"] if r["symbol"] == "COIN0")
        self.assertEqual(row["liquidity"]["reasons"], ["venue_volume_low"])


class QueueTests(unittest.TestCase):
    def test_bounded_top_priority_then_oldest_rotation(self):
        data = universe(80)
        data["books"] = {}
        views = [discovery_view(data, {}, {})]
        queue, all_requests = book_queue(views, {})
        self.assertEqual(len(queue), BOOK_LIMIT)
        self.assertEqual(len(all_requests), 80)
        previous = {"book_attempts": {m["key"]: time.time() for m in queue}}
        next_queue, _ = book_queue(views, previous)
        priority = {m["key"] for m in all_requests[:10]}
        self.assertTrue(priority.issubset({m["key"] for m in queue}))
        self.assertTrue(priority.issubset({m["key"] for m in next_queue}))
        self.assertFalse(({m["key"] for m in queue}-priority) & ({m["key"] for m in next_queue}-priority))
        self.assertNotIn(queue[1]["key"], priority)

    def test_multiple_profiles_deduplicate_and_include_both_venues(self):
        data = universe(40)
        data["books"] = {}
        views = [discovery_view(data, {}, {}), discovery_view(data, {"target_market": "upbit_krw"}, {})]
        queue, _ = book_queue(views*2, {})
        self.assertEqual(len({m["key"] for m in queue}), len(queue))
        self.assertEqual({m["venue"] for m in queue[:10]}, {"upbit", "binance"})


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_collection_joins_reference_prices_tickers_and_books(self):
        data = universe()
        now = data["collected_at"]
        class Public:
            fetched_at = lambda self, *args: now
            async def fetch(self, url, params=None, ttl=60):
                if "exchangeInfo" in url:
                    return {"symbols": [{"symbol": r["binance_symbol"], "baseAsset": r["symbol"],
                        "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT",
                        "marginAsset": "USDT", "underlyingType": "COIN"} for r in data["rows"]]}
                if "market/all" in url:
                    return [{"market": r["upbit_markets"][0]["market"], "english_name": r["name"],
                             "market_event": {"warning": False}} for r in data["rows"]]
                if "coins/list" in url:
                    return [{"id": r["coingecko_id"], "symbol": r["symbol"], "name": r["name"]} for r in data["rows"]]
                if "upbit/tickers" in url:
                    return {"tickers": [{"base": r["symbol"], "target": "KRW", "coin_id": r["coingecko_id"],
                                         "market": {"identifier": "upbit"}} for r in data["rows"]]}
                if "coins/markets" in url:
                    assert params["price_change_percentage"] == "7d"
                    return [{"id": r["coingecko_id"], "symbol": r["symbol"], "market_cap": r["market_cap"],
                             "fully_diluted_valuation": r["fdv"], "current_price": 10, "total_volume": r["volume_24h"],
                             "price_change_percentage_7d_in_currency": r["change_7d_pct"],
                             "last_updated": datetime.fromtimestamp(now, timezone.utc).isoformat()} for r in data["rows"]]
                if "ticker/24hr" in url:
                    return [{"symbol": r["binance_symbol"], "quoteVolume": 2e6, "lastPrice": 10,
                             "closeTime": now*1000} for r in data["rows"]]
                if url.endswith("/ticker"):
                    return [{"market": r["upbit_markets"][0]["market"], "acc_trade_price_24h": 3e9,
                             "trade_price": 15000, "trade_timestamp": now*1000, "timestamp": now*1000} for r in data["rows"]]
                if url.endswith("/depth"):
                    return {"bids": [[9.999, 10000]], "asks": [[10.001, 10000]], "T": now*1000}
                raise AssertionError(url)
        result = await DiscoveryCollector(Public()).collect()
        self.assertEqual(result["schema_version"], 2)
        self.assertEqual(result["rows"][0]["price_usd"], 10)
        self.assertEqual(result["venue_tickers"]["upbit:KRW-COIN0"]["last_price"], 15000)
        view = discovery_view(result, {}, {})
        self.assertEqual(view["screening"]["ranked"], 6)
        self.assertEqual(view["screening"]["liquidity_passed"], 6)
        self.assertEqual(result["book_queue"]["checked"], 6)

    async def test_ticker_units_timestamps_and_partial_coverage(self):
        data = universe(1)
        now = data["collected_at"]
        class Public:
            fetched_at = lambda self, *args: now
            async def fetch(self, url, params=None, ttl=60):
                if "binance" in url:
                    return [{"symbol": "COIN0USDT", "quoteVolume": "2000000", "closeTime": now*1000}]
                return [{"market": "KRW-COIN0", "acc_trade_price_24h": 3e9, "timestamp": now*1000}]
        tickers, sources = await collect_tickers(Public(), data["rows"])
        self.assertEqual(tickers["upbit:KRW-COIN0"]["quote_volume_24h"], 3e9)
        self.assertEqual(tickers["binance:COIN0USDT"]["observed_at"], now)
        self.assertEqual(sources["upbit_tickers"]["status"], "ok")
        public = Public()
        public.fetch = AsyncMock(side_effect=DataUnavailable("http_429"))
        tickers, sources = await collect_tickers(public, data["rows"])
        self.assertFalse(tickers)
        self.assertEqual(sources["binance_tickers"]["errors"], ["http_429"])

    async def test_books_keep_provider_time_and_errors_do_not_reuse_green(self):
        data = universe(1)
        now = data["collected_at"]
        views = [discovery_view(data, {}, {})]
        class Public:
            fetched_at = lambda self, *args: now
            async def fetch(self, url, params=None, ttl=60):
                return {"bids": [[9.99, 10000]], "asks": [[10.01, 10000]], "T": now*1000}
        collected = await collect_books(Public(), views, {})
        self.assertEqual(collected["books"]["binance:COIN0USDT"]["observed_at"], now)
        public = Public()
        public.fetch = AsyncMock(side_effect=DataUnavailable("http_429"))
        collected = await collect_books(public, views, data)
        self.assertEqual(collected["books"]["binance:COIN0USDT"]["status"], "error")
        self.assertNotIn("bids", collected["books"]["binance:COIN0USDT"])


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app(Path(self.tmp.name)/"test.db", "test-secret")
        self.db, self.store = self.app.state.db, self.app.state.radar.discovery
        self.admin = self.db.list_users()[0]
        self.other = self.db.create_user("other@example.com", "test-password", "analyst")
        self.client = TestClient(self.app)
        self.client.post("/api/auth/login", json={"email": "admin@example.com", "password": "change-me-now"})

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def save(self):
        with self.db.connect() as conn:
            conn.execute("UPDATE radar_discovery_state SET last_started=0,next_due=0")
        job = self.store.claim([self.admin["id"]])
        self.assertTrue(self.store.finish(job, universe()))
        return self.store.history(self.admin["id"])[0]["id"]

    def test_freezes_claim_configuration_scores_and_rejected_universe(self):
        job = self.store.claim([self.admin["id"]])
        data = universe()
        data["rows"].append({**data["rows"][0], "symbol": "REJECT", "coingecko_id": "reject", "market_cap": 1e12})
        self.store.update_settings(self.admin["id"], {"max_market_cap": 2_000_000})
        self.assertTrue(self.store.finish(job, data))
        self.assertFalse(self.store.finish(job, data))
        histories = self.store.history(self.admin["id"])
        self.assertEqual(len(histories), 1)
        run = self.store.historical(self.admin["id"], histories[0]["id"])
        self.assertEqual(run["result"]["settings"]["max_market_cap"], 300_000_000)
        self.assertEqual(run["result"]["screening"]["ranked"], 6)
        self.assertEqual(run["result"]["rejected"][0]["symbol"], "REJECT")
        self.assertEqual(len(run["universe"]["rows"]), 7)
        self.assertEqual(self.store.view(self.admin["id"])["matched"], 0)
        self.assertEqual(DiscoveryStore(self.db).historical(self.admin["id"], histories[0]["id"]), run)

    def test_history_owner_permissions_and_pagination(self):
        first, second = self.save(), self.save()
        page = self.client.get("/api/radar/discovery/history?limit=1").json()
        self.assertEqual(page["items"][0]["id"], second)
        page = self.client.get("/api/radar/discovery/history?limit=1&before_id="+str(second)).json()
        self.assertEqual(page["items"][0]["id"], first)
        self.assertEqual(self.client.get("/api/radar/discovery/history?limit=1000").status_code, 422)
        path = "/api/radar/discovery/history/"+str(first)
        self.assertEqual(self.client.get(path).status_code, 200)
        self.client.post("/api/auth/logout")
        self.assertEqual(self.client.get(path).status_code, 401)
        self.client.post("/api/auth/login", json={"email": "other@example.com", "password": "test-password"})
        self.assertEqual(self.client.get(path).status_code, 404)
        self.assertEqual(self.client.get("/api/radar/discovery/history").json()["items"], [])
        self.db.update_user(self.other["id"], permission_revokes=["radar:view"])
        self.assertEqual(self.client.get(path).status_code, 403)

    def test_expiry_cascades_and_failed_runs_do_not_fabricate_history(self):
        first = self.save()
        with self.db.connect() as conn:
            conn.execute("UPDATE radar_discovery_runs SET created_at=?", (time.time()-31*86400,))
        self.save()
        self.assertIsNone(self.store.historical(self.admin["id"], first))
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM radar_discovery_results").fetchone()[0], 1)
            conn.execute("UPDATE radar_discovery_state SET last_started=0,next_due=0")
        job = self.store.claim([self.admin["id"]])
        self.store.finish(job, error="discovery_failed")
        self.assertEqual(len(self.store.history(self.admin["id"])), 1)
