"""Research-only cross-sectional screening and venue-native book capacity checks."""
from __future__ import annotations

import asyncio
import time
from bisect import bisect_left, bisect_right
from itertools import zip_longest

from .radar import number
from .radar_sources import DataUnavailable

SCREEN_VERSION = "universe-screen-v1"
TICKER_TTL = 1200
BOOK_TTL = 120
BOOK_LIMIT = 30
TARGETS = {
    "binance_usdt": {"label": "币安 USDT 永续", "venue": "binance", "quote": "USDT", "default_notional": 1000},
    "upbit_krw": {"label": "Upbit KRW 现货", "venue": "upbit", "quote": "KRW", "default_notional": 1000000},
    "upbit_btc": {"label": "Upbit BTC 现货", "venue": "upbit", "quote": "BTC", "default_notional": .01},
    "upbit_usdt": {"label": "Upbit USDT 现货", "venue": "upbit", "quote": "USDT", "default_notional": 1000},
}


def fresh(at, now, ttl):
    at = number(at)
    return at is not None and -5 <= now - at <= ttl


def target_market(row, cfg):
    target = TARGETS[cfg.target_market]
    if target["venue"] == "binance":
        symbol = row.get("binance_symbol")
    else:
        symbol = next((m["market"] for m in row.get("selected_upbit_markets", row["upbit_markets"])
                       if m["quote"] == target["quote"]), None)
    return {"key": target["venue"] + ":" + symbol, "symbol": symbol, **target} if symbol else None


def ticker_for(row, cfg, payload, now):
    market = target_market(row, cfg)
    if not market:
        return None, None
    source = payload.get("sources", {}).get(market["venue"], {})
    if source.get("status") != "ok" or not fresh(source.get("observed_at"), now, 3600):
        return market, None
    ticker = payload.get("venue_tickers", {}).get(market["key"], {})
    volume = number(ticker.get("quote_volume_24h"))
    if (volume is None or volume < 0 or not fresh(ticker.get("observed_at"), now, TICKER_TTL)
            or not fresh(ticker.get("received_at"), now, TICKER_TTL)):
        return market, None
    return market, ticker


def rank_items(items, cfg, payload, now):
    cohort = []
    for row in items:
        market, ticker = ticker_for(row, cfg, payload, now)
        momentum = number(row.get("change_7d_pct"))
        cap, fdv = number(row.get("market_cap")), number(row.get("fdv"))
        float_ratio = min(cap / fdv, 1) if cap and fdv and fdv >= cap > 0 else None
        values = {"momentum": min(momentum, 15) if momentum is not None and momentum > -100 else None,
                  "liquidity": ticker["quote_volume_24h"] if ticker else None, "supply": float_ratio}
        missing = [key for key, value in values.items() if value is None]
        row["screen"] = {"rank": None, "score": None, "coverage": (3-len(missing))/3*100,
                         "missing": missing, "values": values, "components": {}, "status": "missing" if missing else "small_cohort"}
        row["target"] = market
        row["venue_ticker"] = ticker
        if not missing:
            cohort.append(row)
    weights = {"momentum": 40, "liquidity": 40, "supply": 20}
    if len(cohort) >= 5:
        for key, weight in weights.items():
            values = sorted(row["screen"]["values"][key] for row in cohort)
            for row in cohort:
                value = row["screen"]["values"][key]
                rank = (bisect_left(values, value) + bisect_right(values, value) - 1) / 2
                row["screen"]["components"][key] = rank / (len(values)-1) * weight
        for row in cohort:
            row["screen"].update(score=round(sum(row["screen"]["components"].values()), 4), status="ranked")
        cohort.sort(key=lambda r: (-r["screen"]["score"], r["coingecko_id"]))
        previous_score, rank = None, None
        for index, row in enumerate(cohort, 1):
            if row["screen"]["score"] != previous_score:
                rank = index
            row["screen"]["rank"] = rank
            previous_score = row["screen"]["score"]
    for row in items:
        row["liquidity"] = check_liquidity(row, cfg, payload, now)
    items.sort(key=lambda r: (r["screen"]["rank"] or float("inf"),
                             0 if r["screen"]["rank"] else r["market_cap"], r["coingecko_id"]))
    return {"version": SCREEN_VERSION, "cohort_size": len(cohort), "ranked": len(cohort) if len(cohort) >= 5 else 0,
            "weights": weights, "target_market": cfg.target_market, "book_ttl_seconds": BOOK_TTL,
            "liquidity_passed": sum(r["liquidity"]["status"] == "passed" for r in items)}


