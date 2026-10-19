from __future__ import annotations

import sqlite3
import time

from fastapi import Depends, HTTPException, Query

from .permissions import MANAGE_RADAR, VIEW_RADAR
from .radar import RULE_VERSION, SOURCE_TTLS, RadarAsset, RadarSettings, evaluate
from .radar_factors import DEFERRED_FACTORS, GROUPS


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
