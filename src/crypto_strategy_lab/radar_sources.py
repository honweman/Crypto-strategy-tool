"""Read-only public data adapters with bounded requests and explicit missing data."""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from datetime import datetime, timezone
from decimal import Decimal
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlparse

import httpx
from defusedxml import ElementTree
from eth_abi import decode, encode
from eth_abi.exceptions import DecodingError
from eth_utils import keccak

from .radar import number, source_fresh
from .radar_factors import technical_bundle, closed_daily_bars

CHAINS = {
    "ethereum": {"explorer": "https://eth.blockscout.com", "rpc": "https://ethereum-rpc.publicnode.com",
                 "safe": "https://safe-transaction-mainnet.safe.global", "scan": "https://etherscan.io"},
    "base": {"explorer": "https://base.blockscout.com", "rpc": "https://mainnet.base.org",
             "safe": "https://safe-transaction-base.safe.global", "scan": "https://basescan.org"},
}
FEEDS = [("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
         ("Cointelegraph", "https://cointelegraph.com/rss"), ("Decrypt", "https://decrypt.co/feed")]
EVENTS = {"OwnershipTransferred": "OwnershipTransferred(address,address)",
          "RoleGranted": "RoleGranted(bytes32,address,address)", "RoleRevoked": "RoleRevoked(bytes32,address,address)",
          "AddedOwner": "AddedOwner(address)", "RemovedOwner": "RemovedOwner(address)",
          "ChangedThreshold": "ChangedThreshold(uint256)"}


class DataUnavailable(Exception):
    pass


def timestamp(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value) / 1000 if value > 1e11 else float(value)
    if not value:
        raise DataUnavailable("missing_timestamp")
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()


def positive(value: Any, field: str, allow_zero: bool = False) -> float:
    result = number(value)
    if result is None or result < 0 or (result == 0 and not allow_zero):
        raise DataUnavailable("invalid_" + field)
    return result


def result(source: str, url: str, observed_at: float, data: dict, warnings=None) -> dict:
    return {"status": "ok", "source": source, "url": url, "observed_at": observed_at,
            "collected_at": time.time(), "data": data, "warnings": warnings or []}


class PublicData:
    def __init__(self, client: httpx.AsyncClient | None = None):
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(12, connect=5), follow_redirects=True)
        self.owned_client = client is None
        self.cache: dict[str, tuple[float, Any, float]] = {}
        # Fixed stripes coalesce identical requests without retaining every historical block key.
        self.locks = [asyncio.Lock() for _ in range(64)]
        self.host_locks: dict[str, asyncio.Lock] = {}
        self.next_host: dict[str, float] = {}
        self.blocked_host: dict[str, float] = {}

    async def close(self):
        if self.owned_client:
            await self.client.aclose()

    async def discover(self):
        from .radar_discovery import DiscoveryCollector
        return await DiscoveryCollector(self).collect()

    def fetched_at(self, url, params=None, body=None, raw=False):
        cached = self.cache.get(json.dumps([url, params, body, raw], sort_keys=True))
        if not cached:
            raise DataUnavailable("cache_timestamp_unavailable")
        return cached[2]

    async def fetch(self, url: str, params=None, body=None, ttl: int = 60, raw: bool = False):
        key = json.dumps([url, params, body, raw], sort_keys=True)
        lock = self.locks[hash(key) % len(self.locks)]
        async with lock:
            cached = self.cache.get(key)
            if cached and cached[0] > time.time():
                return cached[1]
            host = urlparse(url).netloc
            async with self.host_locks.setdefault(host, asyncio.Lock()):
                now = time.time()
                if self.blocked_host.get(host, 0) > now:
                    raise DataUnavailable("provider_cooldown")
                await asyncio.sleep(max(0, self.next_host.get(host, 0) - now))
                headers = {"User-Agent": "CryptoStrategyLab/0.1 (public research monitor)"}
                if host == "api.coingecko.com" and os.getenv("CRYPTO_LAB_COINGECKO_DEMO_KEY"):
                    headers["x-cg-demo-api-key"] = os.environ["CRYPTO_LAB_COINGECKO_DEMO_KEY"]
                try:
                    response = await self.client.request("POST" if body is not None else "GET", url,
                                                         params=params, json=body, headers=headers)
                except httpx.HTTPError:
                    self.blocked_host[host] = time.time() + 30
                    raise DataUnavailable("network_error") from None
                finally:
                    if host == "api.coingecko.com":
                        spacing = 2.1 if os.getenv("CRYPTO_LAB_COINGECKO_DEMO_KEY") else 12.1
                    else:
                        spacing = 1.2 if "blockscout" in host else 0.4
                    self.next_host[host] = time.time() + spacing
                if response.status_code == 429:
                    retry = number(response.headers.get("Retry-After"))
                    if retry is None:
                        try:
                            retry = parsedate_to_datetime(response.headers["Retry-After"]).timestamp() - time.time()
                        except (ValueError, KeyError, TypeError):
                            retry = 120
                    self.blocked_host[host] = time.time() + max(30, min(retry, 86400))
                    raise DataUnavailable("http_429")
                if response.status_code >= 400:
                    if response.status_code == 403:
                        self.blocked_host[host] = time.time() + 3600
                    raise DataUnavailable("http_" + str(response.status_code))
                if len(response.content) > 8_000_000:
                    raise DataUnavailable("response_too_large")
                try:
                    value = response.content if raw else response.json()
                except (ValueError, UnicodeError):
                    raise DataUnavailable("invalid_response") from None
                if len(self.cache) >= 500:
                    self.cache.pop(next(iter(self.cache)))
                fetched = time.time()
                self.cache[key] = (fetched + ttl, value, fetched)
                return value

    async def market(self, asset: dict) -> dict:
        url = "https://api.coingecko.com/api/v3/coins/" + asset["coingecko_id"]
        raw = await self.fetch(url, {"localization": "false", "tickers": "false", "community_data": "false",
                                     "developer_data": "false"}, ttl=300)
        m = raw.get("market_data", {})
        identity = (raw.get("id") == asset["coingecko_id"] and str(raw.get("symbol", "")).upper() == asset["symbol"]
                    and str(raw.get("platforms", {}).get(asset["chain"], "")).lower() == asset["contract"])
        if not identity:
            raise DataUnavailable("asset_identity_mismatch")
        price = positive(m.get("current_price", {}).get("usd"), "price")
        circulating, total = number(m.get("circulating_supply")), number(m.get("total_supply"))
        return result("CoinGecko", "https://www.coingecko.com/en/coins/" + asset["coingecko_id"],
                      timestamp(m.get("last_updated") or raw.get("last_updated")), {
                          "identity_verified": identity, "price": price,
                          "market_cap": positive(m.get("market_cap", {}).get("usd"), "market_cap"),
                          "fdv": number(m.get("fully_diluted_valuation", {}).get("usd")),
                          "total_supply_value": total * price if total is not None else None,
                          "circulating_supply": circulating, "total_supply": total, "max_supply": number(m.get("max_supply")),
                          "float_ratio": circulating / total if circulating is not None and total and total > 0 else None,
                          "volume_24h": number(m.get("total_volume", {}).get("usd")),
                          "change_24h_pct": number(m.get("price_change_percentage_24h")),
                          "change_7d_pct": number(m.get("price_change_percentage_7d")),
                          "change_30d_pct": number(m.get("price_change_percentage_30d")),
                          "image": raw.get("image", {}).get("small"),
                      })

    async def spot(self, asset: dict) -> dict:
        base, symbol = "https://api.binance.com", asset["exchange_symbol"]
        ticker, depth, candles = await asyncio.gather(
            self.fetch(base + "/api/v3/ticker/24hr", {"symbol": symbol}, ttl=60),
            self.fetch(base + "/api/v3/depth", {"symbol": symbol, "limit": 100}, ttl=30),
            self.fetch(base + "/api/v3/klines", {"symbol": symbol, "interval": "1d", "limit": 300}, ttl=300))
        if ticker.get("symbol") != symbol:
            raise DataUnavailable("spot_symbol_mismatch")
        bids, asks = depth.get("bids", []), depth.get("asks", [])
        if not bids or not asks:
            raise DataUnavailable("empty_order_book")
        bid, ask = positive(bids[0][0], "bid"), positive(asks[0][0], "ask")
        if ask < bid:
            raise DataUnavailable("crossed_order_book")
        mid, now = (bid + ask) / 2, time.time()
        closed = [x for x in candles if len(x) >= 7 and int(x[6]) / 1000 <= now]
        closed.sort(key=lambda x: x[0])
        if (len(closed) < 30 or now - closed[-1][6] / 1000 > 172800
                or any(b[0] - a[0] != 86400000 for a, b in zip(closed, closed[1:]))):
            raise DataUnavailable("insufficient_or_gapped_daily_candles")
        closes = [positive(x[4], "close") for x in closed]
        deltas = [b - a for a, b in zip(closes[-15:], closes[-14:])]
        gains, losses = sum(max(d, 0) for d in deltas) / 14, sum(max(-d, 0) for d in deltas) / 14
        rsi = 50 if gains == losses == 0 else (100 if losses == 0 else 100 - 100 / (1 + gains / losses))
        high = max(positive(x[2], "high") for x in closed[-30:])
        bid_depth = sum(positive(p, "level_price") * positive(q, "level_qty", True) for p, q in bids if float(p) >= mid * .99)
        ask_depth = sum(positive(p, "level_price") * positive(q, "level_qty", True) for p, q in asks if float(p) <= mid * 1.01)
        return result("Binance Spot", "https://www.binance.com/en/trade/" + asset["symbol"] + "_USDT",
                      timestamp(ticker["closeTime"]), {"symbol": symbol, "price": positive(ticker["lastPrice"], "last"),
                      "bid": bid, "ask": ask, "spread_bps": (ask - bid) / mid * 10000,
                      "quote_volume_24h": positive(ticker.get("quoteVolume"), "quote_volume", True),
                      "depth_1pct_usdt": min(bid_depth, ask_depth), "bid_depth_usdt": bid_depth, "ask_depth_usdt": ask_depth,
                      "rsi14": rsi, "drawdown_30d_pct": (mid / high - 1) * 100,
                      "daily_closes": [{"at": x[6] / 1000, "price": float(x[4])} for x in closed[-30:]]},
                      ["深度为前 100 档中的 1% 区间下界；单次快照无法识别虚假挂单。", "USDT 报价未做美元脱锚调整。"])

    async def technicals(self, asset: dict) -> dict:
        url = "https://api.binance.com/api/v3/klines"
        params = [{"symbol": symbol, "interval": "1d", "limit": 300}
                  for symbol in (asset["exchange_symbol"], "BTCUSDT", "ETHUSDT")]
        raw = await asyncio.gather(*(self.fetch(url, p, ttl=300) for p in params))
        now = time.time()
        try:
            data = await asyncio.to_thread(technical_bundle, *(closed_daily_bars(rows, now) for rows in raw))
        except ValueError as exc:
            raise DataUnavailable(str(exc)) from None
        return result("Binance Spot / UTC daily", "https://www.binance.com/en/trade/" + asset["symbol"] + "_USDT",
                      min(self.fetched_at(url, p) for p in params), data,
                      ["仅已收盘 UTC 日线；日线因子每日变化，盘口与合约数据按采集周期刷新。",
                       "仅 Binance USDT 市场；主动买入占比不是净流入，成交量未剔除刷量。",
                       "BTC/ETH 使用同日窗口，USDT 报价未做美元脱锚调整。"])

    async def derivatives(self, asset: dict) -> dict:
        base, symbol = "https://fapi.binance.com", asset["exchange_symbol"]
        premium, oi, history, funding = await asyncio.gather(
            self.fetch(base + "/fapi/v1/premiumIndex", {"symbol": symbol}),
            self.fetch(base + "/fapi/v1/openInterest", {"symbol": symbol}),
            self.fetch(base + "/futures/data/openInterestHist", {"symbol": symbol, "period": "1h", "limit": 25}, ttl=300),
            self.fetch(base + "/fapi/v1/fundingRate", {"symbol": symbol, "limit": 2}, ttl=300))
        if premium.get("symbol") != symbol or oi.get("symbol") != symbol:
            raise DataUnavailable("derivative_symbol_mismatch")
        history = sorted(history, key=lambda x: int(x["timestamp"]))
        if (len(history) != 25 or any(b["timestamp"] - a["timestamp"] != 3600000
                                     for a, b in zip(history, history[1:]))):
            raise DataUnavailable("insufficient_oi_history")
        before, after = positive(history[0]["sumOpenInterest"], "oi_before"), positive(history[-1]["sumOpenInterest"], "oi_after", True)
        intervals = None
        if len(funding) >= 2:
            intervals = (int(funding[-1]["fundingTime"]) - int(funding[-2]["fundingTime"])) / 3600000
        observed = min(timestamp(oi["time"]), timestamp(premium["time"]), timestamp(history[-1]["timestamp"]))
        index_price = number(premium.get("indexPrice"))
        return result("Binance USD-M", "https://www.binance.com/en/futures/" + symbol, observed, {
            "open_interest": positive(oi["openInterest"], "open_interest", True),
            "open_interest_usdt": positive(oi["openInterest"], "open_interest", True) * positive(premium["markPrice"], "mark_price"),
            "oi_change_24h_pct": (after / before - 1) * 100,
            "basis_bps": (positive(premium["markPrice"], "mark_price") / index_price - 1) * 10000 if index_price and index_price > 0 else None,
            "funding_bps": number(premium.get("lastFundingRate")) * 10000 if number(premium.get("lastFundingRate")) is not None else None,
            "observed_funding_interval_hours": intervals, "next_funding_at": timestamp(premium["nextFundingTime"]),
        }, ["仅覆盖 Binance USD-M，不代表全市场持仓。资金费为本期费率，不假定固定 8 小时。"])

    async def holders(self, asset: dict) -> dict:
        base, address = CHAINS[asset["chain"]]["explorer"], asset["contract"]
        token, raw = await asyncio.gather(self.fetch(base + "/api/v2/tokens/" + address, ttl=300),
                                       self.fetch(base + "/api/v2/tokens/" + address + "/holders", ttl=300))
        decimals = int(token["decimals"])
        if (not 0 <= decimals <= 36 or str(token.get("symbol", "")).upper() != asset["symbol"]
                or str(token.get("address_hash", address)).lower() != address):
            raise DataUnavailable("token_metadata_mismatch")
        scale = Decimal(10) ** decimals
        supply = Decimal(str(token["total_supply"])) / scale
        if supply <= 0:
            raise DataUnavailable("invalid_onchain_supply")
        items = []
        for row in raw.get("items", [])[:50]:
            addr = row["address"]["hash"].lower()
            if addr in ("0x" + "0" * 40, "0x" + "0" * 36 + "dead"):
                continue
            balance = Decimal(str(row["value"])) / scale
            if not balance.is_finite() or balance < 0:
                raise DataUnavailable("invalid_holder_balance")
            items.append({"address": addr, "balance": str(balance), "share": float(balance / supply),
                          "label": row["address"].get("name"), "is_contract": bool(row["address"].get("is_contract"))})
        if not items:
            raise DataUnavailable("empty_holders")
        observed = min(self.fetched_at(base + "/api/v2/tokens/" + address),
                       self.fetched_at(base + "/api/v2/tokens/" + address + "/holders"))
        return result("Blockscout", base + "/token/" + address + "?tab=holders", observed,
                      {"holders": items, "top10_share": sum(x["share"] for x in items[:10]), "total_supply": str(supply),
                       "sample_size": len(items), "paginated": bool(raw.get("next_page_params"))},
                      ["前 50 个地址样本；索引器快照没有固定区块号，可能滞后。交易所与桥未自动剔除。"])

    async def transfers(self, asset: dict, market: dict) -> dict:
        base, address = CHAINS[asset["chain"]]["explorer"], asset["contract"]
        raw = await self.fetch(base + "/api/v2/tokens/" + address + "/transfers", ttl=180)
        labels = {x["address"]: x for x in asset.get("wallet_labels", [])}
        price = market.get("data", {}).get("price") if source_fresh(market, "market", time.time()) else None
        items, seen = [], set()
        for row in raw.get("items", [])[:100]:
            identity = (row["transaction_hash"], row.get("log_index"))
            if identity in seen:
                continue
            seen.add(identity)
            amount = Decimal(str(row["total"]["value"])) / Decimal(10) ** int(row["total"]["decimals"])
            if not amount.is_finite() or amount < 0:
                raise DataUnavailable("invalid_transfer_amount")
            sender, recipient = row["from"]["hash"].lower(), row["to"]["hash"].lower()
            items.append({"tx_hash": row["transaction_hash"], "log_index": row.get("log_index"),
                          "from": sender, "to": recipient, "amount": str(amount),
                          "value_usd": float(amount) * price if price else None,
                          "timestamp": timestamp(row["timestamp"]), "from_label": labels.get(sender, {}).get("label"),
                          "to_label": labels.get(recipient, {}).get("label"), "to_kind": labels.get(recipient, {}).get("kind", "unknown"),
                          "url": base + "/tx/" + row["transaction_hash"]})
        return result("Blockscout", base + "/token/" + address + "?tab=token_transfers",
                      self.fetched_at(base + "/api/v2/tokens/" + address + "/transfers"),
                      {"items": items, "truncated": bool(raw.get("next_page_params"))},
                      ["仅最近一页转账，不是完整净流量；美元额按当前参考价估算。交易所标签由用户维护。"])

    async def rpc(self, chain: str, calls: list[tuple[str, str, str, list, list]], block: str) -> dict:
        body = []
        for i, (_, address, signature, types, args) in enumerate(calls):
            calldata = keccak(text=signature)[:4] + encode(types, args)
            body.append({"jsonrpc": "2.0", "id": i, "method": "eth_call",
                         "params": [{"to": address, "data": "0x" + calldata.hex()}, block]})
        raw = await self.fetch(CHAINS[chain]["rpc"], body=body, ttl=600)
        if not isinstance(raw, list):
            raise DataUnavailable("invalid_rpc_batch")
        by_id = {r.get("id"): r for r in raw}
        return {label: by_id.get(i, {}).get("result") for i, (label, *_) in enumerate(calls)}

    async def events(self, chain: str, address: str, event_name: str, height: int) -> list[dict]:
        chain_def = CHAINS[chain]
        raw = await self.fetch(chain_def["explorer"] + "/api", {
            "module": "logs", "action": "getLogs", "fromBlock": 0, "toBlock": height,
            "address": address, "topic0": "0x" + keccak(text=EVENTS[event_name]).hex()}, ttl=1800)
        items = raw.get("result")
        if not isinstance(items, list) or len(items) >= 1000:
            raise DataUnavailable("incomplete_event_history")
        output = []
        for item in items:
            at = int(item["timeStamp"], 16)
            if 0 <= time.time() - at <= 90 * 86400:
                topics = [x for x in item["topics"] if x]
                output.append({"id": item["transactionHash"] + ":" + str(item["logIndex"]),
                               "event": event_name, "contract": address, "timestamp": at,
                               "tx_hash": item["transactionHash"], "topics": topics, "data": item["data"],
                               "url": chain_def["scan"] + "/tx/" + item["transactionHash"]})
        return output

    async def governance(self, asset: dict) -> dict:
        chain, token = asset["chain"], asset["contract"]
        cfg = CHAINS[chain]
        raw = await self.fetch(cfg["rpc"], body={"jsonrpc": "2.0", "id": 1, "method": "eth_getBlockByNumber",
                                               "params": ["finalized", False]}, ttl=600)
        block = raw.get("result")
        if not isinstance(block, dict):
            raise DataUnavailable("finalized_block_unavailable")
        height, at = int(block["number"], 16), int(block["timestamp"], 16)
        calls = [("owner", token, "owner()", [], []), ("paused", token, "paused()", [], [])]
        if asset.get("governor"):
            calls.extend([("guardian", asset["governor"], "VETO_GUARDIAN()", [], []),
                          ("timelock", asset["governor"], "timelock()", [], [])])
        values = await self.rpc(chain, calls, block["number"])

        def unpack(key, typ="address"):
            value = values.get(key)
            if not value or value == "0x":
                return None
            try:
                return decode([typ], bytes.fromhex(value[2:]))[0]
            except (ValueError, OverflowError, DecodingError):
                return None

        owner, guardian, timelock = unpack("owner"), unpack("guardian"), unpack("timelock")
        output = {"owner": owner, "guardian": guardian, "timelock": timelock, "paused": unpack("paused", "bool"),
                  "owner_kind": "unknown", "safes": [], "events": [], "block": height,
                  "control_verified": False, "min_delay_seconds": None, "event_errors": []}

        async def append_events(address, event):
            try:
                output["events"].extend(await self.events(chain, address, event, height))
            except (DataUnavailable, ValueError, KeyError, TypeError):
                output["event_errors"].append({"address": address, "event": event, "error": "event_history_unavailable"})

        if asset.get("governor") and (guardian is None or timelock is None):
            output["event_errors"].append({"error": "configured_governor_unverified"})
        if owner == "0x" + "0" * 40:
            output.update(owner_kind="renounced", control_verified=True)
        for address, role in dict((x, r) for x, r in [(owner, "owner"), (guardian, "veto_guardian")] if x).items():
            safe_values = await self.rpc(chain, [("owners", address, "getOwners()", [], []),
                                                ("threshold", address, "getThreshold()", [], [])], block["number"])
            if all(safe_values.get(k) not in (None, "0x") for k in ("owners", "threshold")):
                owners = list(decode(["address[]"], bytes.fromhex(safe_values["owners"][2:]))[0])
                threshold = int(decode(["uint256"], bytes.fromhex(safe_values["threshold"][2:]))[0])
                if not 0 < threshold <= len(owners) <= 50:
                    raise DataUnavailable("invalid_multisig_configuration")
                output["safes"].append({"address": address, "role": role, "owners": owners, "threshold": threshold,
                                        "url": cfg["scan"] + "/address/" + address})
                if role == "owner":
                    output.update(owner_kind="multisig", control_verified=True)
                for event in ("AddedOwner", "RemovedOwner", "ChangedThreshold"):
                    await append_events(address, event)
            elif role == "owner" and address != "0x" + "0" * 40:
                code = await self.fetch(cfg["rpc"], body={"jsonrpc": "2.0", "id": 1, "method": "eth_getCode",
                                                         "params": [address, block["number"]]}, ttl=600)
                if code.get("result") == "0x":
                    output.update(owner_kind="eoa", control_verified=True)
        if timelock:
            reads = await self.rpc(chain, [("delay", timelock, "getMinDelay()", [], []),
                       ("admin", token, "hasRole(bytes32,address)", ["bytes32", "address"], [b"\0" * 32, timelock]),
                       ("upgrade", token, "hasRole(bytes32,address)", ["bytes32", "address"], [keccak(text="UPGRADER_ROLE"), timelock])], block["number"])
            if reads.get("delay") not in (None, "0x"):
                output["min_delay_seconds"] = int(decode(["uint256"], bytes.fromhex(reads["delay"][2:]))[0])
                has_admin = reads.get("admin") not in (None, "0x") and decode(["bool"], bytes.fromhex(reads["admin"][2:]))[0]
                has_upgrade = reads.get("upgrade") not in (None, "0x") and decode(["bool"], bytes.fromhex(reads["upgrade"][2:]))[0]
                if owner == timelock or (has_admin and has_upgrade):
                    output.update(owner_kind="timelock", control_verified=True)
                for target in (timelock, token):
                    for event in ("RoleGranted", "RoleRevoked"):
                        await append_events(target, event)
        await append_events(token, "OwnershipTransferred")
        output["events"].sort(key=lambda x: x["timestamp"], reverse=True)
        envelope = result("Ethereum RPC / Blockscout", cfg["scan"] + "/address/" + token, at, output,
                      ["只识别 Owner、已配置 Governor 和标准 Safe；不等于完整安全审计或独立签名人身份核验。",
                       "权限事件来自已执行交易，提案标题不作为权限转移证据。"])
        if output["event_errors"]:
            envelope.update(status="partial", error="governance_history_incomplete")
            envelope["warnings"].append("部分权限历史无法读取，当前权限快照不能证明近期无变更。")
        return envelope

    async def news(self, asset: dict) -> dict:
        reports = await asyncio.gather(*(self.fetch(url, ttl=600, raw=True) for _, url in FEEDS), return_exceptions=True)
        items, successful, seen, times = [], 0, set(), []
        query = re.compile(re.escape(asset["name"]) + r"|\$" + re.escape(asset["symbol"]) + r"\b", re.I)
        risk_terms = re.compile(r"\b(hack(?:ed)?|exploit|delist(?:ing|ed)?|unlock|breach|rug.pull|compromised)\b", re.I)
        for (publisher, feed_url), body in zip(FEEDS, reports):
            if isinstance(body, BaseException):
                continue
            try:
                root = ElementTree.fromstring(body)
            except Exception:
                continue
            successful += 1
            times.append(self.fetched_at(feed_url, raw=True))
            for item in root.findall(".//item")[:150]:
                title, link, published = item.findtext("title") or "", item.findtext("link") or "", item.findtext("pubDate")
                if not query.search(title) or link in seen or urlparse(link).scheme not in ("https", "http"):
                    continue
                try:
                    at = parsedate_to_datetime(published).timestamp()
                except (ValueError, TypeError, AttributeError):
                    continue
                if not 0 <= time.time() - at <= 7 * 86400:
                    continue
                seen.add(link)
                items.append({"title": title[:500], "url": link, "publisher": publisher, "published_at": at,
                              "risk_terms": sorted(set(x.lower() for x in risk_terms.findall(title)))})
        if not successful:
            raise DataUnavailable("news_feeds_unavailable")
        return result("Public RSS", FEEDS[0][1], min(times),
                      {"items": sorted(items, key=lambda x: x["published_at"], reverse=True)[:30],
                       "feeds_available": successful, "feeds_total": len(FEEDS)},
                      ["按项目全名或 $代号匹配标题；关键词只表示需要复核，不能证明事件属实。"])

    async def collect(self, asset: dict, previous: dict | None = None) -> dict:
        async def guarded(key, task):
            try:
                return key, await task
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                old = (previous or {}).get("sources", {}).get(key, {})
                return key, {"status": "error", "error": str(exc) if isinstance(exc, DataUnavailable) else "invalid_provider_data",
                             "source": old.get("source", key), "url": old.get("url"),
                             "observed_at": old.get("observed_at"), "collected_at": time.time(), "data": old.get("data", {})}
        pairs = await asyncio.gather(*(guarded(k, fn(asset)) for k, fn in [
            ("market", self.market), ("spot", self.spot), ("derivatives", self.derivatives),
            ("holders", self.holders), ("governance", self.governance), ("news", self.news),
            ("technicals", self.technicals)]))
        sources = dict(pairs)
        key, source = await guarded("transfers", self.transfers(asset, sources["market"]))
        sources[key] = source
        return sources