def parse_book(raw, market, received_at):
    if market["venue"] == "upbit":
        if not isinstance(raw, list) or len(raw) != 1 or raw[0].get("market") != market["symbol"]:
            raise ValueError("book_identity_mismatch")
        book = raw[0]
        if number(book.get("level", 0)) != 0:
            raise ValueError("aggregated_book_unsupported")
        bids = [[r["bid_price"], r["bid_size"]] for r in book["orderbook_units"]]
        asks = [[r["ask_price"], r["ask_size"]] for r in book["orderbook_units"]]
        at = number(book.get("timestamp"))
    else:
        bids, asks, at = raw["bids"], raw["asks"], number(raw.get("T"))
    sides = []
    for rows, descending in ((bids, True), (asks, False)):
        if not isinstance(rows, list) or not rows or len(rows) > 100:
            raise ValueError("invalid_book_levels")
        levels = []
        for row in rows:
            p, q = number(row[0]), number(row[1])
            if p is None or q is None or p <= 0 or q <= 0 or number(p*q) is None:
                raise ValueError("invalid_book_level")
            levels.append([p, q])
        prices = [p for p, _ in levels]
        if prices != sorted(set(prices), reverse=descending):
            raise ValueError("unordered_or_duplicate_book")
        if number(sum(q for _, q in levels)) is None or number(sum(p*q for p, q in levels)) is None:
            raise ValueError("invalid_book_totals")
        sides.append(levels)
    if sides[0][0][0] >= sides[1][0][0]:
        raise ValueError("crossed_book")
    if at is None or not fresh(at / 1000, received_at, BOOK_TTL):
        raise ValueError("stale_book")
    return {"status": "ok", "observed_at": at / 1000, "received_at": received_at,
            "bids": sides[0], "asks": sides[1], "market": market["key"]}


def book_cost(book, notional):
    bids, asks = book["bids"], book["asks"]
    mid = bids[0][0] + (asks[0][0]-bids[0][0])/2
    remaining, quantity = notional, 0
    for price, size in asks:
        spent = min(remaining, price * size)
        quantity += spent / price
        remaining -= spent
        if remaining <= notional * 1e-10:
            break
    buy_full = remaining <= notional * 1e-10
    remaining_qty, proceeds = quantity, 0
    for price, size in bids:
        sold = min(remaining_qty, size)
        proceeds += sold * price
        remaining_qty -= sold
        if remaining_qty <= quantity * 1e-10:
            break
    sell_full = buy_full and remaining_qty <= quantity * 1e-10
    buy_vwap = notional / quantity if buy_full and quantity else None
    sell_vwap = proceeds / quantity if sell_full and quantity else None
    return {"spread_bps": (asks[0][0]-bids[0][0])/mid*10000,
            "buy_slippage_bps": (buy_vwap/mid-1)*10000 if buy_vwap else None,
            "sell_slippage_bps": (1-sell_vwap/mid)*10000 if sell_vwap else None,
            "buy_vwap": buy_vwap, "sell_vwap": sell_vwap,
            "round_trip_loss_bps": (1-proceeds/notional)*10000 if sell_full else None,
            "bid_depth_1pct": sum(p*q for p, q in bids if p >= mid*.99),
            "ask_depth_1pct": sum(p*q for p, q in asks if p <= mid*1.01),
            "full_size": buy_full and sell_full}


def check_liquidity(row, cfg, payload, now):
    market, ticker = ticker_for(row, cfg, payload, now)
    result = {"status": "unsupported", "reasons": ["target_not_listed"], "notional": cfg.order_notional,
              "quote": TARGETS[cfg.target_market]["quote"], "observed_at": None}
    if not market:
        return result
    result.update(market=market["key"], status="unknown", reasons=["ticker_unavailable"])
    if ticker is None:
        return result
    if ticker["quote_volume_24h"] < cfg.order_notional * cfg.min_volume_multiple:
        result.update(status="failed", reasons=["venue_volume_low"])
        return result
    book = payload.get("books", {}).get(market["key"])
    if not book:
        result.update(status="pending", reasons=["book_not_checked"])
        return result
    result["observed_at"] = book.get("observed_at")
    if book.get("status") != "ok":
        result.update(status="unknown", reasons=[book.get("error", "book_unavailable")])
        return result
    if (not fresh(book.get("observed_at"), now, BOOK_TTL)
            or not fresh(book.get("received_at"), now, BOOK_TTL)):
        result.update(status="stale", reasons=["book_stale"])
        return result
    costs = book_cost(book, cfg.order_notional)
    reasons = []
    if not costs["full_size"]:
        reasons.append("insufficient_book_size")
    if costs["spread_bps"] > cfg.max_spread_bps:
        reasons.append("spread_too_wide")
    if any(costs[k] is not None and costs[k] > cfg.max_slippage_bps for k in ("buy_slippage_bps", "sell_slippage_bps")):
        reasons.append("slippage_too_high")
    if min(costs["bid_depth_1pct"], costs["ask_depth_1pct"]) < cfg.order_notional * cfg.min_depth_multiple:
        reasons.append("depth_too_low")
    result.update(costs, status="failed" if reasons else "passed", reasons=reasons)
    return result


