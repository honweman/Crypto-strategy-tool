"""Public exchange universe discovery, separate from entry signals and order execution."""
from __future__ import annotations

import asyncio
import json
import re
import secrets
import time
from collections import defaultdict
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from .radar import number
from .radar_sources import DataUnavailable, timestamp

LIST_TTL = 3600
CAP_TTL = 3600
MODES = {"intersection": "两边都上市", "union": "任一平台上市", "binance": "币安 USDT 永续", "upbit": "Upbit 韩国现货"}
STABLE_IDS = {"tether", "usd-coin", "dai", "first-digital-usd", "true-usd", "pax-dollar", "frax",
              "ethena-usde", "usdd", "paypal-usd", "usd1-wlfi", "usds", "gemini-dollar", "usual-usd"}
SYMBOL = re.compile(r"^[A-Z0-9]{1,30}$")
COIN_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,100}$")


class DiscoverySettings(BaseModel):
    enabled: bool = True
    mode: Literal["intersection", "union", "binance", "upbit"] = "intersection"
    upbit_quote: Literal["all", "KRW", "BTC", "USDT"] = "all"
    min_market_cap: float = Field(default=1_000_000, ge=0, le=1e13, allow_inf_nan=False)
    max_market_cap: float = Field(default=300_000_000, gt=0, le=1e14, allow_inf_nan=False)
    min_volume: float = Field(default=0, ge=0, le=1e13, allow_inf_nan=False)
    exclude_upbit_warning: bool = True
    interval_seconds: int = Field(default=900, ge=900, le=86400)

    @model_validator(mode="after")
    def valid_range(self):
        if self.min_market_cap >= self.max_market_cap:
            raise ValueError("最小流通市值必须小于最大流通市值")
        return self


def binance_markets(raw: dict) -> dict:
    if not isinstance(raw.get("symbols"), list) or not raw["symbols"]:
        raise DataUnavailable("invalid_binance_listing")
    output = {}
    for row in raw["symbols"]:
        base = row.get("baseAsset", "")
        if (row.get("status") == "TRADING" and row.get("contractType") == "PERPETUAL"
                and row.get("quoteAsset") == "USDT" and row.get("marginAsset") == "USDT"
                and row.get("underlyingType") == "COIN" and SYMBOL.fullmatch(base)
                and row.get("symbol") == base + "USDT"):
            output[base] = row["symbol"]
    if not output:
        raise DataUnavailable("empty_binance_listing")
    return output


def upbit_markets(raw: list) -> dict:
    if not isinstance(raw, list) or not raw:
        raise DataUnavailable("invalid_upbit_listing")
    output = defaultdict(list)
    for row in raw:
        quote, sep, base = str(row.get("market", "")).partition("-")
        if not sep or quote not in ("KRW", "BTC", "USDT") or not SYMBOL.fullmatch(base):
            continue
        event = row.get("market_event", {})
        warning = event.get("warning")
        if not isinstance(warning, bool):
            warning = {"NONE": False, "CAUTION": True}.get(row.get("market_warning"))
        output[base].append({"market": row["market"], "quote": quote, "name": row.get("english_name", base),
                             "warning": warning, "cautions": sorted(k for k, value in event.get("caution", {}).items() if value is True)})
    if not output:
        raise DataUnavailable("empty_upbit_listing")
    return dict(output)


