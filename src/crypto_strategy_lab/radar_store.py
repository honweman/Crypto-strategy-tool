from __future__ import annotations

import json
import secrets
import time
from typing import Optional

from .radar import DEFAULT_ASSETS, RadarAsset, RadarSettings
from .store import Database


class RadarStore:
    def __init__(self, db: Database):
        self.db = db
        with db.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS radar_settings (
                    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                    config TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS radar_watches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    asset TEXT NOT NULL,
                    identity TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    next_due REAL NOT NULL DEFAULT 0,
                    last_started REAL NOT NULL DEFAULT 0,
                    last_finished REAL,
                    lease_token TEXT,
                    lease_until REAL NOT NULL DEFAULT 0,
                    last_error TEXT,
                    UNIQUE(user_id, identity)
                );
                CREATE TABLE IF NOT EXISTS radar_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    watch_id INTEGER NOT NULL REFERENCES radar_watches(id) ON DELETE CASCADE,
                    created_at REAL NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS radar_snapshots_watch_time
                    ON radar_snapshots(watch_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS radar_signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    watch_id INTEGER NOT NULL REFERENCES radar_watches(id) ON DELETE CASCADE,
                    created_at REAL NOT NULL,
                    status TEXT NOT NULL,
                    evidence TEXT NOT NULL,
                    outcomes TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS radar_signals_watch_time
                    ON radar_signals(watch_id, created_at DESC);
            """)

    def ensure_user(self, user_id: int):
        with self.db.connect() as conn:
            cursor = conn.execute("INSERT OR IGNORE INTO radar_settings VALUES (?, ?, ?)",
                                  (user_id, json.dumps(RadarSettings().model_dump()), time.time()))
            if cursor.rowcount:
                for asset in DEFAULT_ASSETS:
                    parsed = RadarAsset(**asset).model_dump()
                    conn.execute("INSERT OR IGNORE INTO radar_watches(user_id, asset, identity) VALUES (?, ?, ?)",
                                 (user_id, json.dumps(parsed), self.identity(parsed)))

    @staticmethod
    def identity(asset: dict) -> str:
        return asset["chain"] + ":" + asset["contract"].lower()

    def settings(self, user_id: int) -> dict:
        self.ensure_user(user_id)
        with self.db.connect() as conn:
            row = conn.execute("SELECT config FROM radar_settings WHERE user_id=?", (user_id,)).fetchone()
        return RadarSettings(**json.loads(row["config"])).model_dump()

    def update_settings(self, user_id: int, config: dict) -> dict:
        value = RadarSettings(**config).model_dump()
        self.ensure_user(user_id)
        with self.db.connect() as conn:
            conn.execute("UPDATE radar_settings SET config=?, updated_at=? WHERE user_id=?",
                         (json.dumps(value), time.time(), user_id))
            conn.execute("UPDATE radar_watches SET revision=revision+1, next_due=0 WHERE user_id=?", (user_id,))
        return value

    def watches(self, user_id: int) -> list[dict]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM radar_watches WHERE user_id=? ORDER BY id", (user_id,)).fetchall()
        return [self.public_watch(row) for row in rows]

    def watch(self, watch_id: int, user_id: int) -> Optional[dict]:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM radar_watches WHERE id=? AND user_id=?", (watch_id, user_id)).fetchone()
        return self.public_watch(row) if row else None

    @staticmethod
    def public_watch(row) -> dict:
        result = dict(row)
        result["asset"] = json.loads(result["asset"])
        result["enabled"] = bool(result["enabled"])
        result["updating"] = result["lease_until"] > time.time()
        result.pop("lease_token", None)
        return result

    def save_watch(self, user_id: int, asset: dict, watch_id: Optional[int] = None) -> dict:
        value = RadarAsset(**asset).model_dump()
        self.ensure_user(user_id)
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if watch_id is None:
                count = conn.execute("SELECT COUNT(*) FROM radar_watches WHERE user_id=?", (user_id,)).fetchone()[0]
                if count >= 20:
                    raise ValueError("每个用户最多观察 20 个币种")
                cursor = conn.execute("INSERT INTO radar_watches(user_id, asset, identity, enabled) VALUES (?, ?, ?, ?)",
                                      (user_id, json.dumps(value), self.identity(value), int(value["enabled"])))
                watch_id = cursor.lastrowid
            else:
                old = conn.execute("SELECT * FROM radar_watches WHERE id=? AND user_id=?", (watch_id, user_id)).fetchone()
                if not old:
                    raise KeyError("watch_not_found")
                # An identity change needs a new watch: old observations must not become another token's history.
                if old["identity"] != self.identity(value):
                    raise ValueError("修改链或合约地址时请新建观察项")
                conn.execute("""UPDATE radar_watches SET asset=?, enabled=?, revision=revision+1,
                             next_due=0 WHERE id=? AND user_id=?""",
                             (json.dumps(value), int(value["enabled"]), watch_id, user_id))
        return self.watch(watch_id, user_id)

    def delete_watch(self, watch_id: int, user_id: int) -> bool:
        with self.db.connect() as conn:
            return bool(conn.execute("DELETE FROM radar_watches WHERE id=? AND user_id=?", (watch_id, user_id)).rowcount)

    def request_refresh(self, user_id: int, watch_id: Optional[int] = None) -> int:
        now = time.time()
        with self.db.connect() as conn:
            cursor = conn.execute("""UPDATE radar_watches SET next_due=0 WHERE user_id=? AND enabled=1
                                  AND last_started <= ? AND lease_until <= ? AND (? IS NULL OR id=?)""",
                                  (user_id, now - 60, now, watch_id, watch_id))
        return cursor.rowcount

    def claim(self, permitted_user_ids: list[int], now: Optional[float] = None) -> Optional[dict]:
        now = time.time() if now is None else now
        if not permitted_user_ids:
            return None
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute("""SELECT w.*, s.config FROM radar_watches w JOIN radar_settings s
                                 ON s.user_id=w.user_id JOIN users u ON u.id=w.user_id
                                 WHERE w.enabled=1 AND u.status='active' AND w.next_due<=? AND w.lease_until<=?
                                 ORDER BY w.next_due, w.id""", (now, now)).fetchall()
            row = next((r for r in rows if r["user_id"] in permitted_user_ids and json.loads(r["config"])["enabled"]), None)
            if row is None:
                return None
            token = secrets.token_hex(16)
            conn.execute("UPDATE radar_watches SET lease_token=?, lease_until=?, last_started=? WHERE id=?",
                         (token, now + 240, now, row["id"]))
            job = self.public_watch(row)
            job.update(lease_token=token, settings=RadarSettings(**json.loads(row["config"])).model_dump())
            return job

    def release(self, job: dict, error: Optional[str] = None):
        with self.db.connect() as conn:
            conn.execute("""UPDATE radar_watches SET lease_token=NULL, lease_until=0, last_error=?,
                         next_due=CASE WHEN revision=? THEN ? ELSE 0 END
                         WHERE id=? AND lease_token=?""",
                         (error, job["revision"], time.time() + max(60, job["settings"]["interval_seconds"]), job["id"], job["lease_token"]))

    def latest(self, watch_id: int) -> Optional[dict]:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM radar_snapshots WHERE watch_id=? ORDER BY id DESC LIMIT 1", (watch_id,)).fetchone()
        if not row:
            return None
        return {"id": row["id"], "created_at": row["created_at"], **json.loads(row["payload"])}

    def holder_baseline(self, watch_id: int, observed_at: float) -> Optional[dict]:
        with self.db.connect() as conn:
            rows = conn.execute("""SELECT payload FROM radar_snapshots WHERE watch_id=? AND created_at>=?
                                 AND created_at<=? ORDER BY ABS(created_at-?) LIMIT 100""",
                                (watch_id, observed_at - 172800, observed_at - 3600, observed_at - 86400)).fetchall()
        for row in rows:
            source = json.loads(row["payload"]).get("sources", {}).get("holders", {})
            age = observed_at - (source.get("observed_at") or observed_at)
            if source.get("status") == "ok" and 3600 <= age <= 172800:
                return source
        return None

    def finish(self, job: dict, payload: dict) -> tuple[bool, Optional[dict]]:
        now, alert = time.time(), None
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM radar_watches WHERE id=? AND lease_token=? AND revision=? AND enabled=1",
                               (job["id"], job["lease_token"], job["revision"])).fetchone()
            if not row:
                return False, None
            conn.execute("INSERT INTO radar_snapshots(watch_id, created_at, payload) VALUES (?, ?, ?)",
                         (job["id"], now, json.dumps(payload, allow_nan=False)))
            status = payload["evaluation"]["status"]
            last = conn.execute("SELECT * FROM radar_signals WHERE watch_id=? ORDER BY id DESC LIMIT 1", (job["id"],)).fetchone()
            if status in ("candidate", "avoid") and (not last or last["status"] != status or now - last["created_at"] >= 86400):
                evidence = {**payload, "asset": job["asset"], "settings": job["settings"]}
                cursor = conn.execute("INSERT INTO radar_signals(watch_id, created_at, status, evidence) VALUES (?, ?, ?, ?)",
                                      (job["id"], now, status, json.dumps(evidence, allow_nan=False)))
                alert = {"id": cursor.lastrowid, "status": status, "asset": job["asset"], "evaluation": payload["evaluation"]}
            conn.execute("""UPDATE radar_watches SET lease_token=NULL, lease_until=0, last_finished=?,
                         last_error=NULL, next_due=? WHERE id=?""",
                         (now, now + job["settings"]["interval_seconds"], job["id"]))
            self._mark_outcomes(conn, job["id"], payload, now)
            conn.execute("DELETE FROM radar_snapshots WHERE watch_id=? AND created_at<?", (job["id"], now - 30 * 86400))
            conn.execute("""DELETE FROM radar_snapshots WHERE watch_id=? AND id NOT IN
                         (SELECT id FROM radar_snapshots WHERE watch_id=? ORDER BY id DESC LIMIT 3000)""",
                         (job["id"], job["id"]))
            conn.execute("DELETE FROM radar_signals WHERE watch_id=? AND created_at<?", (job["id"], now - 180 * 86400))
        return True, alert

    @staticmethod
    def _mark_outcomes(conn, watch_id: int, payload: dict, now: float):
        spot = payload["sources"].get("spot", {})
        if (not payload["evaluation"]["fresh_sources"].get("spot")
                or not payload["evaluation"]["fresh_sources"].get("market")
                or payload["sources"].get("market", {}).get("data", {}).get("identity_verified") is not True):
            return
        exit_bid = spot.get("data", {}).get("bid")
        observed = spot.get("observed_at", now)
        if not exit_bid or exit_bid <= 0:
            return
        rows = conn.execute("SELECT * FROM radar_signals WHERE watch_id=? AND status='candidate' AND created_at>?",
                            (watch_id, now - 180 * 86400)).fetchall()
        for row in rows:
            evidence, outcomes = json.loads(row["evidence"]), json.loads(row["outcomes"])
            if evidence["asset"]["exchange_symbol"] != payload.get("asset", {}).get("exchange_symbol"):
                continue
            settings = evidence["settings"]
            slip, fee = settings["slippage_bps"] / 10000, settings["fee_bps"] / 10000
            entry = evidence["sources"].get("spot", {}).get("data", {}).get("ask")
            if not entry or entry <= 0:
                continue
            for label, seconds in (("1h", 3600), ("24h", 86400), ("7d", 604800)):
                if label in outcomes or observed < row["created_at"] + seconds:
                    continue
                delay = observed - row["created_at"] - seconds
                if delay > min(900, max(120, settings["interval_seconds"] * 2)):
                    outcomes[label] = {"status": "missed", "reason": "no_fresh_quote_in_window"}
                else:
                    net = exit_bid * (1 - slip) * (1 - fee) / (entry * (1 + slip) * (1 + fee)) - 1
                    outcomes[label] = {"status": "observed", "net_return_pct": net * 100,
                                       "price": exit_bid, "observed_at": observed, "delay_seconds": delay}
            conn.execute("UPDATE radar_signals SET outcomes=? WHERE id=?", (json.dumps(outcomes), row["id"]))

    def history(self, watch_id: int, user_id: int, limit: int = 100) -> list[dict]:
        if not self.watch(watch_id, user_id):
            raise KeyError("watch_not_found")
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM radar_snapshots WHERE watch_id=? ORDER BY id DESC LIMIT ?",
                                (watch_id, limit)).fetchall()
        result = []
        for row in reversed(rows):
            payload = json.loads(row["payload"])
            result.append({"created_at": row["created_at"], "evaluation": payload["evaluation"],
                           "price": payload["sources"].get("spot", {}).get("data", {}).get("price")})
        return result

    def signals(self, user_id: int, limit: int = 100) -> list[dict]:
        with self.db.connect() as conn:
            rows = conn.execute("""SELECT s.*, w.asset FROM radar_signals s JOIN radar_watches w ON w.id=s.watch_id
                                  WHERE w.user_id=? ORDER BY s.id DESC LIMIT ?""", (user_id, limit)).fetchall()
        return [{**dict(row), "asset": json.loads(row["asset"]), "evidence": json.loads(row["evidence"]),
                 "outcomes": json.loads(row["outcomes"])} for row in rows]