async def collect_tickers(public, rows):
    output, sources = {}, {}
    bn_symbols = {r["binance_symbol"] for r in rows if r["binance_symbol"]}
    ub_symbols = sorted({m["market"] for r in rows for m in r["upbit_markets"]})
    requests = []
    if bn_symbols:
        requests.append(("binance", "https://fapi.binance.com/fapi/v1/ticker/24hr", None, bn_symbols))
    for start in range(0, len(ub_symbols), 80):
        symbols = ub_symbols[start:start+80]
        requests.append(("upbit", "https://api.upbit.com/v1/ticker", {"markets": ",".join(symbols)}, set(symbols)))
    for venue, url, params, symbols in requests:
        source = sources.setdefault(venue+"_tickers", {"status": "ok", "url": url, "returned": 0, "errors": []})
        try:
            raw = await public.fetch(url, params, ttl=60)
            received = public.fetched_at(url, params)
            if not isinstance(raw, list):
                raise ValueError("invalid_tickers")
            parsed = {}
            for r in raw:
                if not isinstance(r, dict):
                    continue
                symbol = r.get("symbol" if venue == "binance" else "market")
                if symbol not in symbols:
                    continue
                volume = number(r.get("quoteVolume" if venue == "binance" else "acc_trade_price_24h"))
                at = number(r.get("closeTime" if venue == "binance" else "timestamp"))
                if volume is None or volume < 0 or at is None:
                    continue
                parsed[venue+":"+symbol] = {"quote_volume_24h": volume, "observed_at": at/1000,
                    "received_at": received, "last_price": number(r.get("lastPrice" if venue == "binance" else "trade_price")),
                    "last_trade_at": number(r.get("trade_timestamp"))/1000
                        if venue == "upbit" and number(r.get("trade_timestamp")) is not None else None}
            output.update(parsed)
            source["returned"] += len(parsed)
            source["observed_at"] = min(source.get("observed_at", received), received)
            if len(parsed) < len(symbols):
                source["status"] = "partial"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            source["status"] = "partial"
            source["errors"].append(str(exc) if isinstance(exc, DataUnavailable) else "invalid_tickers")
    return output, sources


def book_queue(views, previous):
    # Round-robin across distinct configurations, then spend most capacity on oldest/unseen markets.
    groups = [[r["target"] for r in view["items"] if r.get("target") and r.get("venue_ticker")
               and r["liquidity"]["status"] != "failed"] for view in views]
    requests, seen = [], set()
    for group in zip_longest(*groups):
        for market in group:
            if market and market["key"] not in seen:
                requests.append(market)
                seen.add(market["key"])
    attempts = (previous or {}).get("book_attempts", {})
    priority = requests[:10]
    rest = sorted(requests[10:], key=lambda m: attempts.get(m["key"], 0))[:BOOK_LIMIT-len(priority)]
    queue = []
    # Interleave rotation slots so slow priority requests cannot exhaust the whole time budget first.
    for group in zip_longest(priority, rest[::2], rest[1::2]):
        queue.extend(m for m in group if m is not None)
    return queue, requests


async def collect_books(public, views, previous):
    queue, all_requests = book_queue(views, previous)
    keys = {m["key"] for m in all_requests}
    books = {k: v for k, v in (previous or {}).get("books", {}).items() if k in keys}
    attempts = {k: v for k, v in (previous or {}).get("book_attempts", {}).items() if k in keys}
    deadline, checked = time.monotonic() + 40, 0
    for market in queue:
        remaining = deadline-time.monotonic()
        if remaining < 1:
            break
        key = market["key"]
        if market["venue"] == "binance":
            url, params = "https://fapi.binance.com/fapi/v1/depth", {"symbol": market["symbol"], "limit": 100}
        else:
            url, params = "https://api.upbit.com/v1/orderbook", {"markets": market["symbol"], "count": 30, "level": 0}
        attempts[key] = time.time()
        checked += 1
        try:
            raw = await asyncio.wait_for(public.fetch(url, params, ttl=30), timeout=min(8, remaining))
            books[key] = {**parse_book(raw, market, public.fetched_at(url, params)), "url": url}
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            books[key] = {"status": "error", "error": str(exc) if isinstance(exc, (DataUnavailable, ValueError)) else "book_unavailable",
                          "received_at": time.time(), "url": url}
    return {"books": books, "book_attempts": attempts,
            "book_queue": {"requested": len(all_requests), "checked": checked, "limit": BOOK_LIMIT}}