def map_assets(binance: dict, upbit: dict, catalog: list, tickers: list) -> tuple[list, list]:
    by_symbol, by_pair = defaultdict(dict), defaultdict(set)
    for coin in catalog:
        symbol, cid = str(coin.get("symbol", "")).upper(), coin.get("id", "")
        if SYMBOL.fullmatch(symbol) and COIN_ID.fullmatch(cid):
            by_symbol[symbol][cid] = coin
    for ticker in tickers:
        cid = ticker.get("coin_id", "")
        if ticker.get("market", {}).get("identifier") == "upbit" and COIN_ID.fullmatch(cid):
            by_pair[str(ticker.get("target", "")) + "-" + str(ticker.get("base", ""))].add(cid)
    rows, unresolved = [], []
    for symbol in sorted(set(binance) | set(upbit)):
        markets = upbit.get(symbol, [])
        row = {"symbol": symbol, "name": markets[0]["name"] if markets else symbol,
               "binance_symbol": binance.get(symbol), "upbit_markets": markets}
        ids = set().union(*(by_pair[m["market"]] for m in markets)) if markets else set()
        candidates = by_symbol[symbol]
        method = "upbit_pair"
        if not ids:
            exact_name = {cid for cid, c in candidates.items() if markets and
                          str(c.get("name", "")).casefold() == str(markets[0]["name"]).casefold()}
            ids = exact_name or set(candidates)
            method = "name_match" if exact_name else "unique_symbol"
        if len(ids) != 1:
            unresolved.append({**row, "reason": "ambiguous_identity" if ids else "unmapped_identity"})
            continue
        cid = next(iter(ids))
        rows.append({**row, "coingecko_id": cid, "mapping": method})
    return rows, unresolved


class DiscoveryCollector:
    def __init__(self, public):
        self.public = public

    async def collect(self):
        sources = {}

        async def get(key, url, params=None, ttl=900, fallback=None):
            try:
                data = await self.public.fetch(url, params, ttl=ttl)
                sources[key] = {"status": "ok", "observed_at": self.public.fetched_at(url, params), "url": url}
                return data
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                sources[key] = {"status": "error", "error": str(exc) if isinstance(exc, DataUnavailable) else "invalid_provider_data", "url": url}
                return fallback

        bn_raw = await get("binance", "https://fapi.binance.com/fapi/v1/exchangeInfo", fallback={})
        ub_raw = await get("upbit", "https://api.upbit.com/v1/market/all", {"is_details": "true"}, fallback=[])
        bn, ub = {}, {}
        for key, raw, parser in (("binance", bn_raw, binance_markets), ("upbit", ub_raw, upbit_markets)):
            try:
                parsed = parser(raw)
                if key == "binance":
                    bn = parsed
                else:
                    ub = parsed
                sources[key]["assets"] = len(parsed)
            except (DataUnavailable, TypeError, AttributeError, KeyError):
                sources[key].update(status="error", error=sources[key].get("error", "invalid_listing_data"))
        catalog = await get("catalog", "https://api.coingecko.com/api/v3/coins/list", ttl=86400, fallback=[])
        if not isinstance(catalog, list):
            sources["catalog"].update(status="error", error="invalid_catalog")
            catalog = []
        tickers, page_times, seen = [], [], set()
        complete = False
        ticker_url = "https://api.coingecko.com/api/v3/exchanges/upbit/tickers"
        if ub:
            for page in range(1, 11):
                raw = await get("mapping", ticker_url, {"page": page, "order": "base_target", "depth": "false"}, ttl=21600, fallback={})
                batch = raw.get("tickers") if isinstance(raw, dict) else None
                if not isinstance(batch, list):
                    sources["mapping"].update(status="partial", error=sources["mapping"].get("error", "invalid_ticker_page"))
                    break
                page_times.append(sources["mapping"]["observed_at"])
                identities = {(x.get("base"), x.get("target"), x.get("coin_id")) for x in batch}
                if batch and not identities - seen:
                    sources["mapping"].update(status="partial", error="repeated_ticker_page")
                    break
                seen.update(identities)
                tickers.extend(batch)
                if len(batch) < 100:
                    complete = True
                    break
            sources["mapping"].update(status="ok" if complete else "partial", pages=len(page_times),
                                      observed_at=min(page_times) if page_times else None)
            if not complete:
                sources["mapping"].setdefault("error", "ticker_page_limit")
        else:
            sources["mapping"] = {"status": "error", "error": "upbit_listing_unavailable", "url": ticker_url}
        rows, unresolved = map_assets(bn, ub, catalog, tickers)
        ids = sorted({row["coingecko_id"] for row in rows})
        market_data, cap_times, errors = {}, [], []
        cap_url = "https://api.coingecko.com/api/v3/coins/markets"
        for offset in range(0, len(ids), 200):
            batch_ids = ids[offset:offset+200]
            raw = await get("caps", cap_url, {"vs_currency": "usd", "ids": ",".join(batch_ids),
                                             "per_page": 250, "page": 1, "sparkline": "false"}, fallback=[])
            if sources["caps"]["status"] != "ok" or not isinstance(raw, list):
                errors.append(sources["caps"].get("error", "invalid_market_data"))
                continue
            cap_times.append(sources["caps"]["observed_at"])
            for coin in raw:
                if coin.get("id") in batch_ids:
                    market_data[coin["id"]] = coin
        sources["caps"] = {"status": "partial" if errors or not ids or len(market_data) < len(ids) else "ok", "url": cap_url,
                           "observed_at": min(cap_times) if cap_times else None, "errors": errors,
                           "requested": len(ids), "returned": len(market_data)}
        for row in rows:
            coin = market_data.get(row["coingecko_id"], {})
            if str(coin.get("symbol", "")).upper() != row["symbol"]:
                row["cap_error"] = "market_identity_mismatch" if coin else "market_cap_missing"
                continue
            try:
                observed = timestamp(coin.get("last_updated"))
            except (ValueError, DataUnavailable, TypeError):
                observed = None
            row.update(name=coin.get("name", row["name"]), market_cap=number(coin.get("market_cap")),
                       fdv=number(coin.get("fully_diluted_valuation")), volume_24h=number(coin.get("total_volume")),
                       change_24h_pct=number(coin.get("price_change_percentage_24h")),
                       cap_observed_at=observed, url="https://www.coingecko.com/en/coins/" + row["coingecko_id"])
        return {"schema_version": 1, "collected_at": time.time(), "sources": sources, "rows": rows, "unresolved": unresolved}


