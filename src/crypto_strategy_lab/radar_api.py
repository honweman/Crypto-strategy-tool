from __future__ import annotations

import sqlite3
import time

from fastapi import Depends, HTTPException, Query

from .permissions import MANAGE_RADAR, VIEW_RADAR
from .radar import RULE_VERSION, SOURCE_TTLS, RadarAsset, RadarSettings, evaluate
from .radar_factors import DEFERRED_FACTORS, GROUPS
from .radar_discovery import DiscoverySettings
from .radar_sources import DataUnavailable


def register_radar_routes(app, service, require):
    store = service.store
    read, write = require(VIEW_RADAR), require(MANAGE_RADAR)

    def owned(watch_id, user):
        watch = store.watch(watch_id, user["id"])
        if not watch:
            raise HTTPException(404, "watch_not_found")
        return watch

    def audit(user, action, entity_id=None):
        service.db.create_audit_log(user_id=user["id"], action="radar." + action,
                                    entity_type="radar_watch", entity_id=entity_id, status="ok", detail={})

    def snapshot(watch, settings):
        latest = store.latest(watch["id"])
        if latest:
            # Re-evaluate freshness on every read; a stopped worker must never leave a green stale signal.
            latest["evaluation"] = evaluate(latest["sources"], settings, latest.get("holder_baseline"))
            if latest.get("asset") != watch["asset"]:
                latest["evaluation"] = evaluate({}, settings)
                latest["evaluation"]["status"] = "incomplete"
                latest["evaluation"]["blockers"].append("watch_configuration_changed")
            if not watch["enabled"] or not settings["enabled"]:
                latest["evaluation"]["status"] = "paused"
            return latest
        return None

    @app.get("/api/radar")
    def overview(user=Depends(read)):
        settings = store.settings(user["id"])
        return {"settings": settings, "worker": service.status(), "rule_version": RULE_VERSION,
                "server_time": time.time(), "source_ttls": SOURCE_TTLS,
                "factor_groups": GROUPS, "deferred_factors": DEFERRED_FACTORS,
                "watches": [{**w, "snapshot": snapshot(w, settings)} for w in store.watches(user["id"])]}

    @app.get("/api/radar/settings")
    def settings(user=Depends(read)):
        return store.settings(user["id"])

    @app.get("/api/radar/discovery")
    def discovery(user=Depends(read)):
        result = service.discovery.view(user["id"])
        result["worker_running"] = bool(service.discovery_task and not service.discovery_task.done())
        return result

    @app.put("/api/radar/discovery/settings")
    def discovery_settings(payload: DiscoverySettings, user=Depends(write), reader=Depends(read)):
        value = service.discovery.update_settings(user["id"], payload.model_dump())
        audit(user, "discovery.settings")
        return value

    @app.get("/api/radar/discovery/history")
    def discovery_history(limit: int = Query(20, ge=1, le=100), before_id: int = Query(None, ge=1), user=Depends(read)):
        rows = service.discovery.history(user["id"], limit, before_id)
        return {"items": rows, "next_before_id": rows[-1]["id"] if len(rows) == limit else None,
                "retention_days": 30, "max_runs": 3000}

    @app.get("/api/radar/discovery/history/{run_id}")
    def discovery_historical(run_id: int, user=Depends(read)):
        result = service.discovery.historical(user["id"], run_id)
        if result is None:
            raise HTTPException(404, "discovery_history_not_found")
        return result

    @app.post("/api/radar/discovery/refresh", status_code=202)
    def discovery_refresh(user=Depends(write), reader=Depends(read)):
        if not service.discovery.settings(user["id"])["enabled"]:
            raise HTTPException(409, "请先启用自动扫描")
        if not service.discovery_task or service.discovery_task.done():
            raise HTTPException(503, "后台扫描服务未运行")
        queued = service.discovery.refresh()
        audit(user, "discovery.refresh")
        return {"queued": queued, "message": "已加入扫描队列" if queued else "正在扫描或处于 60 秒冷却期"}

    @app.post("/api/radar/discovery/prepare/{coin_id}")
    async def discovery_prepare(coin_id: str, user=Depends(write), reader=Depends(read)):
        selected = next((row for row in service.discovery.view(user["id"])["items"] if row["coingecko_id"] == coin_id), None)
        if not selected:
            raise HTTPException(404, "币种不在当前有效筛选结果内，请等待扫描或调整条件")
        if service.collector is None:
            raise HTTPException(503, "后台采集服务未运行")
        try:
            raw = await service.collector.fetch("https://api.coingecko.com/api/v3/coins/" + coin_id,
                {"localization": "false", "tickers": "false", "community_data": "false", "developer_data": "false"}, ttl=300)
            if not isinstance(raw, dict) or raw.get("id") != coin_id or str(raw.get("symbol", "")).upper() != selected["symbol"]:
                raise ValueError("币种身份未核验")
            platforms = raw.get("platforms", {})
            if not isinstance(platforms, dict):
                raise ValueError("币种合约信息无效")
            chain = next((key for key in ("ethereum", "base") if platforms.get(key)), None)
            if not chain:
                raise ValueError("此币种尚不支持链上深度观察；当前仅支持 Ethereum / Base 代币")
            pair = selected["symbol"] + "USDT"
            listing = await service.collector.fetch("https://api.binance.com/api/v3/exchangeInfo", {"symbol": pair}, ttl=900)
            pairs = listing.get("symbols") if isinstance(listing, dict) else None
            if not isinstance(pairs, list) or not any(isinstance(s, dict) and s.get("symbol") == pair and s.get("status") == "TRADING" and s.get("isSpotTradingAllowed") is True for s in pairs):
                raise ValueError("此币种没有可用的 Binance USDT 现货，暂不能加入深度观察")
            asset = RadarAsset(symbol=selected["symbol"], name=raw["name"], coingecko_id=coin_id,
                               chain=chain, contract=platforms[chain], exchange_symbol=pair).model_dump()
        except DataUnavailable:
            raise HTTPException(422, "数据源暂不可用或该币种不支持深度观察，请稍后重试") from None
        except (ValueError, KeyError, TypeError) as exc:
            raise HTTPException(422, str(exc)) from None
        return {"asset": asset}

    @app.put("/api/radar/settings")
    def save_settings(payload: RadarSettings, user=Depends(write), reader=Depends(read)):
        value = store.update_settings(user["id"], payload.model_dump())
        audit(user, "settings.update")
        return value

    @app.post("/api/radar/watches", status_code=201)
    def add_watch(payload: RadarAsset, user=Depends(write), reader=Depends(read)):
        try:
            watch = store.save_watch(user["id"], payload.model_dump())
        except sqlite3.IntegrityError:
            raise HTTPException(409, "该链和合约已在观察列表中") from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        audit(user, "watch.create", watch["id"])
        return watch

    @app.put("/api/radar/watches/{watch_id}")
    def update_watch(watch_id: int, payload: RadarAsset, user=Depends(write), reader=Depends(read)):
        owned(watch_id, user)
        try:
            watch = store.save_watch(user["id"], payload.model_dump(), watch_id)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        audit(user, "watch.update", watch_id)
        return watch

    @app.delete("/api/radar/watches/{watch_id}")
    def delete_watch(watch_id: int, user=Depends(write), reader=Depends(read)):
        owned(watch_id, user)
        store.delete_watch(watch_id, user["id"])
        audit(user, "watch.delete", watch_id)
        return {"ok": True}

    @app.post("/api/radar/refresh", status_code=202)
    def refresh(watch_id: int = Query(default=None), user=Depends(write), reader=Depends(read)):
        if watch_id is not None:
            owned(watch_id, user)
        if not store.settings(user["id"])["enabled"]:
            raise HTTPException(409, "请先启用自动观察")
        if not service.status()["running"]:
            raise HTTPException(503, "后台采集服务未运行")
        queued = store.request_refresh(user["id"], watch_id)
        audit(user, "refresh", watch_id)
        return {"queued": queued, "message": "已加入采集队列" if queued else "正在更新或处于 60 秒冷却期"}

    @app.get("/api/radar/signals")
    def signals(limit: int = Query(100, ge=1, le=200), user=Depends(read)):
        return {"items": store.signals(user["id"], limit)}

    @app.get("/api/radar/watches/{watch_id}/history")
    def history(watch_id: int, limit: int = Query(100, ge=1, le=500), user=Depends(read)):
        watch = owned(watch_id, user)
        return {"watch": watch, "items": store.history(watch_id, user["id"], limit)}
