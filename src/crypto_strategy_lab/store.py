from __future__ import annotations

import json
import secrets
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .permissions import normalize_permissions, normalize_role, permissions_for_user
from .security import SecretBox, hash_password, hash_token, mask_secret


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Database:
    path: Path
    secret_box: SecretBox

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    permission_grants TEXT NOT NULL DEFAULT '[]',
                    permission_revokes TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    expires_at REAL NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS exchange_api_keys (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    exchange TEXT NOT NULL,
                    label TEXT NOT NULL,
                    api_key_enc TEXT NOT NULL,
                    api_secret_enc TEXT NOT NULL,
                    passphrase_enc TEXT NOT NULL,
                    scopes TEXT NOT NULL DEFAULT '[]',
                    can_trade INTEGER NOT NULL DEFAULT 0,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_used_at TEXT,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS strategy_configs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    strategy_type TEXT NOT NULL,
                    market_type TEXT NOT NULL DEFAULT 'spot',
                    parameters TEXT NOT NULL DEFAULT '{}',
                    current_version INTEGER NOT NULL DEFAULT 1,
                    is_default INTEGER NOT NULL DEFAULT 0,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS strategy_config_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy_config_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    version INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    strategy_type TEXT NOT NULL,
                    market_type TEXT NOT NULL DEFAULT 'spot',
                    parameters TEXT NOT NULL DEFAULT '{}',
                    change_note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    UNIQUE (strategy_config_id, version),
                    FOREIGN KEY (strategy_config_id) REFERENCES strategy_configs(id) ON DELETE CASCADE,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS trade_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    strategy_config_id INTEGER,
                    strategy_version INTEGER,
                    strategy_type TEXT NOT NULL,
                    run_mode TEXT NOT NULL DEFAULT 'backtest',
                    status TEXT NOT NULL DEFAULT 'completed',
                    market_type TEXT NOT NULL DEFAULT 'spot',
                    bars_file TEXT,
                    events_file TEXT,
                    parameters TEXT NOT NULL DEFAULT '{}',
                    metrics TEXT NOT NULL DEFAULT '{}',
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                    FOREIGN KEY (strategy_config_id) REFERENCES strategy_configs(id) ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS trade_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER,
                    user_id INTEGER NOT NULL,
                    strategy_config_id INTEGER,
                    run_mode TEXT NOT NULL DEFAULT 'backtest',
                    exchange TEXT NOT NULL DEFAULT 'simulated',
                    market_type TEXT NOT NULL DEFAULT 'spot',
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    price REAL NOT NULL,
                    notional REAL NOT NULL,
                    fee REAL NOT NULL DEFAULT 0,
                    order_id TEXT,
                    reason TEXT,
                    status TEXT NOT NULL DEFAULT 'filled',
                    traded_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES trade_runs(id) ON DELETE CASCADE,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                    FOREIGN KEY (strategy_config_id) REFERENCES strategy_configs(id) ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS trade_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_order_id TEXT NOT NULL UNIQUE,
                    exchange_order_id TEXT,
                    user_id INTEGER NOT NULL,
                    strategy_config_id INTEGER,
                    run_id INTEGER,
                    run_mode TEXT NOT NULL DEFAULT 'paper',
                    exchange TEXT NOT NULL DEFAULT 'paper',
                    market_type TEXT NOT NULL DEFAULT 'spot',
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    order_type TEXT NOT NULL DEFAULT 'market',
                    time_in_force TEXT NOT NULL DEFAULT 'GTC',
                    post_only INTEGER NOT NULL DEFAULT 0,
                    reduce_only INTEGER NOT NULL DEFAULT 0,
                    quantity REAL NOT NULL,
                    requested_price REAL,
                    limit_price REAL,
                    stop_price REAL,
                    trailing_delta_pct REAL,
                    leverage REAL NOT NULL DEFAULT 1,
                    filled_quantity REAL NOT NULL DEFAULT 0,
                    average_fill_price REAL,
                    status TEXT NOT NULL DEFAULT 'created',
                    reason TEXT,
                    error_message TEXT,
                    raw_response TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    submitted_at TEXT,
                    updated_at TEXT NOT NULL,
                    finished_at TEXT,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                    FOREIGN KEY (strategy_config_id) REFERENCES strategy_configs(id) ON DELETE SET NULL,
                    FOREIGN KEY (run_id) REFERENCES trade_runs(id) ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL DEFAULT '',
                    entity_id TEXT,
                    status TEXT NOT NULL DEFAULT 'ok',
                    detail TEXT NOT NULL DEFAULT '{}',
                    ip_address TEXT NOT NULL DEFAULT '',
                    user_agent TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS paper_accounts (
                    user_id INTEGER PRIMARY KEY,
                    cash REAL NOT NULL DEFAULT 10000,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS paper_positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    average_price REAL NOT NULL DEFAULT 0,
                    realized_pnl REAL NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    UNIQUE (user_id, symbol),
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS user_risk_settings (
                    user_id INTEGER PRIMARY KEY,
                    paper_trading_enabled INTEGER NOT NULL DEFAULT 1,
                    live_trading_enabled INTEGER NOT NULL DEFAULT 0,
                    max_order_notional REAL NOT NULL DEFAULT 2500,
                    max_daily_loss REAL NOT NULL DEFAULT 500,
                    max_symbol_weight REAL NOT NULL DEFAULT 0.35,
                    max_gross_exposure REAL NOT NULL DEFAULT 0.80,
                    max_open_positions INTEGER NOT NULL DEFAULT 5,
                    max_leverage REAL NOT NULL DEFAULT 3,
                    max_consecutive_losses INTEGER NOT NULL DEFAULT 3,
                    require_order_confirmation INTEGER NOT NULL DEFAULT 0,
                    allow_reverse_position INTEGER NOT NULL DEFAULT 0,
                    allow_duplicate_position INTEGER NOT NULL DEFAULT 0,
                    auto_disable_on_daily_loss INTEGER NOT NULL DEFAULT 1,
                    require_live_confirmation INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS exchange_sync_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    exchange TEXT NOT NULL DEFAULT 'paper',
                    run_mode TEXT NOT NULL DEFAULT 'paper',
                    status TEXT NOT NULL DEFAULT 'completed',
                    local_snapshot TEXT NOT NULL DEFAULT '{}',
                    exchange_snapshot TEXT NOT NULL DEFAULT '{}',
                    differences TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS symbol_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    exchange TEXT NOT NULL DEFAULT 'paper',
                    market_type TEXT NOT NULL DEFAULT 'spot',
                    symbol TEXT NOT NULL,
                    min_quantity REAL NOT NULL DEFAULT 0,
                    quantity_step REAL NOT NULL DEFAULT 0,
                    price_tick REAL NOT NULL DEFAULT 0,
                    min_notional REAL NOT NULL DEFAULT 0,
                    max_notional REAL NOT NULL DEFAULT 0,
                    max_leverage REAL NOT NULL DEFAULT 1,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    source TEXT NOT NULL DEFAULT 'manual',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (exchange, market_type, symbol)
                );

                CREATE TABLE IF NOT EXISTS realtime_market_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'local',
                    best_bid REAL NOT NULL,
                    best_ask REAL NOT NULL,
                    last_price REAL NOT NULL,
                    spread_bps REAL NOT NULL,
                    order_book TEXT NOT NULL DEFAULT '{}',
                    recent_trades TEXT NOT NULL DEFAULT '[]',
                    kline TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS signal_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    strategy_config_id INTEGER,
                    strategy_type TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    signal TEXT NOT NULL,
                    score REAL NOT NULL DEFAULT 0,
                    parameters TEXT NOT NULL DEFAULT '{}',
                    market_snapshot TEXT NOT NULL DEFAULT '{}',
                    reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                    FOREIGN KEY (strategy_config_id) REFERENCES strategy_configs(id) ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS automation_controls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    strategy_config_id INTEGER,
                    run_mode TEXT NOT NULL DEFAULT 'paper',
                    status TEXT NOT NULL DEFAULT 'stopped',
                    trading_enabled INTEGER NOT NULL DEFAULT 0,
                    last_action TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (user_id, strategy_config_id, run_mode),
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                    FOREIGN KEY (strategy_config_id) REFERENCES strategy_configs(id) ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS system_flags (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL DEFAULT '',
                    updated_by INTEGER,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (updated_by) REFERENCES users(id) ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS execution_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    order_id INTEGER,
                    run_id INTEGER,
                    event_type TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT '',
                    price_before REAL,
                    price_after REAL,
                    risk_checks TEXT NOT NULL DEFAULT '[]',
                    strategy_snapshot TEXT NOT NULL DEFAULT '{}',
                    raw_response TEXT NOT NULL DEFAULT '{}',
                    error_message TEXT,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                    FOREIGN KEY (order_id) REFERENCES trade_orders(id) ON DELETE SET NULL,
                    FOREIGN KEY (run_id) REFERENCES trade_runs(id) ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS notification_channels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    channel_type TEXT NOT NULL,
                    label TEXT NOT NULL,
                    destination TEXT NOT NULL DEFAULT '',
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    channel_type TEXT NOT NULL DEFAULT 'local',
                    event_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    message TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'stored',
                    payload TEXT NOT NULL DEFAULT '{}',
                    destination TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    delivered_at TEXT,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS watchlists (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    symbols TEXT NOT NULL DEFAULT '[]',
                    notes TEXT NOT NULL DEFAULT '',
                    is_default INTEGER NOT NULL DEFAULT 0,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                );
                """
            )
            _ensure_column(conn, "users", "permission_grants", "TEXT NOT NULL DEFAULT '[]'")
            _ensure_column(conn, "users", "permission_revokes", "TEXT NOT NULL DEFAULT '[]'")
            _ensure_column(conn, "strategy_configs", "current_version", "INTEGER NOT NULL DEFAULT 1")
            _ensure_column(conn, "trade_runs", "strategy_version", "INTEGER")
            _ensure_column(conn, "trade_orders", "time_in_force", "TEXT NOT NULL DEFAULT 'GTC'")
            _ensure_column(conn, "trade_orders", "post_only", "INTEGER NOT NULL DEFAULT 0")
            _ensure_column(conn, "trade_orders", "reduce_only", "INTEGER NOT NULL DEFAULT 0")
            _ensure_column(conn, "trade_orders", "stop_price", "REAL")
            _ensure_column(conn, "trade_orders", "trailing_delta_pct", "REAL")
            _ensure_column(conn, "trade_orders", "leverage", "REAL NOT NULL DEFAULT 1")
            _ensure_column(conn, "trade_records", "realized_pnl", "REAL")
            _ensure_column(conn, "user_risk_settings", "max_open_positions", "INTEGER NOT NULL DEFAULT 5")
            _ensure_column(conn, "user_risk_settings", "max_leverage", "REAL NOT NULL DEFAULT 3")
            _ensure_column(conn, "user_risk_settings", "max_consecutive_losses", "INTEGER NOT NULL DEFAULT 3")
            _ensure_column(conn, "user_risk_settings", "require_order_confirmation", "INTEGER NOT NULL DEFAULT 0")
            _ensure_column(conn, "user_risk_settings", "allow_reverse_position", "INTEGER NOT NULL DEFAULT 0")
            _ensure_column(conn, "user_risk_settings", "allow_duplicate_position", "INTEGER NOT NULL DEFAULT 0")
            _ensure_column(conn, "user_risk_settings", "auto_disable_on_daily_loss", "INTEGER NOT NULL DEFAULT 1")

    def user_count(self) -> int:
        with self.connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM users").fetchone()
            return int(row["count"])

    def create_user(
        self,
        email: str,
        password: str,
        role: str,
        status: str = "active",
        permission_grants: list[str] | None = None,
        permission_revokes: list[str] | None = None,
    ) -> dict[str, Any]:
        normalized_role = normalize_role(role)
        normalized_status = _normalize_status(status)
        grants, revokes = _normalize_permission_overrides(permission_grants, permission_revokes)
        now = utc_now()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO users (
                    email, password_hash, role, status, permission_grants, permission_revokes,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    email.strip().lower(),
                    hash_password(password),
                    normalized_role,
                    normalized_status,
                    json.dumps(grants),
                    json.dumps(revokes),
                    now,
                    now,
                ),
            )
            user_id = int(cursor.lastrowid)
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            return _public_user(row)

    def get_user_for_login(self, email: str) -> Optional[dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE email = ?", (email.strip().lower(),)).fetchone()
            return dict(row) if row else None

    def get_user(self, user_id: int) -> Optional[dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            return _public_user(row) if row else None

    def list_users(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
            return [_public_user(row) for row in rows]

    def update_user(
        self,
        user_id: int,
        role: str | None = None,
        status: str | None = None,
        permission_grants: list[str] | None = None,
        permission_revokes: list[str] | None = None,
    ) -> Optional[dict[str, Any]]:
        updates: list[str] = []
        values: list[Any] = []
        if role is not None:
            updates.append("role = ?")
            values.append(normalize_role(role))
        if status is not None:
            updates.append("status = ?")
            values.append(_normalize_status(status))
        if permission_grants is not None or permission_revokes is not None:
            current = self.get_user(user_id)
            if current is None:
                return None
            grants, revokes = _normalize_permission_overrides(
                permission_grants if permission_grants is not None else current["permission_grants"],
                permission_revokes if permission_revokes is not None else current["permission_revokes"],
            )
            updates.append("permission_grants = ?")
            values.append(json.dumps(grants))
            updates.append("permission_revokes = ?")
            values.append(json.dumps(revokes))
        if not updates:
            return self.get_user(user_id)

        updates.append("updated_at = ?")
        values.append(utc_now())
        values.append(user_id)
        with self.connect() as conn:
            conn.execute(f"UPDATE users SET {', '.join(updates)} WHERE id = ?", values)
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            return _public_user(row) if row else None

    def update_user_password(self, user_id: int, password: str) -> Optional[dict[str, Any]]:
        with self.connect() as conn:
            conn.execute(
                "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
                (hash_password(password), utc_now(), user_id),
            )
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            return _public_user(row) if row else None

    def create_session(self, user_id: int, ttl_seconds: int = 86_400) -> str:
        token = secrets.token_urlsafe(32)
        expires_at = time.time() + ttl_seconds
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO sessions (token_hash, user_id, expires_at, created_at) VALUES (?, ?, ?, ?)",
                (hash_token(token), user_id, expires_at, utc_now()),
            )
        return token

    def get_session_user(self, token: str | None) -> Optional[dict[str, Any]]:
        if not token:
            return None
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT users.*
                FROM sessions
                JOIN users ON users.id = sessions.user_id
                WHERE sessions.token_hash = ?
                  AND sessions.expires_at > ?
                  AND users.status = 'active'
                """,
                (hash_token(token), time.time()),
            ).fetchone()
            return _public_user(row) if row else None

    def delete_session(self, token: str | None) -> None:
        if not token:
            return
        with self.connect() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (hash_token(token),))

    def delete_user_sessions(self, user_id: int, keep_token: str | None = None) -> None:
        with self.connect() as conn:
            if keep_token:
                conn.execute(
                    "DELETE FROM sessions WHERE user_id = ? AND token_hash != ?",
                    (user_id, hash_token(keep_token)),
                )
            else:
                conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))

    def create_api_key(
        self,
        user_id: int,
        exchange: str,
        label: str,
        api_key: str,
        api_secret: str,
        passphrase: str = "",
        scopes: list[str] | None = None,
        can_trade: bool = False,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO exchange_api_keys (
                    user_id, exchange, label, api_key_enc, api_secret_enc, passphrase_enc,
                    scopes, can_trade, is_active, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    user_id,
                    exchange.strip().lower(),
                    label.strip(),
                    self.secret_box.encrypt(api_key.strip()),
                    self.secret_box.encrypt(api_secret.strip()),
                    self.secret_box.encrypt(passphrase.strip()),
                    json.dumps(scopes or []),
                    1 if can_trade else 0,
                    now,
                    now,
                ),
            )
            api_key_id = int(cursor.lastrowid)
        return self.get_api_key(api_key_id)

    def get_api_key(self, api_key_id: int) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT exchange_api_keys.*, users.email AS owner_email
                FROM exchange_api_keys
                JOIN users ON users.id = exchange_api_keys.user_id
                WHERE exchange_api_keys.id = ?
                """,
                (api_key_id,),
            ).fetchone()
            if not row:
                raise KeyError(api_key_id)
            return self._public_api_key(row)

    def list_api_keys(self, user_id: int, include_all: bool = False) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if include_all:
                rows = conn.execute(
                    """
                    SELECT exchange_api_keys.*, users.email AS owner_email
                    FROM exchange_api_keys
                    JOIN users ON users.id = exchange_api_keys.user_id
                    ORDER BY exchange_api_keys.id DESC
                    """
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT exchange_api_keys.*, users.email AS owner_email
                    FROM exchange_api_keys
                    JOIN users ON users.id = exchange_api_keys.user_id
                    WHERE exchange_api_keys.user_id = ?
                    ORDER BY exchange_api_keys.id DESC
                    """,
                    (user_id,),
                ).fetchall()
            return [self._public_api_key(row) for row in rows]

    def delete_api_key(self, api_key_id: int, user_id: int, is_admin: bool = False) -> bool:
        with self.connect() as conn:
            if is_admin:
                cursor = conn.execute("DELETE FROM exchange_api_keys WHERE id = ?", (api_key_id,))
            else:
                cursor = conn.execute(
                    "DELETE FROM exchange_api_keys WHERE id = ? AND user_id = ?",
                    (api_key_id, user_id),
            )
            return cursor.rowcount > 0

    def create_strategy_config(
        self,
        user_id: int,
        name: str,
        strategy_type: str,
        parameters: dict[str, Any],
        market_type: str = "spot",
        is_default: bool = False,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as conn:
            if is_default:
                conn.execute("UPDATE strategy_configs SET is_default = 0 WHERE user_id = ?", (user_id,))
            cursor = conn.execute(
                """
                INSERT INTO strategy_configs (
                    user_id, name, strategy_type, market_type, parameters,
                    current_version, is_default, is_active, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, 1, ?, 1, ?, ?)
                """,
                (
                    user_id,
                    name.strip(),
                    strategy_type.strip(),
                    market_type.strip(),
                    json.dumps(parameters, sort_keys=True),
                    1 if is_default else 0,
                    now,
                    now,
                ),
            )
            config_id = int(cursor.lastrowid)
            self._insert_strategy_config_version(
                conn,
                strategy_config_id=config_id,
                user_id=user_id,
                version=1,
                name=name,
                strategy_type=strategy_type,
                market_type=market_type,
                parameters=parameters,
                change_note="created",
            )
        config = self.get_strategy_config(config_id, user_id=user_id, is_admin=True)
        if config is None:
            raise KeyError(config_id)
        return config

    def _insert_strategy_config_version(
        self,
        conn: sqlite3.Connection,
        strategy_config_id: int,
        user_id: int,
        version: int,
        name: str,
        strategy_type: str,
        market_type: str,
        parameters: dict[str, Any],
        change_note: str = "",
    ) -> None:
        conn.execute(
            """
            INSERT OR IGNORE INTO strategy_config_versions (
                strategy_config_id, user_id, version, name, strategy_type,
                market_type, parameters, change_note, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                strategy_config_id,
                user_id,
                int(version),
                name.strip(),
                strategy_type.strip(),
                market_type.strip(),
                json.dumps(parameters, sort_keys=True),
                change_note.strip(),
                utc_now(),
            ),
        )

    def list_strategy_configs(self, user_id: int, include_all: bool = False) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if include_all:
                rows = conn.execute(
                    """
                    SELECT strategy_configs.*, users.email AS owner_email
                    FROM strategy_configs
                    JOIN users ON users.id = strategy_configs.user_id
                    WHERE strategy_configs.is_active = 1
                    ORDER BY strategy_configs.is_default DESC, strategy_configs.updated_at DESC
                    """
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT strategy_configs.*, users.email AS owner_email
                    FROM strategy_configs
                    JOIN users ON users.id = strategy_configs.user_id
                    WHERE strategy_configs.user_id = ?
                      AND strategy_configs.is_active = 1
                    ORDER BY strategy_configs.is_default DESC, strategy_configs.updated_at DESC
                    """,
                    (user_id,),
                ).fetchall()
            return [_public_strategy_config(row) for row in rows]

    def get_strategy_config(self, config_id: int, user_id: int, is_admin: bool = False) -> Optional[dict[str, Any]]:
        with self.connect() as conn:
            if is_admin:
                row = conn.execute(
                    """
                    SELECT strategy_configs.*, users.email AS owner_email
                    FROM strategy_configs
                    JOIN users ON users.id = strategy_configs.user_id
                    WHERE strategy_configs.id = ?
                      AND strategy_configs.is_active = 1
                    """,
                    (config_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT strategy_configs.*, users.email AS owner_email
                    FROM strategy_configs
                    JOIN users ON users.id = strategy_configs.user_id
                    WHERE strategy_configs.id = ?
                      AND strategy_configs.user_id = ?
                      AND strategy_configs.is_active = 1
                    """,
                    (config_id, user_id),
                ).fetchone()
            return _public_strategy_config(row) if row else None

    def update_strategy_config(
        self,
        config_id: int,
        user_id: int,
        is_admin: bool = False,
        name: str | None = None,
        strategy_type: str | None = None,
        parameters: dict[str, Any] | None = None,
        market_type: str | None = None,
        is_default: bool | None = None,
    ) -> Optional[dict[str, Any]]:
        existing = self.get_strategy_config(config_id, user_id=user_id, is_admin=is_admin)
        if not existing:
            return None

        owner_id = existing["user_id"]
        updates: list[str] = []
        values: list[Any] = []
        if name is not None:
            updates.append("name = ?")
            values.append(name.strip())
        if strategy_type is not None:
            updates.append("strategy_type = ?")
            values.append(strategy_type.strip())
        if parameters is not None:
            updates.append("parameters = ?")
            values.append(json.dumps(parameters, sort_keys=True))
        if market_type is not None:
            updates.append("market_type = ?")
            values.append(market_type.strip())
        if is_default is not None:
            updates.append("is_default = ?")
            values.append(1 if is_default else 0)
        if not updates:
            return existing

        versionable_change = any(item is not None for item in (name, strategy_type, parameters, market_type))
        next_version = int(existing.get("current_version") or 1) + 1
        if versionable_change:
            updates.append("current_version = ?")
            values.append(next_version)
        updates.append("updated_at = ?")
        values.append(utc_now())
        values.append(config_id)

        with self.connect() as conn:
            if is_default:
                conn.execute("UPDATE strategy_configs SET is_default = 0 WHERE user_id = ?", (owner_id,))
            conn.execute(f"UPDATE strategy_configs SET {', '.join(updates)} WHERE id = ?", values)
            if versionable_change:
                self._insert_strategy_config_version(
                    conn,
                    strategy_config_id=config_id,
                    user_id=owner_id,
                    version=next_version,
                    name=name if name is not None else existing["name"],
                    strategy_type=strategy_type if strategy_type is not None else existing["strategy_type"],
                    market_type=market_type if market_type is not None else existing["market_type"],
                    parameters=parameters if parameters is not None else existing["parameters"],
                    change_note="updated",
                )

        return self.get_strategy_config(config_id, user_id=user_id, is_admin=is_admin)

    def list_strategy_config_versions(
        self,
        config_id: int,
        user_id: int,
        is_admin: bool = False,
    ) -> list[dict[str, Any]]:
        config = self.get_strategy_config(config_id, user_id=user_id, is_admin=is_admin)
        if not config:
            return []
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT strategy_config_versions.*, users.email AS owner_email
                FROM strategy_config_versions
                JOIN users ON users.id = strategy_config_versions.user_id
                WHERE strategy_config_versions.strategy_config_id = ?
                ORDER BY strategy_config_versions.version DESC
                """,
                (config_id,),
            ).fetchall()
            return [_public_strategy_config_version(row) for row in rows]

    def delete_strategy_config(self, config_id: int, user_id: int, is_admin: bool = False) -> bool:
        existing = self.get_strategy_config(config_id, user_id=user_id, is_admin=is_admin)
        if not existing:
            return False
        with self.connect() as conn:
            conn.execute(
                "UPDATE strategy_configs SET is_active = 0, updated_at = ? WHERE id = ?",
                (utc_now(), config_id),
            )
        return True

    def create_trade_run(
        self,
        user_id: int,
        strategy_type: str,
        parameters: dict[str, Any],
        metrics: dict[str, Any],
        run_mode: str = "backtest",
        status: str = "completed",
        market_type: str = "spot",
        strategy_config_id: int | None = None,
        strategy_version: int | None = None,
        bars_file: str | None = None,
        events_file: str | None = None,
        started_at: str | None = None,
        finished_at: str | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        started = started_at or now
        finished = finished_at or now
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO trade_runs (
                    user_id, strategy_config_id, strategy_version, strategy_type, run_mode, status,
                    market_type, bars_file, events_file, parameters, metrics,
                    started_at, finished_at, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    strategy_config_id,
                    strategy_version,
                    strategy_type,
                    run_mode,
                    status,
                    market_type,
                    bars_file,
                    events_file,
                    json.dumps(parameters, sort_keys=True),
                    json.dumps(metrics, sort_keys=True),
                    started,
                    finished,
                    now,
                ),
            )
            run_id = int(cursor.lastrowid)
        run = self.get_trade_run(run_id, user_id=user_id, is_admin=True)
        if run is None:
            raise KeyError(run_id)
        return run

    def get_trade_run(self, run_id: int, user_id: int, is_admin: bool = False) -> Optional[dict[str, Any]]:
        with self.connect() as conn:
            if is_admin:
                row = conn.execute(
                    """
                    SELECT trade_runs.*, users.email AS owner_email, strategy_configs.name AS strategy_name
                    FROM trade_runs
                    JOIN users ON users.id = trade_runs.user_id
                    LEFT JOIN strategy_configs ON strategy_configs.id = trade_runs.strategy_config_id
                    WHERE trade_runs.id = ?
                    """,
                    (run_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT trade_runs.*, users.email AS owner_email, strategy_configs.name AS strategy_name
                    FROM trade_runs
                    JOIN users ON users.id = trade_runs.user_id
                    LEFT JOIN strategy_configs ON strategy_configs.id = trade_runs.strategy_config_id
                    WHERE trade_runs.id = ?
                      AND trade_runs.user_id = ?
                    """,
                    (run_id, user_id),
                ).fetchone()
            return _public_trade_run(row) if row else None

    def list_trade_runs(
        self,
        user_id: int,
        include_all: bool = False,
        limit: int = 50,
        run_mode: str | None = None,
    ) -> list[dict[str, Any]]:
        limit = _bounded_limit(limit)
        mode_filter = "AND trade_runs.run_mode = ?" if run_mode else ""
        with self.connect() as conn:
            if include_all:
                values: tuple[Any, ...] = (run_mode, limit) if run_mode else (limit,)
                rows = conn.execute(
                    f"""
                    SELECT trade_runs.*, users.email AS owner_email, strategy_configs.name AS strategy_name
                    FROM trade_runs
                    JOIN users ON users.id = trade_runs.user_id
                    LEFT JOIN strategy_configs ON strategy_configs.id = trade_runs.strategy_config_id
                    WHERE 1 = 1
                    {mode_filter}
                    ORDER BY trade_runs.id DESC
                    LIMIT ?
                    """,
                    values,
                ).fetchall()
            else:
                values = (user_id, run_mode, limit) if run_mode else (user_id, limit)
                rows = conn.execute(
                    f"""
                    SELECT trade_runs.*, users.email AS owner_email, strategy_configs.name AS strategy_name
                    FROM trade_runs
                    JOIN users ON users.id = trade_runs.user_id
                    LEFT JOIN strategy_configs ON strategy_configs.id = trade_runs.strategy_config_id
                    WHERE trade_runs.user_id = ?
                    {mode_filter}
                    ORDER BY trade_runs.id DESC
                    LIMIT ?
                    """,
                    values,
                ).fetchall()
            return [_public_trade_run(row) for row in rows]

    def create_trade_records(
        self,
        run_id: int,
        user_id: int,
        trades: list[dict[str, Any]],
        strategy_config_id: int | None = None,
        run_mode: str = "backtest",
        exchange: str = "simulated",
        market_type: str = "spot",
    ) -> list[dict[str, Any]]:
        if not trades:
            return []
        now = utc_now()
        with self.connect() as conn:
            trade_ids: list[int] = []
            for trade in trades:
                quantity = float(trade["quantity"])
                price = float(trade["price"])
                cursor = conn.execute(
                    """
                    INSERT INTO trade_records (
                        run_id, user_id, strategy_config_id, run_mode, exchange, market_type,
                        symbol, side, quantity, price, notional, fee, order_id, reason,
                        status, realized_pnl, traded_at, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        user_id,
                        strategy_config_id,
                        run_mode,
                        exchange,
                        market_type,
                        str(trade["symbol"]).upper(),
                        str(trade["side"]).lower(),
                        quantity,
                        price,
                        quantity * price,
                        float(trade.get("fee", 0.0)),
                        trade.get("order_id"),
                        trade.get("reason", ""),
                        trade.get("status", "filled"),
                        trade.get("realized_pnl"),
                        trade["traded_at"],
                        now,
                    ),
                )
                trade_ids.append(int(cursor.lastrowid))

        return self.list_trade_records(user_id=user_id, include_all=True, trade_ids=trade_ids, limit=len(trade_ids))

    def create_trade_order(
        self,
        user_id: int,
        client_order_id: str,
        symbol: str,
        side: str,
        quantity: float,
        requested_price: float | None = None,
        order_type: str = "market",
        time_in_force: str = "GTC",
        post_only: bool = False,
        reduce_only: bool = False,
        run_mode: str = "paper",
        exchange: str = "paper",
        market_type: str = "spot",
        strategy_config_id: int | None = None,
        run_id: int | None = None,
        limit_price: float | None = None,
        stop_price: float | None = None,
        trailing_delta_pct: float | None = None,
        leverage: float = 1.0,
        reason: str = "",
        status: str = "created",
        raw_response: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO trade_orders (
                    client_order_id, user_id, strategy_config_id, run_id, run_mode,
                    exchange, market_type, symbol, side, order_type, time_in_force,
                    post_only, reduce_only, quantity, requested_price, limit_price,
                    stop_price, trailing_delta_pct, leverage, status, reason, raw_response,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    client_order_id,
                    user_id,
                    strategy_config_id,
                    run_id,
                    run_mode,
                    exchange,
                    market_type,
                    symbol.upper(),
                    side.lower(),
                    order_type.lower(),
                    time_in_force.upper(),
                    1 if post_only else 0,
                    1 if reduce_only else 0,
                    float(quantity),
                    requested_price,
                    limit_price,
                    stop_price,
                    trailing_delta_pct,
                    float(leverage),
                    status,
                    reason,
                    json.dumps(raw_response or {}, sort_keys=True),
                    now,
                    now,
                ),
            )
            order_id = int(cursor.lastrowid)
        order = self.get_trade_order(order_id, user_id=user_id, is_admin=True)
        if order is None:
            raise KeyError(order_id)
        return order

    def update_trade_order(
        self,
        order_id: int,
        status: str | None = None,
        exchange_order_id: str | None = None,
        run_id: int | None = None,
        filled_quantity: float | None = None,
        average_fill_price: float | None = None,
        error_message: str | None = None,
        raw_response: dict[str, Any] | None = None,
        submitted: bool = False,
        finished: bool = False,
    ) -> dict[str, Any] | None:
        updates: list[str] = []
        values: list[Any] = []
        if status is not None:
            updates.append("status = ?")
            values.append(status)
        if exchange_order_id is not None:
            updates.append("exchange_order_id = ?")
            values.append(exchange_order_id)
        if run_id is not None:
            updates.append("run_id = ?")
            values.append(run_id)
        if filled_quantity is not None:
            updates.append("filled_quantity = ?")
            values.append(float(filled_quantity))
        if average_fill_price is not None:
            updates.append("average_fill_price = ?")
            values.append(float(average_fill_price))
        if error_message is not None:
            updates.append("error_message = ?")
            values.append(error_message)
        if raw_response is not None:
            updates.append("raw_response = ?")
            values.append(json.dumps(raw_response, sort_keys=True))
        now = utc_now()
        if submitted:
            updates.append("submitted_at = ?")
            values.append(now)
        if finished:
            updates.append("finished_at = ?")
            values.append(now)
        if not updates:
            return self.get_trade_order(order_id, user_id=0, is_admin=True)
        updates.append("updated_at = ?")
        values.append(now)
        values.append(order_id)
        with self.connect() as conn:
            cursor = conn.execute(f"UPDATE trade_orders SET {', '.join(updates)} WHERE id = ?", values)
            if cursor.rowcount == 0:
                return None
        return self.get_trade_order(order_id, user_id=0, is_admin=True)

    def get_trade_order(self, order_id: int, user_id: int, is_admin: bool = False) -> dict[str, Any] | None:
        with self.connect() as conn:
            if is_admin:
                row = conn.execute(
                    """
                    SELECT trade_orders.*, users.email AS owner_email, strategy_configs.name AS strategy_name
                    FROM trade_orders
                    JOIN users ON users.id = trade_orders.user_id
                    LEFT JOIN strategy_configs ON strategy_configs.id = trade_orders.strategy_config_id
                    WHERE trade_orders.id = ?
                    """,
                    (order_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT trade_orders.*, users.email AS owner_email, strategy_configs.name AS strategy_name
                    FROM trade_orders
                    JOIN users ON users.id = trade_orders.user_id
                    LEFT JOIN strategy_configs ON strategy_configs.id = trade_orders.strategy_config_id
                    WHERE trade_orders.id = ?
                      AND trade_orders.user_id = ?
                    """,
                    (order_id, user_id),
                ).fetchone()
            return _public_trade_order(row) if row else None

    def list_trade_orders(
        self,
        user_id: int,
        include_all: bool = False,
        limit: int = 100,
        run_mode: str | None = None,
    ) -> list[dict[str, Any]]:
        limit = _bounded_limit(limit)
        filters: list[str] = []
        values: list[Any] = []
        if not include_all:
            filters.append("trade_orders.user_id = ?")
            values.append(user_id)
        if run_mode:
            filters.append("trade_orders.run_mode = ?")
            values.append(run_mode)
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        values.append(limit)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT trade_orders.*, users.email AS owner_email, strategy_configs.name AS strategy_name
                FROM trade_orders
                JOIN users ON users.id = trade_orders.user_id
                LEFT JOIN strategy_configs ON strategy_configs.id = trade_orders.strategy_config_id
                {where}
                ORDER BY trade_orders.id DESC
                LIMIT ?
                """,
                values,
            ).fetchall()
            return [_public_trade_order(row) for row in rows]

    def create_audit_log(
        self,
        user_id: int | None,
        action: str,
        entity_type: str = "",
        entity_id: str | int | None = None,
        status: str = "ok",
        detail: dict[str, Any] | None = None,
        ip_address: str = "",
        user_agent: str = "",
    ) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO audit_logs (
                    user_id, action, entity_type, entity_id, status, detail,
                    ip_address, user_agent, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    action.strip(),
                    entity_type.strip(),
                    str(entity_id) if entity_id is not None else None,
                    status.strip(),
                    json.dumps(detail or {}, sort_keys=True),
                    ip_address.strip(),
                    user_agent.strip()[:240],
                    now,
                ),
            )
            audit_id = int(cursor.lastrowid)
            row = conn.execute(
                """
                SELECT audit_logs.*, users.email AS owner_email
                FROM audit_logs
                LEFT JOIN users ON users.id = audit_logs.user_id
                WHERE audit_logs.id = ?
                """,
                (audit_id,),
            ).fetchone()
            return _public_audit_log(row)

    def list_audit_logs(self, user_id: int, include_all: bool = False, limit: int = 100) -> list[dict[str, Any]]:
        limit = _bounded_limit(limit)
        filters: list[str] = []
        values: list[Any] = []
        if not include_all:
            filters.append("audit_logs.user_id = ?")
            values.append(user_id)
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        values.append(limit)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT audit_logs.*, users.email AS owner_email
                FROM audit_logs
                LEFT JOIN users ON users.id = audit_logs.user_id
                {where}
                ORDER BY audit_logs.id DESC
                LIMIT ?
                """,
                values,
            ).fetchall()
            return [_public_audit_log(row) for row in rows]

    def create_exchange_sync_run(
        self,
        user_id: int,
        exchange: str,
        run_mode: str,
        status: str,
        local_snapshot: dict[str, Any],
        exchange_snapshot: dict[str, Any],
        differences: list[dict[str, Any]],
    ) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO exchange_sync_runs (
                    user_id, exchange, run_mode, status, local_snapshot,
                    exchange_snapshot, differences, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    exchange.strip().lower(),
                    run_mode.strip().lower(),
                    status.strip().lower(),
                    json.dumps(local_snapshot, sort_keys=True),
                    json.dumps(exchange_snapshot, sort_keys=True),
                    json.dumps(differences, sort_keys=True),
                    now,
                ),
            )
            sync_id = int(cursor.lastrowid)
            row = conn.execute(
                """
                SELECT exchange_sync_runs.*, users.email AS owner_email
                FROM exchange_sync_runs
                JOIN users ON users.id = exchange_sync_runs.user_id
                WHERE exchange_sync_runs.id = ?
                """,
                (sync_id,),
            ).fetchone()
            return _public_exchange_sync_run(row)

    def list_exchange_sync_runs(
        self,
        user_id: int,
        include_all: bool = False,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        limit = _bounded_limit(limit)
        filters: list[str] = []
        values: list[Any] = []
        if not include_all:
            filters.append("exchange_sync_runs.user_id = ?")
            values.append(user_id)
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        values.append(limit)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT exchange_sync_runs.*, users.email AS owner_email
                FROM exchange_sync_runs
                JOIN users ON users.id = exchange_sync_runs.user_id
                {where}
                ORDER BY exchange_sync_runs.id DESC
                LIMIT ?
                """,
                values,
            ).fetchall()
            return [_public_exchange_sync_run(row) for row in rows]

    def upsert_symbol_rule(
        self,
        exchange: str,
        market_type: str,
        symbol: str,
        min_quantity: float = 0.0,
        quantity_step: float = 0.0,
        price_tick: float = 0.0,
        min_notional: float = 0.0,
        max_notional: float = 0.0,
        max_leverage: float = 1.0,
        is_active: bool = True,
        source: str = "manual",
    ) -> dict[str, Any]:
        normalized_exchange = (exchange or "paper").strip().lower()
        normalized_market_type = (market_type or "spot").strip().lower()
        normalized_symbol = symbol.strip().upper().replace(" ", "")
        if not normalized_symbol:
            raise ValueError("symbol_required")
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO symbol_rules (
                    exchange, market_type, symbol, min_quantity, quantity_step, price_tick,
                    min_notional, max_notional, max_leverage, is_active, source, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(exchange, market_type, symbol) DO UPDATE SET
                    min_quantity = excluded.min_quantity,
                    quantity_step = excluded.quantity_step,
                    price_tick = excluded.price_tick,
                    min_notional = excluded.min_notional,
                    max_notional = excluded.max_notional,
                    max_leverage = excluded.max_leverage,
                    is_active = excluded.is_active,
                    source = excluded.source,
                    updated_at = excluded.updated_at
                """,
                (
                    normalized_exchange,
                    normalized_market_type,
                    normalized_symbol,
                    float(min_quantity),
                    float(quantity_step),
                    float(price_tick),
                    float(min_notional),
                    float(max_notional),
                    float(max_leverage),
                    1 if is_active else 0,
                    (source or "manual").strip().lower(),
                    now,
                    now,
                ),
            )
            row = conn.execute(
                """
                SELECT *
                FROM symbol_rules
                WHERE exchange = ? AND market_type = ? AND symbol = ?
                """,
                (normalized_exchange, normalized_market_type, normalized_symbol),
            ).fetchone()
            return _public_symbol_rule(row)

    def get_symbol_rule(
        self,
        symbol: str,
        exchange: str = "paper",
        market_type: str = "spot",
        active_only: bool = True,
    ) -> dict[str, Any] | None:
        filters = ["exchange = ?", "market_type = ?", "symbol = ?"]
        values: list[Any] = [
            (exchange or "paper").strip().lower(),
            (market_type or "spot").strip().lower(),
            symbol.strip().upper().replace(" ", ""),
        ]
        if active_only:
            filters.append("is_active = 1")
        with self.connect() as conn:
            row = conn.execute(
                f"""
                SELECT *
                FROM symbol_rules
                WHERE {' AND '.join(filters)}
                """,
                values,
            ).fetchone()
            return _public_symbol_rule(row) if row else None

    def list_symbol_rules(
        self,
        exchange: str | None = None,
        market_type: str | None = None,
        symbol: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        limit = _bounded_limit(limit)
        filters: list[str] = []
        values: list[Any] = []
        if exchange:
            filters.append("exchange = ?")
            values.append(exchange.strip().lower())
        if market_type:
            filters.append("market_type = ?")
            values.append(market_type.strip().lower())
        if symbol:
            filters.append("symbol = ?")
            values.append(symbol.strip().upper().replace(" ", ""))
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        values.append(limit)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM symbol_rules
                {where}
                ORDER BY is_active DESC, exchange, market_type, symbol
                LIMIT ?
                """,
                values,
            ).fetchall()
            return [_public_symbol_rule(row) for row in rows]

    def create_realtime_market_snapshot(
        self,
        symbol: str,
        best_bid: float,
        best_ask: float,
        last_price: float,
        order_book: dict[str, Any],
        recent_trades: list[dict[str, Any]],
        kline: dict[str, Any],
        source: str = "local",
    ) -> dict[str, Any]:
        now = utc_now()
        spread_bps = ((best_ask - best_bid) / last_price * 10_000) if last_price else 0.0
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO realtime_market_snapshots (
                    symbol, source, best_bid, best_ask, last_price, spread_bps,
                    order_book, recent_trades, kline, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    symbol.strip().upper(),
                    source.strip().lower(),
                    float(best_bid),
                    float(best_ask),
                    float(last_price),
                    float(spread_bps),
                    json.dumps(order_book, sort_keys=True),
                    json.dumps(recent_trades, sort_keys=True),
                    json.dumps(kline, sort_keys=True),
                    now,
                ),
            )
            snapshot_id = int(cursor.lastrowid)
            row = conn.execute("SELECT * FROM realtime_market_snapshots WHERE id = ?", (snapshot_id,)).fetchone()
            return _public_realtime_market_snapshot(row)

    def list_realtime_market_snapshots(
        self,
        symbol: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        limit = _bounded_limit(limit)
        filters: list[str] = []
        values: list[Any] = []
        if symbol:
            filters.append("symbol = ?")
            values.append(symbol.strip().upper())
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        values.append(limit)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM realtime_market_snapshots
                {where}
                ORDER BY id DESC
                LIMIT ?
                """,
                values,
            ).fetchall()
            return [_public_realtime_market_snapshot(row) for row in rows]

    def create_signal_log(
        self,
        user_id: int,
        strategy_type: str,
        symbol: str,
        signal: str,
        score: float,
        parameters: dict[str, Any],
        market_snapshot: dict[str, Any],
        strategy_config_id: int | None = None,
        reason: str = "",
    ) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO signal_logs (
                    user_id, strategy_config_id, strategy_type, symbol, signal, score,
                    parameters, market_snapshot, reason, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    strategy_config_id,
                    strategy_type,
                    symbol.strip().upper(),
                    signal,
                    float(score),
                    json.dumps(parameters, sort_keys=True),
                    json.dumps(market_snapshot, sort_keys=True),
                    reason,
                    now,
                ),
            )
            signal_id = int(cursor.lastrowid)
            row = conn.execute(
                """
                SELECT signal_logs.*, users.email AS owner_email, strategy_configs.name AS strategy_name
                FROM signal_logs
                JOIN users ON users.id = signal_logs.user_id
                LEFT JOIN strategy_configs ON strategy_configs.id = signal_logs.strategy_config_id
                WHERE signal_logs.id = ?
                """,
                (signal_id,),
            ).fetchone()
            return _public_signal_log(row)

    def list_signal_logs(self, user_id: int, include_all: bool = False, limit: int = 100) -> list[dict[str, Any]]:
        limit = _bounded_limit(limit)
        filters: list[str] = []
        values: list[Any] = []
        if not include_all:
            filters.append("signal_logs.user_id = ?")
            values.append(user_id)
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        values.append(limit)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT signal_logs.*, users.email AS owner_email, strategy_configs.name AS strategy_name
                FROM signal_logs
                JOIN users ON users.id = signal_logs.user_id
                LEFT JOIN strategy_configs ON strategy_configs.id = signal_logs.strategy_config_id
                {where}
                ORDER BY signal_logs.id DESC
                LIMIT ?
                """,
                values,
            ).fetchall()
            return [_public_signal_log(row) for row in rows]

    def upsert_automation_control(
        self,
        user_id: int,
        strategy_config_id: int | None,
        run_mode: str,
        status: str,
        trading_enabled: bool,
        last_action: str,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as conn:
            normalized_mode = run_mode.strip().lower()
            if strategy_config_id is None:
                existing = conn.execute(
                    """
                    SELECT id FROM automation_controls
                    WHERE user_id = ?
                      AND strategy_config_id IS NULL
                      AND run_mode = ?
                    """,
                    (user_id, normalized_mode),
                ).fetchone()
            else:
                existing = conn.execute(
                    """
                    SELECT id FROM automation_controls
                    WHERE user_id = ?
                      AND strategy_config_id = ?
                      AND run_mode = ?
                    """,
                    (user_id, strategy_config_id, normalized_mode),
                ).fetchone()

            if existing:
                conn.execute(
                    """
                    UPDATE automation_controls
                    SET status = ?, trading_enabled = ?, last_action = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        status.strip().lower(),
                        1 if trading_enabled else 0,
                        last_action.strip().lower(),
                        now,
                        existing["id"],
                    ),
                )
                control_id = int(existing["id"])
            else:
                cursor = conn.execute(
                    """
                    INSERT INTO automation_controls (
                        user_id, strategy_config_id, run_mode, status, trading_enabled,
                        last_action, updated_at, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        strategy_config_id,
                        normalized_mode,
                        status.strip().lower(),
                        1 if trading_enabled else 0,
                        last_action.strip().lower(),
                        now,
                        now,
                    ),
                )
                control_id = int(cursor.lastrowid)

            row = conn.execute(
                """
                SELECT automation_controls.*, users.email AS owner_email, strategy_configs.name AS strategy_name
                FROM automation_controls
                JOIN users ON users.id = automation_controls.user_id
                LEFT JOIN strategy_configs ON strategy_configs.id = automation_controls.strategy_config_id
                WHERE automation_controls.id = ?
                """,
                (control_id,),
            ).fetchone()
            return _public_automation_control(row)

    def list_automation_controls(self, user_id: int, include_all: bool = False) -> list[dict[str, Any]]:
        filters: list[str] = []
        values: list[Any] = []
        if not include_all:
            filters.append("automation_controls.user_id = ?")
            values.append(user_id)
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT automation_controls.*, users.email AS owner_email, strategy_configs.name AS strategy_name
                FROM automation_controls
                JOIN users ON users.id = automation_controls.user_id
                LEFT JOIN strategy_configs ON strategy_configs.id = automation_controls.strategy_config_id
                {where}
                ORDER BY automation_controls.updated_at DESC
                """,
                values,
            ).fetchall()
            return [_public_automation_control(row) for row in rows]

    def set_system_flag(self, key: str, value: str, updated_by: int | None = None) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO system_flags (key, value, updated_by, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key)
                DO UPDATE SET value = excluded.value, updated_by = excluded.updated_by, updated_at = excluded.updated_at
                """,
                (key.strip(), value, updated_by, now),
            )
            row = conn.execute("SELECT * FROM system_flags WHERE key = ?", (key.strip(),)).fetchone()
            return _public_system_flag(row)

    def get_system_flag(self, key: str, default: str = "") -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM system_flags WHERE key = ?", (key.strip(),)).fetchone()
            if row:
                return _public_system_flag(row)
        return {"key": key.strip(), "value": default, "updated_by": None, "updated_at": None}

    def create_execution_event(
        self,
        user_id: int,
        event_type: str,
        status: str = "",
        order_id: int | None = None,
        run_id: int | None = None,
        price_before: float | None = None,
        price_after: float | None = None,
        risk_checks: list[dict[str, Any]] | None = None,
        strategy_snapshot: dict[str, Any] | None = None,
        raw_response: dict[str, Any] | None = None,
        error_message: str | None = None,
        retry_count: int = 0,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO execution_events (
                    user_id, order_id, run_id, event_type, status, price_before, price_after,
                    risk_checks, strategy_snapshot, raw_response, error_message, retry_count, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    order_id,
                    run_id,
                    event_type,
                    status,
                    price_before,
                    price_after,
                    json.dumps(risk_checks or [], sort_keys=True),
                    json.dumps(strategy_snapshot or {}, sort_keys=True),
                    json.dumps(raw_response or {}, sort_keys=True),
                    error_message,
                    int(retry_count),
                    now,
                ),
            )
            event_id = int(cursor.lastrowid)
            row = conn.execute(
                """
                SELECT execution_events.*, users.email AS owner_email
                FROM execution_events
                JOIN users ON users.id = execution_events.user_id
                WHERE execution_events.id = ?
                """,
                (event_id,),
            ).fetchone()
            return _public_execution_event(row)

    def list_execution_events(
        self,
        user_id: int,
        include_all: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        limit = _bounded_limit(limit)
        filters: list[str] = []
        values: list[Any] = []
        if not include_all:
            filters.append("execution_events.user_id = ?")
            values.append(user_id)
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        values.append(limit)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT execution_events.*, users.email AS owner_email
                FROM execution_events
                JOIN users ON users.id = execution_events.user_id
                {where}
                ORDER BY execution_events.id DESC
                LIMIT ?
                """,
                values,
            ).fetchall()
            return [_public_execution_event(row) for row in rows]

    def create_notification_channel(
        self,
        user_id: int,
        channel_type: str,
        label: str,
        destination: str = "",
        is_active: bool = True,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO notification_channels (
                    user_id, channel_type, label, destination, is_active, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, channel_type.strip().lower(), label.strip(), destination.strip(), 1 if is_active else 0, now, now),
            )
            channel_id = int(cursor.lastrowid)
            row = conn.execute(
                """
                SELECT notification_channels.*, users.email AS owner_email
                FROM notification_channels
                JOIN users ON users.id = notification_channels.user_id
                WHERE notification_channels.id = ?
                """,
                (channel_id,),
            ).fetchone()
            return _public_notification_channel(row)

    def list_notification_channels(self, user_id: int, include_all: bool = False) -> list[dict[str, Any]]:
        filters: list[str] = []
        values: list[Any] = []
        if not include_all:
            filters.append("notification_channels.user_id = ?")
            values.append(user_id)
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT notification_channels.*, users.email AS owner_email
                FROM notification_channels
                JOIN users ON users.id = notification_channels.user_id
                {where}
                ORDER BY notification_channels.id DESC
                """,
                values,
            ).fetchall()
            return [_public_notification_channel(row) for row in rows]

    def create_notification(
        self,
        user_id: int,
        event_type: str,
        title: str,
        message: str,
        payload: dict[str, Any] | None = None,
        channel_type: str = "local",
        status: str = "stored",
        destination: str = "",
        delivered_at: str | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO notifications (
                    user_id, channel_type, event_type, title, message, status, payload,
                    destination, created_at, delivered_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    channel_type.strip().lower(),
                    event_type.strip(),
                    title.strip(),
                    message.strip(),
                    status.strip(),
                    json.dumps(payload or {}, sort_keys=True),
                    destination.strip(),
                    now,
                    delivered_at,
                ),
            )
            notification_id = int(cursor.lastrowid)
            row = conn.execute(
                """
                SELECT notifications.*, users.email AS owner_email
                FROM notifications
                JOIN users ON users.id = notifications.user_id
                WHERE notifications.id = ?
                """,
                (notification_id,),
            ).fetchone()
            return _public_notification(row)

    def list_notifications(
        self,
        user_id: int,
        include_all: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        limit = _bounded_limit(limit)
        filters: list[str] = []
        values: list[Any] = []
        if not include_all:
            filters.append("notifications.user_id = ?")
            values.append(user_id)
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        values.append(limit)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT notifications.*, users.email AS owner_email
                FROM notifications
                JOIN users ON users.id = notifications.user_id
                {where}
                ORDER BY notifications.id DESC
                LIMIT ?
                """,
                values,
            ).fetchall()
            return [_public_notification(row) for row in rows]

    def update_notification_status(
        self,
        notification_id: int,
        user_id: int,
        status: str,
        payload: dict[str, Any] | None = None,
        delivered_at: str | None = None,
        include_all: bool = False,
    ) -> dict[str, Any] | None:
        updates = ["status = ?"]
        values: list[Any] = [status.strip()]
        if payload is not None:
            updates.append("payload = ?")
            values.append(json.dumps(payload, sort_keys=True))
        if delivered_at is not None:
            updates.append("delivered_at = ?")
            values.append(delivered_at)
        values.append(notification_id)
        if not include_all:
            values.append(user_id)

        where = "id = ?" if include_all else "id = ? AND user_id = ?"
        with self.connect() as conn:
            cursor = conn.execute(f"UPDATE notifications SET {', '.join(updates)} WHERE {where}", values)
            if cursor.rowcount == 0:
                return None
            row = conn.execute(
                """
                SELECT notifications.*, users.email AS owner_email
                FROM notifications
                JOIN users ON users.id = notifications.user_id
                WHERE notifications.id = ?
                """,
                (notification_id,),
            ).fetchone()
            return _public_notification(row) if row else None

    def list_trade_records(
        self,
        user_id: int,
        include_all: bool = False,
        limit: int = 100,
        trade_ids: list[int] | None = None,
        run_id: int | None = None,
    ) -> list[dict[str, Any]]:
        limit = _bounded_limit(limit)
        with self.connect() as conn:
            filters: list[str] = []
            values: list[Any] = []
            if not include_all:
                filters.append("trade_records.user_id = ?")
                values.append(user_id)
            if trade_ids is not None:
                if not trade_ids:
                    return []
                placeholders = ",".join("?" for _ in trade_ids)
                filters.append(f"trade_records.id IN ({placeholders})")
                values.extend(trade_ids)
            if run_id is not None:
                filters.append("trade_records.run_id = ?")
                values.append(run_id)

            where = f"WHERE {' AND '.join(filters)}" if filters else ""
            values.append(limit)
            rows = conn.execute(
                f"""
                SELECT trade_records.*, users.email AS owner_email, strategy_configs.name AS strategy_name
                FROM trade_records
                JOIN users ON users.id = trade_records.user_id
                LEFT JOIN strategy_configs ON strategy_configs.id = trade_records.strategy_config_id
                {where}
                ORDER BY trade_records.id DESC
                LIMIT ?
                """,
                values,
            ).fetchall()
            return [_public_trade_record(row) for row in rows]

    def get_or_create_risk_settings(self, user_id: int) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO user_risk_settings (
                    user_id, paper_trading_enabled, live_trading_enabled, max_order_notional,
                    max_daily_loss, max_symbol_weight, max_gross_exposure,
                    max_open_positions, max_leverage, max_consecutive_losses,
                    require_order_confirmation, allow_reverse_position, allow_duplicate_position,
                    auto_disable_on_daily_loss, require_live_confirmation, updated_at
                )
                VALUES (?, 1, 0, 2500, 500, 0.35, 0.80, 5, 3, 3, 0, 0, 0, 1, 1, ?)
                """,
                (user_id, now),
            )
            row = conn.execute("SELECT * FROM user_risk_settings WHERE user_id = ?", (user_id,)).fetchone()
            return _public_risk_settings(row)

    def update_risk_settings(
        self,
        user_id: int,
        paper_trading_enabled: bool | None = None,
        live_trading_enabled: bool | None = None,
        max_order_notional: float | None = None,
        max_daily_loss: float | None = None,
        max_symbol_weight: float | None = None,
        max_gross_exposure: float | None = None,
        max_open_positions: int | None = None,
        max_leverage: float | None = None,
        max_consecutive_losses: int | None = None,
        require_order_confirmation: bool | None = None,
        allow_reverse_position: bool | None = None,
        allow_duplicate_position: bool | None = None,
        auto_disable_on_daily_loss: bool | None = None,
        require_live_confirmation: bool | None = None,
    ) -> dict[str, Any]:
        self.get_or_create_risk_settings(user_id)
        updates: list[str] = []
        values: list[Any] = []

        if paper_trading_enabled is not None:
            updates.append("paper_trading_enabled = ?")
            values.append(1 if paper_trading_enabled else 0)
        if live_trading_enabled is not None:
            updates.append("live_trading_enabled = ?")
            values.append(1 if live_trading_enabled else 0)
        if max_order_notional is not None:
            updates.append("max_order_notional = ?")
            values.append(float(max_order_notional))
        if max_daily_loss is not None:
            updates.append("max_daily_loss = ?")
            values.append(float(max_daily_loss))
        if max_symbol_weight is not None:
            updates.append("max_symbol_weight = ?")
            values.append(float(max_symbol_weight))
        if max_gross_exposure is not None:
            updates.append("max_gross_exposure = ?")
            values.append(float(max_gross_exposure))
        if max_open_positions is not None:
            updates.append("max_open_positions = ?")
            values.append(int(max_open_positions))
        if max_leverage is not None:
            updates.append("max_leverage = ?")
            values.append(float(max_leverage))
        if max_consecutive_losses is not None:
            updates.append("max_consecutive_losses = ?")
            values.append(int(max_consecutive_losses))
        if require_order_confirmation is not None:
            updates.append("require_order_confirmation = ?")
            values.append(1 if require_order_confirmation else 0)
        if allow_reverse_position is not None:
            updates.append("allow_reverse_position = ?")
            values.append(1 if allow_reverse_position else 0)
        if allow_duplicate_position is not None:
            updates.append("allow_duplicate_position = ?")
            values.append(1 if allow_duplicate_position else 0)
        if auto_disable_on_daily_loss is not None:
            updates.append("auto_disable_on_daily_loss = ?")
            values.append(1 if auto_disable_on_daily_loss else 0)
        if require_live_confirmation is not None:
            updates.append("require_live_confirmation = ?")
            values.append(1 if require_live_confirmation else 0)

        if updates:
            updates.append("updated_at = ?")
            values.append(utc_now())
            values.append(user_id)
            with self.connect() as conn:
                conn.execute(f"UPDATE user_risk_settings SET {', '.join(updates)} WHERE user_id = ?", values)

        return self.get_or_create_risk_settings(user_id)

    def monitoring_summary(self, user_id: int, include_all: bool = False) -> dict[str, Any]:
        user_filter = "" if include_all else "WHERE user_id = ?"
        user_values: tuple[Any, ...] = () if include_all else (user_id,)
        with self.connect() as conn:
            run_row = conn.execute(
                f"""
                SELECT
                    COUNT(*) AS total_runs,
                    SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed_runs,
                    SUM(CASE WHEN status != 'completed' THEN 1 ELSE 0 END) AS non_completed_runs
                FROM trade_runs
                {user_filter}
                """,
                user_values,
            ).fetchone()
            trade_row = conn.execute(
                f"""
                SELECT
                    COUNT(*) AS total_trades,
                    COALESCE(SUM(notional), 0) AS total_notional,
                    COALESCE(SUM(fee), 0) AS total_fees
                FROM trade_records
                {user_filter}
                """,
                user_values,
            ).fetchone()
            config_row = conn.execute(
                f"""
                SELECT COUNT(*) AS strategy_configs
                FROM strategy_configs
                {user_filter + (" AND " if user_filter else "WHERE ") + "is_active = 1"}
                """,
                user_values,
            ).fetchone()
            api_key_row = conn.execute(
                f"""
                SELECT COUNT(*) AS api_keys
                FROM exchange_api_keys
                {user_filter + (" AND " if user_filter else "WHERE ") + "is_active = 1"}
                """,
                user_values,
            ).fetchone()
            latest_run = conn.execute(
                f"""
                SELECT trade_runs.*, users.email AS owner_email, strategy_configs.name AS strategy_name
                FROM trade_runs
                JOIN users ON users.id = trade_runs.user_id
                LEFT JOIN strategy_configs ON strategy_configs.id = trade_runs.strategy_config_id
                {user_filter.replace("user_id", "trade_runs.user_id")}
                ORDER BY trade_runs.id DESC
                LIMIT 1
                """,
                user_values,
            ).fetchone()

        return {
            "scope": "all" if include_all else "self",
            "total_runs": int(run_row["total_runs"] or 0),
            "completed_runs": int(run_row["completed_runs"] or 0),
            "non_completed_runs": int(run_row["non_completed_runs"] or 0),
            "total_trades": int(trade_row["total_trades"] or 0),
            "total_notional": float(trade_row["total_notional"] or 0.0),
            "total_fees": float(trade_row["total_fees"] or 0.0),
            "strategy_configs": int(config_row["strategy_configs"] or 0),
            "api_keys": int(api_key_row["api_keys"] or 0),
            "latest_run": _public_trade_run(latest_run) if latest_run else None,
        }

    def list_watchlists(self, user_id: int, include_all: bool = False) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if include_all:
                rows = conn.execute(
                    """
                    SELECT watchlists.*, users.email AS owner_email
                    FROM watchlists
                    JOIN users ON users.id = watchlists.user_id
                    WHERE watchlists.is_active = 1
                    ORDER BY watchlists.is_default DESC, watchlists.updated_at DESC
                    """
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT watchlists.*, users.email AS owner_email
                    FROM watchlists
                    JOIN users ON users.id = watchlists.user_id
                    WHERE watchlists.user_id = ?
                      AND watchlists.is_active = 1
                    ORDER BY watchlists.is_default DESC, watchlists.updated_at DESC
                    """,
                    (user_id,),
                ).fetchall()
            return [_public_watchlist(row) for row in rows]

    def create_watchlist(
        self,
        user_id: int,
        name: str,
        symbols: list[str],
        notes: str = "",
        is_default: bool = False,
    ) -> dict[str, Any]:
        normalized_symbols = _normalize_symbols(symbols)
        if not normalized_symbols:
            raise ValueError("watchlist_symbols_required")
        now = utc_now()
        with self.connect() as conn:
            if is_default:
                conn.execute("UPDATE watchlists SET is_default = 0 WHERE user_id = ?", (user_id,))
            cursor = conn.execute(
                """
                INSERT INTO watchlists (
                    user_id, name, symbols, notes, is_default, is_active, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    user_id,
                    name.strip(),
                    json.dumps(normalized_symbols),
                    notes.strip(),
                    1 if is_default else 0,
                    now,
                    now,
                ),
            )
            watchlist_id = int(cursor.lastrowid)
        watchlist = self.get_watchlist(watchlist_id, user_id=user_id, is_admin=True)
        if not watchlist:
            raise KeyError(watchlist_id)
        return watchlist

    def get_watchlist(self, watchlist_id: int, user_id: int, is_admin: bool = False) -> dict[str, Any] | None:
        with self.connect() as conn:
            if is_admin:
                row = conn.execute(
                    """
                    SELECT watchlists.*, users.email AS owner_email
                    FROM watchlists
                    JOIN users ON users.id = watchlists.user_id
                    WHERE watchlists.id = ?
                      AND watchlists.is_active = 1
                    """,
                    (watchlist_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT watchlists.*, users.email AS owner_email
                    FROM watchlists
                    JOIN users ON users.id = watchlists.user_id
                    WHERE watchlists.id = ?
                      AND watchlists.user_id = ?
                      AND watchlists.is_active = 1
                    """,
                    (watchlist_id, user_id),
                ).fetchone()
            return _public_watchlist(row) if row else None

    def update_watchlist(
        self,
        watchlist_id: int,
        user_id: int,
        is_admin: bool = False,
        name: str | None = None,
        symbols: list[str] | None = None,
        notes: str | None = None,
        is_default: bool | None = None,
    ) -> dict[str, Any] | None:
        existing = self.get_watchlist(watchlist_id, user_id=user_id, is_admin=is_admin)
        if not existing:
            return None
        updates: list[str] = []
        values: list[Any] = []
        if name is not None:
            updates.append("name = ?")
            values.append(name.strip())
        if symbols is not None:
            normalized_symbols = _normalize_symbols(symbols)
            if not normalized_symbols:
                raise ValueError("watchlist_symbols_required")
            updates.append("symbols = ?")
            values.append(json.dumps(normalized_symbols))
        if notes is not None:
            updates.append("notes = ?")
            values.append(notes.strip())
        if is_default is not None:
            updates.append("is_default = ?")
            values.append(1 if is_default else 0)
        if not updates:
            return existing

        updates.append("updated_at = ?")
        values.append(utc_now())
        values.append(watchlist_id)
        with self.connect() as conn:
            if is_default:
                conn.execute("UPDATE watchlists SET is_default = 0 WHERE user_id = ?", (existing["user_id"],))
            conn.execute(f"UPDATE watchlists SET {', '.join(updates)} WHERE id = ?", values)
        return self.get_watchlist(watchlist_id, user_id=user_id, is_admin=is_admin)

    def delete_watchlist(self, watchlist_id: int, user_id: int, is_admin: bool = False) -> bool:
        existing = self.get_watchlist(watchlist_id, user_id=user_id, is_admin=is_admin)
        if not existing:
            return False
        with self.connect() as conn:
            conn.execute(
                "UPDATE watchlists SET is_active = 0, updated_at = ? WHERE id = ?",
                (utc_now(), watchlist_id),
            )
        return True

    def get_or_create_paper_account(self, user_id: int, initial_cash: float = 10_000.0) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO paper_accounts (user_id, cash, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (user_id, float(initial_cash), now, now),
            )
            row = conn.execute(
                """
                SELECT paper_accounts.*, users.email AS owner_email
                FROM paper_accounts
                JOIN users ON users.id = paper_accounts.user_id
                WHERE paper_accounts.user_id = ?
                """,
                (user_id,),
            ).fetchone()
            return _public_paper_account(row)

    def list_paper_accounts(self, user_id: int, include_all: bool = False) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if include_all:
                rows = conn.execute(
                    """
                    SELECT paper_accounts.*, users.email AS owner_email
                    FROM paper_accounts
                    JOIN users ON users.id = paper_accounts.user_id
                    ORDER BY users.id
                    """
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT paper_accounts.*, users.email AS owner_email
                    FROM paper_accounts
                    JOIN users ON users.id = paper_accounts.user_id
                    WHERE paper_accounts.user_id = ?
                    """,
                    (user_id,),
                ).fetchall()
            return [_public_paper_account(row) for row in rows]

    def list_paper_positions(
        self,
        user_id: int,
        include_all: bool = False,
        include_closed: bool = False,
    ) -> list[dict[str, Any]]:
        quantity_filter = "" if include_closed else "AND ABS(paper_positions.quantity) > 1e-12"
        with self.connect() as conn:
            if include_all:
                rows = conn.execute(
                    f"""
                    SELECT paper_positions.*, users.email AS owner_email
                    FROM paper_positions
                    JOIN users ON users.id = paper_positions.user_id
                    WHERE 1 = 1
                      {quantity_filter}
                    ORDER BY paper_positions.symbol
                    """
                ).fetchall()
            else:
                rows = conn.execute(
                    f"""
                    SELECT paper_positions.*, users.email AS owner_email
                    FROM paper_positions
                    JOIN users ON users.id = paper_positions.user_id
                    WHERE paper_positions.user_id = ?
                      {quantity_filter}
                    ORDER BY paper_positions.symbol
                    """,
                    (user_id,),
                ).fetchall()
            return [_public_paper_position(row) for row in rows]

    def get_paper_position(self, user_id: int, symbol: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT paper_positions.*, users.email AS owner_email
                FROM paper_positions
                JOIN users ON users.id = paper_positions.user_id
                WHERE paper_positions.user_id = ?
                  AND paper_positions.symbol = ?
                """,
                (user_id, symbol.upper()),
            ).fetchone()
            return _public_paper_position(row) if row else None

    def apply_paper_fill(
        self,
        user_id: int,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
        fee: float = 0.0,
        initial_cash: float = 10_000.0,
    ) -> dict[str, Any]:
        symbol = symbol.upper()
        side = side.lower()
        quantity = float(quantity)
        price = float(price)
        fee = float(fee)
        if quantity <= 0 or price <= 0:
            raise ValueError("quantity and price must be positive")
        if side not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")

        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO paper_accounts (user_id, cash, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (user_id, float(initial_cash), now, now),
            )
            account = conn.execute("SELECT * FROM paper_accounts WHERE user_id = ?", (user_id,)).fetchone()
            position = conn.execute(
                "SELECT * FROM paper_positions WHERE user_id = ? AND symbol = ?",
                (user_id, symbol),
            ).fetchone()

            cash = float(account["cash"])
            old_quantity = float(position["quantity"]) if position else 0.0
            average_price = float(position["average_price"]) if position else 0.0
            realized_pnl = float(position["realized_pnl"]) if position else 0.0
            realized_pnl_before = realized_pnl
            notional = quantity * price

            if side == "buy":
                total_cost = notional + fee
                if total_cost > cash + 1e-9:
                    raise ValueError("insufficient_paper_cash")
                new_quantity = old_quantity + quantity
                old_cost = old_quantity * average_price
                average_price = (old_cost + notional) / new_quantity if new_quantity > 0 else 0.0
                cash -= total_cost
            else:
                if quantity > old_quantity + 1e-9:
                    raise ValueError("insufficient_paper_position")
                realized_pnl += (price - average_price) * quantity - fee
                new_quantity = old_quantity - quantity
                cash += notional - fee
                if new_quantity <= 1e-12:
                    new_quantity = 0.0
                    average_price = 0.0

            conn.execute(
                """
                UPDATE paper_accounts
                SET cash = ?, updated_at = ?
                WHERE user_id = ?
                """,
                (cash, now, user_id),
            )
            conn.execute(
                """
                INSERT INTO paper_positions (user_id, symbol, quantity, average_price, realized_pnl, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, symbol)
                DO UPDATE SET
                    quantity = excluded.quantity,
                    average_price = excluded.average_price,
                    realized_pnl = excluded.realized_pnl,
                    updated_at = excluded.updated_at
                """,
                (user_id, symbol, new_quantity, average_price, realized_pnl, now),
            )

        return {
            "account": self.get_or_create_paper_account(user_id, initial_cash=initial_cash),
            "position": self.get_paper_position(user_id, symbol),
            "realized_pnl_delta": realized_pnl - realized_pnl_before,
        }

    def _public_api_key(self, row: sqlite3.Row) -> dict[str, Any]:
        api_key_value = self.secret_box.decrypt(row["api_key_enc"])
        return {
            "id": row["id"],
            "user_id": row["user_id"],
            "owner_email": row["owner_email"],
            "exchange": row["exchange"],
            "label": row["label"],
            "api_key_masked": mask_secret(api_key_value),
            "scopes": json.loads(row["scopes"] or "[]"),
            "can_trade": bool(row["can_trade"]),
            "is_active": bool(row["is_active"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "last_used_at": row["last_used_at"],
        }


def _public_user(row: sqlite3.Row) -> dict[str, Any]:
    grants = _json_list(row, "permission_grants")
    revokes = _json_list(row, "permission_revokes")
    return {
        "id": row["id"],
        "email": row["email"],
        "role": row["role"],
        "status": row["status"],
        "permission_grants": grants,
        "permission_revokes": revokes,
        "permissions": sorted(permissions_for_user(row["role"], grants, revokes)),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _public_strategy_config(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "owner_email": row["owner_email"],
        "name": row["name"],
        "strategy_type": row["strategy_type"],
        "market_type": row["market_type"],
        "parameters": json.loads(row["parameters"] or "{}"),
        "current_version": row["current_version"],
        "is_default": bool(row["is_default"]),
        "is_active": bool(row["is_active"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _public_strategy_config_version(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "strategy_config_id": row["strategy_config_id"],
        "user_id": row["user_id"],
        "owner_email": row["owner_email"],
        "version": row["version"],
        "name": row["name"],
        "strategy_type": row["strategy_type"],
        "market_type": row["market_type"],
        "parameters": json.loads(row["parameters"] or "{}"),
        "change_note": row["change_note"],
        "created_at": row["created_at"],
    }


def _public_trade_run(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "owner_email": row["owner_email"],
        "strategy_config_id": row["strategy_config_id"],
        "strategy_name": row["strategy_name"],
        "strategy_version": row["strategy_version"],
        "strategy_type": row["strategy_type"],
        "run_mode": row["run_mode"],
        "status": row["status"],
        "market_type": row["market_type"],
        "bars_file": row["bars_file"],
        "events_file": row["events_file"],
        "parameters": json.loads(row["parameters"] or "{}"),
        "metrics": json.loads(row["metrics"] or "{}"),
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "created_at": row["created_at"],
    }


def _public_trade_record(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "run_id": row["run_id"],
        "user_id": row["user_id"],
        "owner_email": row["owner_email"],
        "strategy_config_id": row["strategy_config_id"],
        "strategy_name": row["strategy_name"],
        "run_mode": row["run_mode"],
        "exchange": row["exchange"],
        "market_type": row["market_type"],
        "symbol": row["symbol"],
        "side": row["side"],
        "quantity": row["quantity"],
        "price": row["price"],
        "notional": row["notional"],
        "fee": row["fee"],
        "realized_pnl": row["realized_pnl"],
        "order_id": row["order_id"],
        "reason": row["reason"],
        "status": row["status"],
        "traded_at": row["traded_at"],
        "created_at": row["created_at"],
    }


def _public_trade_order(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "client_order_id": row["client_order_id"],
        "exchange_order_id": row["exchange_order_id"],
        "user_id": row["user_id"],
        "owner_email": row["owner_email"],
        "strategy_config_id": row["strategy_config_id"],
        "strategy_name": row["strategy_name"],
        "run_id": row["run_id"],
        "run_mode": row["run_mode"],
        "exchange": row["exchange"],
        "market_type": row["market_type"],
        "symbol": row["symbol"],
        "side": row["side"],
        "order_type": row["order_type"],
        "time_in_force": row["time_in_force"],
        "post_only": bool(row["post_only"]),
        "reduce_only": bool(row["reduce_only"]),
        "quantity": row["quantity"],
        "requested_price": row["requested_price"],
        "limit_price": row["limit_price"],
        "stop_price": row["stop_price"],
        "trailing_delta_pct": row["trailing_delta_pct"],
        "leverage": row["leverage"],
        "filled_quantity": row["filled_quantity"],
        "average_fill_price": row["average_fill_price"],
        "status": row["status"],
        "reason": row["reason"],
        "error_message": row["error_message"],
        "raw_response": json.loads(row["raw_response"] or "{}"),
        "created_at": row["created_at"],
        "submitted_at": row["submitted_at"],
        "updated_at": row["updated_at"],
        "finished_at": row["finished_at"],
    }


def _public_audit_log(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "owner_email": row["owner_email"],
        "action": row["action"],
        "entity_type": row["entity_type"],
        "entity_id": row["entity_id"],
        "status": row["status"],
        "detail": json.loads(row["detail"] or "{}"),
        "ip_address": row["ip_address"],
        "user_agent": row["user_agent"],
        "created_at": row["created_at"],
    }


def _public_exchange_sync_run(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "owner_email": row["owner_email"],
        "exchange": row["exchange"],
        "run_mode": row["run_mode"],
        "status": row["status"],
        "local_snapshot": json.loads(row["local_snapshot"] or "{}"),
        "exchange_snapshot": json.loads(row["exchange_snapshot"] or "{}"),
        "differences": json.loads(row["differences"] or "[]"),
        "created_at": row["created_at"],
    }


def _public_realtime_market_snapshot(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "symbol": row["symbol"],
        "source": row["source"],
        "best_bid": row["best_bid"],
        "best_ask": row["best_ask"],
        "last_price": row["last_price"],
        "spread_bps": row["spread_bps"],
        "order_book": json.loads(row["order_book"] or "{}"),
        "recent_trades": json.loads(row["recent_trades"] or "[]"),
        "kline": json.loads(row["kline"] or "{}"),
        "created_at": row["created_at"],
    }


def _public_signal_log(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "owner_email": row["owner_email"],
        "strategy_config_id": row["strategy_config_id"],
        "strategy_name": row["strategy_name"],
        "strategy_type": row["strategy_type"],
        "symbol": row["symbol"],
        "signal": row["signal"],
        "score": row["score"],
        "parameters": json.loads(row["parameters"] or "{}"),
        "market_snapshot": json.loads(row["market_snapshot"] or "{}"),
        "reason": row["reason"],
        "created_at": row["created_at"],
    }


def _public_automation_control(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "owner_email": row["owner_email"],
        "strategy_config_id": row["strategy_config_id"],
        "strategy_name": row["strategy_name"],
        "run_mode": row["run_mode"],
        "status": row["status"],
        "trading_enabled": bool(row["trading_enabled"]),
        "last_action": row["last_action"],
        "updated_at": row["updated_at"],
        "created_at": row["created_at"],
    }


def _public_system_flag(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "key": row["key"],
        "value": row["value"],
        "updated_by": row["updated_by"],
        "updated_at": row["updated_at"],
    }


def _public_execution_event(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "owner_email": row["owner_email"],
        "order_id": row["order_id"],
        "run_id": row["run_id"],
        "event_type": row["event_type"],
        "status": row["status"],
        "price_before": row["price_before"],
        "price_after": row["price_after"],
        "risk_checks": json.loads(row["risk_checks"] or "[]"),
        "strategy_snapshot": json.loads(row["strategy_snapshot"] or "{}"),
        "raw_response": json.loads(row["raw_response"] or "{}"),
        "error_message": row["error_message"],
        "retry_count": row["retry_count"],
        "created_at": row["created_at"],
    }


def _public_notification_channel(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "owner_email": row["owner_email"],
        "channel_type": row["channel_type"],
        "label": row["label"],
        "destination": row["destination"],
        "is_active": bool(row["is_active"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _public_notification(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "owner_email": row["owner_email"],
        "channel_type": row["channel_type"],
        "event_type": row["event_type"],
        "title": row["title"],
        "message": row["message"],
        "status": row["status"],
        "payload": json.loads(row["payload"] or "{}"),
        "destination": row["destination"],
        "created_at": row["created_at"],
        "delivered_at": row["delivered_at"],
    }


def _public_paper_account(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "user_id": row["user_id"],
        "owner_email": row["owner_email"],
        "cash": row["cash"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _public_paper_position(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "owner_email": row["owner_email"],
        "symbol": row["symbol"],
        "quantity": row["quantity"],
        "average_price": row["average_price"],
        "realized_pnl": row["realized_pnl"],
        "updated_at": row["updated_at"],
    }


def _public_risk_settings(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "user_id": row["user_id"],
        "paper_trading_enabled": bool(row["paper_trading_enabled"]),
        "live_trading_enabled": bool(row["live_trading_enabled"]),
        "max_order_notional": row["max_order_notional"],
        "max_daily_loss": row["max_daily_loss"],
        "max_symbol_weight": row["max_symbol_weight"],
        "max_gross_exposure": row["max_gross_exposure"],
        "max_open_positions": row["max_open_positions"],
        "max_leverage": row["max_leverage"],
        "max_consecutive_losses": row["max_consecutive_losses"],
        "require_order_confirmation": bool(row["require_order_confirmation"]),
        "allow_reverse_position": bool(row["allow_reverse_position"]),
        "allow_duplicate_position": bool(row["allow_duplicate_position"]),
        "auto_disable_on_daily_loss": bool(row["auto_disable_on_daily_loss"]),
        "require_live_confirmation": bool(row["require_live_confirmation"]),
        "updated_at": row["updated_at"],
    }


def _public_symbol_rule(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "exchange": row["exchange"],
        "market_type": row["market_type"],
        "symbol": row["symbol"],
        "min_quantity": row["min_quantity"],
        "quantity_step": row["quantity_step"],
        "price_tick": row["price_tick"],
        "min_notional": row["min_notional"],
        "max_notional": row["max_notional"],
        "max_leverage": row["max_leverage"],
        "is_active": bool(row["is_active"]),
        "source": row["source"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _public_watchlist(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "owner_email": row["owner_email"],
        "name": row["name"],
        "symbols": json.loads(row["symbols"] or "[]"),
        "notes": row["notes"],
        "is_default": bool(row["is_default"]),
        "is_active": bool(row["is_active"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _bounded_limit(limit: int, upper: int = 500) -> int:
    return max(1, min(int(limit), upper))


def _normalize_status(status: str) -> str:
    normalized = status.strip().lower()
    if normalized not in {"active", "disabled"}:
        raise ValueError(f"unknown status: {status}")
    return normalized


def _normalize_permission_overrides(
    permission_grants: list[str] | None,
    permission_revokes: list[str] | None,
) -> tuple[list[str], list[str]]:
    revokes = normalize_permissions(permission_revokes)
    revoked = set(revokes)
    grants = [permission for permission in normalize_permissions(permission_grants) if permission not in revoked]
    return grants, revokes


def _json_list(row: sqlite3.Row, key: str) -> list[str]:
    if key not in row.keys():
        return []
    value = row[key]
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _normalize_symbols(symbols: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for symbol in symbols:
        value = str(symbol).strip().upper().replace(" ", "")
        if not value or value in seen:
            continue
        normalized.append(value)
        seen.add(value)
    return normalized