def discovery_view(payload, settings, state, now=None):
    now = time.time() if now is None else now
    cfg = DiscoverySettings(**settings)
    payload = payload or {"sources": {}, "rows": [], "unresolved": []}
    sources = payload["sources"]
    fresh = {key: sources.get(key, {}).get("status") == "ok" and
             number(sources.get(key, {}).get("observed_at")) is not None and
             -60 <= now-sources[key]["observed_at"] <= LIST_TTL for key in ("binance", "upbit")}
    matches, excluded = [], defaultdict(int)
    scope_count = 0
    for original in payload["rows"]:
        row = dict(original)
        markets = [m for m in row["upbit_markets"] if cfg.upbit_quote == "all" or m["quote"] == cfg.upbit_quote]
        bn, ub = bool(row["binance_symbol"]), bool(markets)
        in_scope = {"intersection": bn and ub, "union": bn or ub, "binance": bn, "upbit": ub}[cfg.mode]
        if not in_scope:
            continue
        scope_count += 1
        reasons = []
        valid_listing = {"intersection": bn and ub and fresh["binance"] and fresh["upbit"],
                         "union": (bn and fresh["binance"]) or (ub and fresh["upbit"]),
                         "binance": bn and fresh["binance"], "upbit": ub and fresh["upbit"]}[cfg.mode]
        if not valid_listing or state.get("last_error"):
            reasons.append("listing_unavailable")
        cap, at, volume = number(row.get("market_cap")), number(row.get("cap_observed_at")), number(row.get("volume_24h"))
        if row.get("cap_error") or cap is None or cap <= 0:
            reasons.append("market_cap_missing")
        elif at is None or not -60 <= now-at <= CAP_TTL:
            reasons.append("market_cap_stale")
        elif not cfg.min_market_cap <= cap <= cfg.max_market_cap:
            reasons.append("outside_cap_range")
        if cfg.min_volume > 0 and (volume is None or volume < cfg.min_volume):
            reasons.append("insufficient_volume")
        if cfg.exclude_upbit_warning and markets and (not fresh["upbit"] or any(m["warning"] is not False for m in markets)):
            reasons.append("upbit_warning_or_unknown")
        if row["coingecko_id"] in STABLE_IDS:
            reasons.append("stablecoin")
        if reasons:
            for reason in set(reasons):
                excluded[reason] += 1
            continue
        row["selected_upbit_markets"] = markets
        matches.append(row)
    unresolved = []
    for row in payload["unresolved"]:
        bn = bool(row["binance_symbol"])
        ub = any(cfg.upbit_quote == "all" or m["quote"] == cfg.upbit_quote for m in row["upbit_markets"])
        if {"intersection": bn and ub, "union": bn or ub, "binance": bn, "upbit": ub}[cfg.mode]:
            unresolved.append(row)
    matches.sort(key=lambda x: (x["market_cap"], x["symbol"]))
    return {"settings": cfg.model_dump(), "modes": MODES, "state": state, "sources": sources,
            "listing_fresh": fresh, "collected_at": payload.get("collected_at"), "items": matches,
            "unresolved": unresolved, "excluded": dict(excluded), "mapped_in_scope": scope_count,
            "matched": len(matches), "mapping_complete": sources.get("mapping", {}).get("status") == "ok",
            "scope_complete": all(fresh[k] for k in ({"binance", "upbit"} if cfg.mode in ("intersection", "union") else {cfg.mode}))}


