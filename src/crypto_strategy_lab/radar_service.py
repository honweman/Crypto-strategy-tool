from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import suppress

from .permissions import MANAGE_RADAR, VIEW_RADAR, has_user_permission
from .radar import evaluate
from .radar_discovery import DiscoveryStore
from .radar_sources import PublicData
from .radar_store import RadarStore

logger = logging.getLogger(__name__)


class RadarService:
    def __init__(self, db, notify=None, collector=None):
        self.db = db
        self.store = RadarStore(db)
        self.discovery = DiscoveryStore(db)
        self.discovery_task = None
        self.collector = collector
        self.notify = notify
        self.task = None
        self.heartbeat = None
        self.last_error = None
        self.enabled = os.getenv("CRYPTO_LAB_RADAR_ENABLED", "1") == "1"

    @staticmethod
    def permitted(user):
        return (user and user.get("status") == "active" and has_user_permission(user, VIEW_RADAR)
                and has_user_permission(user, MANAGE_RADAR))

    def status(self):
        return {"enabled": self.enabled, "running": bool(self.task and not self.task.done()),
                "heartbeat": self.heartbeat, "last_error": self.last_error,
                "source_cooldowns": {host: until for host, until in
                    getattr(self.collector, "blocked_host", {}).items() if until > time.time()}
                    if isinstance(self.collector, PublicData) else {}}

    async def start(self):
        if not self.enabled or (self.task and not self.task.done()):
            return
        if self.collector is None:
            self.collector = PublicData()
        self.task = asyncio.create_task(self.run(), name="altcoin-observer")
        self.discovery_task = asyncio.create_task(self.run_discovery(), name="altcoin-discovery")

    async def close(self):
        if self.discovery_task:
            self.discovery_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.discovery_task
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
        if self.collector:
            await self.collector.close()

    async def run_discovery(self):
        while True:
            job = None
            try:
                ids = [u["id"] for u in self.db.list_users() if self.permitted(u)]
                job = self.discovery.claim(ids)
                if job:
                    previous, _ = self.discovery.snapshot()
                    payload = await asyncio.wait_for(self.collector.discover(profiles=job["profiles"], previous=previous), timeout=240)
                    allowed = {u["id"] for u in self.db.list_users()
                               if self.permitted(u) and self.discovery.settings(u["id"])["enabled"]}
                    job["profiles"] = [p for p in job["profiles"] if p["user_id"] in allowed]
                    if job["profiles"]:
                        self.discovery.finish(job, payload)
                    else:
                        self.discovery.finish(job, error="discovery_paused")
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                if job:
                    self.discovery.finish(job, error="worker_shutdown")
                raise
            except Exception:
                logger.exception("Universe discovery failed")
                if job:
                    self.discovery.finish(job, error="discovery_failed")
                await asyncio.sleep(15)

    async def run(self):
        while True:
            self.heartbeat = time.time()
            try:
                users = [u for u in self.db.list_users() if self.permitted(u)]
                for user in users:
                    self.store.ensure_user(user["id"])
                job = self.store.claim([u["id"] for u in users])
                if job:
                    await self.scan(job)
                else:
                    await asyncio.sleep(5)
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Radar worker cycle failed")
                self.last_error = "worker_cycle_failed"
                await asyncio.sleep(15)

    async def scan(self, job):
        error = None
        try:
            previous = self.store.latest(job["id"])
            sources = await asyncio.wait_for(self.collector.collect(job["asset"], previous), timeout=180)
            if not self.permitted(self.db.get_user(job["user_id"])):
                error = "permission_changed"
                return
            baseline = self.store.holder_baseline(job["id"], sources.get("holders", {}).get("observed_at") or time.time())
            payload = {"asset": job["asset"], "watch_revision": job["revision"], "sources": sources, "holder_baseline": baseline,
                       "evaluation": evaluate(sources, job["settings"], baseline)}
            saved, alert = self.store.finish(job, payload)
            if saved and alert and job["settings"]["notifications_enabled"] and self.notify:
                title = job["asset"]["symbol"] + (" 进入建仓观察候选" if alert["status"] == "candidate" else " 出现需回避/复核风险")
                self.notify(self.db, job["user_id"], "radar." + alert["status"], title,
                            "机会分 %s，风险分 %s。仅研究信号，未下单，不代表上涨概率。" %
                            (alert["evaluation"]["score"], alert["evaluation"]["risk_score"]), alert)
        except asyncio.CancelledError:
            error = "worker_shutdown"
            raise
        except asyncio.TimeoutError:
            error = "scan_timeout"
        except Exception:
            logger.exception("Radar scan failed for watch %s", job["id"])
            error = "scan_failed"
        finally:
            self.store.release(job, error)
            self.heartbeat = time.time()