class DiscoveryStore:
    def __init__(self, db):
        self.db = db
        with db.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS radar_discovery_settings (
                    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                    config TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS radar_discovery_state (
                    id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT, next_due REAL NOT NULL DEFAULT 0,
                    last_started REAL NOT NULL DEFAULT 0, last_finished REAL, last_error TEXT,
                    lease_token TEXT, lease_until REAL NOT NULL DEFAULT 0
                );
                INSERT OR IGNORE INTO radar_discovery_state(id) VALUES (1);
            """)

    def settings(self, user_id):
        with self.db.connect() as conn:
            conn.execute("INSERT OR IGNORE INTO radar_discovery_settings VALUES (?, ?)",
                         (user_id, json.dumps(DiscoverySettings().model_dump())))
            row = conn.execute("SELECT config FROM radar_discovery_settings WHERE user_id=?", (user_id,)).fetchone()
        return DiscoverySettings(**json.loads(row["config"])).model_dump()

    def update_settings(self, user_id, config):
        value = DiscoverySettings(**config).model_dump()
        self.settings(user_id)
        with self.db.connect() as conn:
            conn.execute("UPDATE radar_discovery_settings SET config=? WHERE user_id=?", (json.dumps(value), user_id))
        return value

    def snapshot(self):
        with self.db.connect() as conn:
            row = dict(conn.execute("SELECT * FROM radar_discovery_state WHERE id=1").fetchone())
        serialized = row.pop("payload")
        payload = json.loads(serialized) if serialized else None
        row.pop("lease_token", None)
        row["updating"] = row.pop("lease_until") > time.time()
        return payload, row

    def view(self, user_id):
        payload, state = self.snapshot()
        return discovery_view(payload, self.settings(user_id), state)

    def claim(self, user_ids, now=None):
        now = time.time() if now is None else now
        configs = [self.settings(uid) for uid in user_ids]
        intervals = [c["interval_seconds"] for c in configs if c["enabled"]]
        if not intervals:
            return None
        token = secrets.token_hex(16)
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute("""UPDATE radar_discovery_state SET lease_token=?, lease_until=?, last_started=?
                                  WHERE id=1 AND lease_until<=? AND next_due<=? AND last_started<=?""",
                                  (token, now+300, now, now, now, now-60))
        return {"token": token, "interval": min(intervals)} if cursor.rowcount else None

    def finish(self, job, payload=None, error=None):
        now = time.time()
        serialized = json.dumps(payload, allow_nan=False) if payload is not None else None
        with self.db.connect() as conn:
            return bool(conn.execute("""UPDATE radar_discovery_state SET payload=COALESCE(?,payload),
                        last_finished=CASE WHEN ? IS NOT NULL THEN ? ELSE last_finished END,
                        last_error=?, next_due=?, lease_token=NULL, lease_until=0 WHERE id=1 AND lease_token=?""",
                        (serialized, serialized, now, error, now+job["interval"], job["token"])).rowcount)

    def refresh(self):
        now = time.time()
        with self.db.connect() as conn:
            return bool(conn.execute("UPDATE radar_discovery_state SET next_due=0 WHERE id=1 AND lease_until<=? AND last_started<=?",
                                     (now, now-60)).rowcount)
