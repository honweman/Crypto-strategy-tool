from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import math
import os
from itertools import product
from pathlib import Path
from typing import Any, Callable, Optional

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect, status
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .broker import SimulatedBroker
from .data import CsvBarDataSource, CsvExternalEventSource, MergedEventFeed
from .engine import BacktestConfig, BacktestEngine
from .exchange_rules import fetch_exchange_symbol_rules, list_rule_sources
from .execution import PaperExchangeConnector
from .indicators import calculate_indicator_points, indicator_catalog
from .market_data import (
    MarketDataRequest,
    default_output_path,
    fetch_ohlcv,
    list_market_data_sources,
    write_bars_csv,
)
from .market_quotes import fetch_market_quote, list_market_quote_sources
from .models import Order, OrderStatus, OrderType, Side, TimeInForce
from .permissions import (
    ALL_PERMISSIONS,
    EXECUTE_LIVE_TRADES,
    EXECUTE_PAPER_TRADES,
    MANAGE_ALL_API_KEYS,
    MANAGE_ALL_STRATEGIES,
    MANAGE_OWN_API_KEYS,
    MANAGE_OWN_STRATEGIES,
    MANAGE_SYSTEM,
    MANAGE_USERS,
    ROLE_PERMISSIONS,
    RUN_BACKTESTS,
    VIEW_ALL_TRADES,
    VIEW_MONITORING,
    VIEW_OWN_TRADES,
    VIEW_STRATEGIES,
    has_user_permission,
    permissions_for_user,
)
from .risk import RiskConfig, RiskManager
from .security import SecretBox, verify_password
from .store import Database, utc_now
from .strategy_registry import create_strategy, list_strategy_definitions, validate_parameters
from .strategy_tools import build_pmm_quote_plan, calculate_spot_perp_arbitrage, calculate_xemm_opportunity

SESSION_COOKIE = "crypto_lab_session"
DEFAULT_DB_PATH = Path("data/crypto_lab.sqlite3")
PROJECT_ROOT = Path(__file__).resolve().parents[2]
STATIC_DIR = Path(__file__).resolve().parent / "static"


class LoginRequest(BaseModel):
    email: str
    password: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=128)


class CreateUserRequest(BaseModel):
    email: str
    password: str
    role: str = "viewer"
    status: str = "active"
    permission_grants: list[str] = Field(default_factory=list)
    permission_revokes: list[str] = Field(default_factory=list)


class UpdateUserRequest(BaseModel):
    role: Optional[str] = None
    status: Optional[str] = None
    permission_grants: Optional[list[str]] = None
    permission_revokes: Optional[list[str]] = None


class CreateApiKeyRequest(BaseModel):
    exchange: str
    label: str
    api_key: str
    api_secret: str
    passphrase: str = ""
    scopes: list[str] = Field(default_factory=list)
    can_trade: bool = False


class CreateStrategyConfigRequest(BaseModel):
    name: str
    strategy_type: str = "statistical_event"
    market_type: str = "spot"
    parameters: dict[str, Any] = Field(default_factory=dict)
    is_default: bool = False


class UpdateStrategyConfigRequest(BaseModel):
    name: Optional[str] = None
    strategy_type: Optional[str] = None
    market_type: Optional[str] = None
    parameters: Optional[dict[str, Any]] = None
    is_default: Optional[bool] = None


class BacktestRunRequest(BaseModel):
    bars_file: str = "data/sample_bars.csv"
    events_file: Optional[str] = "data/sample_events.csv"
    strategy_config_id: Optional[int] = None
    strategy_type: str = "statistical_event"
    market_type: str = "spot"
    strategy_parameters: dict[str, Any] = Field(default_factory=dict)
    cash: float = 10_000.0
    fee_bps: float = 5.0
    slippage_bps: float = 2.0
    execution_delay_bars: int = Field(default=0, ge=0, le=100)
    funding_bps_per_bar: float = 0.0
    stop_loss_pct: float = Field(default=0.0, ge=0.0, le=1.0)
    take_profit_pct: float = Field(default=0.0, ge=0.0, le=10.0)
    max_holding_bars: int = Field(default=0, ge=0, le=100_000)
    leverage: float = Field(default=1.0, ge=1.0, le=125.0)
    maintenance_margin_ratio: float = Field(default=0.005, ge=0.0, le=1.0)
    out_of_sample_ratio: float = Field(default=0.0, ge=0.0, le=0.9)
    lookback: int = 20
    entry_z: float = -1.2
    exit_z: float = 0.2
    base_weight: float = 0.20
    event_weight: float = 0.25
    event_threshold: float = 0.60
    max_order_notional: float = 2_500.0
    max_symbol_weight: float = 0.35
    max_gross_exposure: float = 0.80


class WalkForwardBacktestRequest(BacktestRunRequest):
    train_events: int = Field(default=120, ge=2, le=100_000)
    test_events: int = Field(default=40, ge=1, le=100_000)
    step_events: int = Field(default=40, ge=1, le=100_000)
    max_windows: int = Field(default=12, ge=1, le=100)


class GridSearchRequest(BaseModel):
    bars_file: str = "data/sample_bars.csv"
    events_file: Optional[str] = "data/sample_events.csv"
    strategy_config_id: Optional[int] = None
    strategy_type: str = "statistical_event"
    market_type: str = "spot"
    strategy_parameters: dict[str, Any] = Field(default_factory=dict)
    parameter_grid: dict[str, list[Any]] = Field(default_factory=dict)
    max_combinations: int = Field(default=50, ge=1, le=200)
    cash: float = 10_000.0
    fee_bps: float = 5.0
    slippage_bps: float = 2.0
    execution_delay_bars: int = Field(default=0, ge=0, le=100)
    funding_bps_per_bar: float = 0.0
    stop_loss_pct: float = Field(default=0.0, ge=0.0, le=1.0)
    take_profit_pct: float = Field(default=0.0, ge=0.0, le=10.0)
    max_holding_bars: int = Field(default=0, ge=0, le=100_000)
    leverage: float = Field(default=1.0, ge=1.0, le=125.0)
    maintenance_margin_ratio: float = Field(default=0.005, ge=0.0, le=1.0)
    max_order_notional: float = 2_500.0
    max_symbol_weight: float = 0.35
    max_gross_exposure: float = 0.80


class ManualPaperOrderRequest(BaseModel):
    symbol: str
    action: str = "buy"
    quantity: Optional[float] = None
    price: float
    order_type: str = "market"
    time_in_force: str = "GTC"
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    trailing_delta_pct: Optional[float] = Field(default=None, ge=0)
    post_only: bool = False
    reduce_only: bool = False
    leverage: float = Field(default=1.0, ge=1.0, le=125.0)
    maintenance_margin_ratio: float = Field(default=0.005, ge=0.0, le=1.0)
    funding_bps_per_8h: float = 0.0
    confirm_order: bool = False
    fee_bps: float = 5.0
    slippage_bps: float = 2.0
    reason: str = "manual_order"
    market_type: str = "spot"


class TradePreflightRequest(ManualPaperOrderRequest):
    run_mode: str = "paper"


class AutoPaperExecuteRequest(BaseModel):
    bars_file: str = "data/sample_bars.csv"
    events_file: Optional[str] = "data/sample_events.csv"
    strategy_config_id: Optional[int] = None
    strategy_type: str = "statistical_event"
    strategy_parameters: dict[str, Any] = Field(default_factory=dict)
    fee_bps: float = 5.0
    slippage_bps: float = 2.0
    max_order_notional: float = 2_500.0
    max_symbol_weight: float = 0.35
    max_gross_exposure: float = 0.80


class FetchMarketDataRequest(BaseModel):
    provider: str = "binance_spot"
    symbol: str = "BTCUSDT"
    interval: str = "1h"
    start: Optional[str] = None
    end: Optional[str] = None
    limit: int = Field(default=200, ge=1, le=1500)
    output_file: Optional[str] = None


class UpdateRiskSettingsRequest(BaseModel):
    paper_trading_enabled: Optional[bool] = None
    live_trading_enabled: Optional[bool] = None
    max_order_notional: Optional[float] = Field(default=None, ge=0)
    max_daily_loss: Optional[float] = Field(default=None, ge=0)
    max_symbol_weight: Optional[float] = Field(default=None, ge=0, le=1)
    max_gross_exposure: Optional[float] = Field(default=None, ge=0, le=1)
    max_open_positions: Optional[int] = Field(default=None, ge=0, le=1_000)
    max_leverage: Optional[float] = Field(default=None, ge=1, le=125)
    max_consecutive_losses: Optional[int] = Field(default=None, ge=0, le=1_000)
    require_order_confirmation: Optional[bool] = None
    allow_reverse_position: Optional[bool] = None
    allow_duplicate_position: Optional[bool] = None
    auto_disable_on_daily_loss: Optional[bool] = None
    require_live_confirmation: Optional[bool] = None


class ExchangeSyncRequest(BaseModel):
    exchange: str = "paper"
    run_mode: str = "paper"
    exchange_snapshot: Optional[dict[str, Any]] = None


class SymbolRuleRequest(BaseModel):
    exchange: str = "paper"
    market_type: str = "spot"
    symbol: str
    min_quantity: float = Field(default=0.0, ge=0)
    quantity_step: float = Field(default=0.0, ge=0)
    price_tick: float = Field(default=0.0, ge=0)
    min_notional: float = Field(default=0.0, ge=0)
    max_notional: float = Field(default=0.0, ge=0)
    max_leverage: float = Field(default=1.0, ge=1, le=125)
    is_active: bool = True
    source: str = "manual"


class SymbolRuleSyncRequest(BaseModel):
    provider: str = "binance_spot"
    symbol: Optional[str] = None
    limit: int = Field(default=200, ge=1, le=2000)


class PmmQuotePlanRequest(BaseModel):
    symbol: str = "BTCUSDT"
    exchange: str = "paper"
    market_type: str = "spot"
    mid_price: float = Field(default=100.0, gt=0)
    base_balance: float = Field(default=0.0, ge=0)
    quote_balance: float = Field(default=10_000.0, ge=0)
    order_amount: float = Field(default=0.0, ge=0)
    order_notional: float = Field(default=100.0, ge=0)
    bid_spread_bps: float = Field(default=10.0, ge=0)
    ask_spread_bps: float = Field(default=10.0, ge=0)
    minimum_spread_bps: float = Field(default=1.0, ge=0)
    order_levels: int = Field(default=1, ge=1, le=20)
    order_level_spread_bps: float = Field(default=0.0, ge=0)
    order_level_amount: float = Field(default=0.0, ge=0)
    inventory_skew_enabled: bool = True
    inventory_target_base_pct: float = Field(default=50.0, ge=0, le=100)
    inventory_range_multiplier: float = Field(default=50.0, ge=1)
    price_floor: float = Field(default=0.0, ge=0)
    price_ceiling: float = Field(default=0.0, ge=0)
    order_refresh_time: float = Field(default=60.0, ge=1)
    filled_order_delay: float = Field(default=60.0, ge=0)


class XemmOpportunityRequest(BaseModel):
    symbol: str = "BTCUSDT"
    maker_exchange: str = "binance"
    taker_exchange: str = "okx"
    maker_bid: float = Field(default=99.9, gt=0)
    maker_ask: float = Field(default=100.1, gt=0)
    taker_bid: float = Field(default=100.2, gt=0)
    taker_ask: float = Field(default=100.4, gt=0)
    order_amount: float = Field(default=1.0, ge=0)
    min_profitability_bps: float = 10.0
    maker_fee_bps: float = Field(default=2.0, ge=0)
    taker_fee_bps: float = Field(default=5.0, ge=0)
    slippage_buffer_bps: float = Field(default=5.0, ge=0)
    top_depth_tolerance: float = Field(default=0.0, ge=0)


class FundingArbitrageRequest(BaseModel):
    symbol: str = "BTCUSDT"
    spot_exchange: str = "binance"
    perp_exchange: str = "binance_usdm"
    spot_price: float = Field(default=100.0, gt=0)
    perp_price: float = Field(default=100.2, gt=0)
    notional: float = Field(default=1_000.0, ge=0)
    funding_bps_per_8h: float = 1.0
    holding_hours: float = Field(default=24.0, ge=0)
    funding_interval_hours: float = Field(default=8.0, gt=0)
    spot_fee_bps: float = Field(default=5.0, ge=0)
    perp_fee_bps: float = Field(default=5.0, ge=0)
    slippage_bps: float = Field(default=5.0, ge=0)
    borrow_bps_per_day: float = Field(default=0.0, ge=0)
    leverage: float = Field(default=3.0, ge=1, le=125)
    maintenance_margin_ratio: float = Field(default=0.005, ge=0, le=1)
    min_net_edge_bps: float = 5.0
    include_basis_edge: bool = True


class RealtimeSignalRequest(BaseModel):
    symbol: str = "BTCUSDT"
    strategy_config_id: Optional[int] = None
    strategy_type: str = "manual_signal"
    parameters: dict[str, Any] = Field(default_factory=dict)


class AutomationControlRequest(BaseModel):
    action: str = "pause"
    strategy_config_id: Optional[int] = None
    run_mode: str = "paper"


class AutomationRunOnceRequest(BaseModel):
    symbol: str = "BTCUSDT"
    strategy_config_id: Optional[int] = None
    strategy_type: str = "manual_signal"
    parameters: dict[str, Any] = Field(default_factory=dict)
    run_mode: str = "paper"
    order_notional: float = Field(default=100.0, gt=0)
    quantity: Optional[float] = Field(default=None, gt=0)
    fee_bps: float = Field(default=5.0, ge=0)
    slippage_bps: float = Field(default=2.0, ge=0)
    confirm_order: bool = False


class UserTradingSwitchRequest(BaseModel):
    paper_trading_enabled: Optional[bool] = None
    live_trading_enabled: Optional[bool] = None


class CloseAllPositionsRequest(BaseModel):
    run_mode: str = "paper"
    price_map: dict[str, float] = Field(default_factory=dict)
    reason: str = "close_all_positions"


class CreateNotificationChannelRequest(BaseModel):
    channel_type: str = "webhook"
    label: str
    destination: str = ""
    is_active: bool = True


class NotificationDispatchRequest(BaseModel):
    limit: int = Field(default=20, ge=1, le=100)
    dry_run: bool = True


class IndicatorAnalysisRequest(BaseModel):
    bars_file: str = "data/sample_bars.csv"
    symbol: Optional[str] = None
    fast_window: int = Field(default=12, ge=1, le=500)
    slow_window: int = Field(default=26, ge=2, le=1000)
    rsi_window: int = Field(default=14, ge=2, le=500)
    bollinger_window: int = Field(default=20, ge=2, le=500)
    atr_window: int = Field(default=14, ge=2, le=500)
    macd_signal_window: int = Field(default=9, ge=1, le=500)
    limit: int = Field(default=80, ge=1, le=500)


class CreateWatchlistRequest(BaseModel):
    name: str
    symbols: list[str] = Field(default_factory=list)
    notes: str = ""
    is_default: bool = False


class UpdateWatchlistRequest(BaseModel):
    name: Optional[str] = None
    symbols: Optional[list[str]] = None
    notes: Optional[str] = None
    is_default: Optional[bool] = None


def create_app(db_path: str | Path | None = None, secret_key: str | None = None) -> FastAPI:
    secret_box = SecretBox(secret_key or os.environ.get("CRYPTO_LAB_SECRET_KEY", "dev-secret-change-before-deploy"))
    db = Database(Path(db_path or os.environ.get("CRYPTO_LAB_DB", DEFAULT_DB_PATH)), secret_box)
    db.init_schema()
    _bootstrap_admin(db)

    app = FastAPI(title="Crypto Strategy Lab", version="0.1.0")
    app.state.db = db
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    def current_user(request: Request) -> dict:
        token = request.cookies.get(SESSION_COOKIE)
        user = db.get_session_user(token)
        if not user:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="not_authenticated")
        return user

    def require(permission: str) -> Callable:
        def dependency(user: dict = Depends(current_user)) -> dict:
            if not has_user_permission(user, permission):
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")
            return user

        return dependency

    def require_any(*permissions: str) -> Callable:
        def dependency(user: dict = Depends(current_user)) -> dict:
            if not any(has_user_permission(user, permission) for permission in permissions):
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")
            return user

        return dependency

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(
            STATIC_DIR / "index.html",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.post("/api/auth/login")
    def login(payload: LoginRequest, request: Request, response: Response) -> dict:
        login_user = db.get_user_for_login(payload.email)
        if not login_user or login_user["status"] != "active":
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_credentials")
        if not verify_password(payload.password, login_user["password_hash"]):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_credentials")

        token = db.create_session(login_user["id"])
        response.set_cookie(
            SESSION_COOKIE,
            token,
            httponly=True,
            samesite="lax",
            secure=os.environ.get("CRYPTO_LAB_COOKIE_SECURE", "0") == "1",
            max_age=86_400,
        )
        user = db.get_user(login_user["id"])
        db.create_audit_log(
            user_id=login_user["id"],
            action="auth.login",
            entity_type="session",
            status="ok",
            detail={"email": login_user["email"]},
            **_audit_context(request),
        )
        return _user_payload(user)

    @app.post("/api/auth/logout")
    def logout(request: Request, response: Response) -> dict:
        db.delete_session(request.cookies.get(SESSION_COOKIE))
        response.delete_cookie(SESSION_COOKIE)
        return {"ok": True}

    @app.get("/api/me")
    def me(user: dict = Depends(current_user)) -> dict:
        return _user_payload(user)

    @app.post("/api/me/password")
    def change_password(payload: ChangePasswordRequest, request: Request, user: dict = Depends(current_user)) -> dict:
        login_user = db.get_user_for_login(user["email"])
        if not login_user or not verify_password(payload.current_password, login_user["password_hash"]):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_current_password")
        updated = db.update_user_password(user["id"], payload.new_password)
        db.delete_user_sessions(user["id"], keep_token=request.cookies.get(SESSION_COOKIE))
        db.create_audit_log(
            user_id=user["id"],
            action="auth.change_password",
            entity_type="user",
            entity_id=user["id"],
            status="ok",
            **_audit_context(request),
        )
        return _user_payload(updated)

    @app.get("/api/permissions")
    def permissions(user: dict = Depends(current_user)) -> dict:
        return {
            "role": user["role"],
            "permissions": sorted(permissions_for_user(user["role"], user["permission_grants"], user["permission_revokes"])),
            "permission_catalog": sorted(ALL_PERMISSIONS),
            "role_permissions": {role: sorted(perms) for role, perms in ROLE_PERMISSIONS.items()},
        }

    @app.get("/api/data-sources/catalog")
    def data_sources(_: dict = Depends(require_any(VIEW_STRATEGIES, RUN_BACKTESTS))) -> list[dict]:
        return list_market_data_sources()

    @app.get("/api/market-quotes/sources")
    def market_quote_sources(_: dict = Depends(require_any(VIEW_MONITORING, VIEW_STRATEGIES, RUN_BACKTESTS, EXECUTE_PAPER_TRADES))) -> list[dict]:
        return list_market_quote_sources()

    @app.get("/api/market-quotes/quote")
    async def market_quote(
        provider: str = Query("binance_spot"),
        symbol: str = Query("BTCUSDT"),
        _: dict = Depends(require_any(VIEW_MONITORING, VIEW_STRATEGIES, RUN_BACKTESTS, EXECUTE_PAPER_TRADES)),
    ) -> dict:
        try:
            quote = await fetch_market_quote(provider, symbol)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"market_quote_request_failed:{exc}") from exc
        return quote.as_dict()

    @app.get("/api/analysis/indicator-catalog")
    def analysis_indicator_catalog(_: dict = Depends(require_any(VIEW_STRATEGIES, RUN_BACKTESTS))) -> list[dict]:
        return indicator_catalog()

    @app.post("/api/analysis/indicators")
    def analyze_indicators(payload: IndicatorAnalysisRequest, _: dict = Depends(require(RUN_BACKTESTS))) -> dict:
        bars_file = _safe_project_path(payload.bars_file)
        symbol_filter = {payload.symbol.strip().upper()} if payload.symbol else None
        bars = list(CsvBarDataSource(bars_file, symbols=symbol_filter))
        if not bars:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no_bars_for_indicator_analysis")

        grouped: dict[str, list] = {}
        for bar in bars:
            grouped.setdefault(bar.symbol, []).append(bar)

        results = []
        for symbol, symbol_bars in grouped.items():
            points = calculate_indicator_points(
                symbol_bars,
                fast_window=payload.fast_window,
                slow_window=payload.slow_window,
                rsi_window=payload.rsi_window,
                bollinger_window=payload.bollinger_window,
                atr_window=payload.atr_window,
                macd_signal_window=payload.macd_signal_window,
            )
            if not points:
                continue
            latest = points[-1].__dict__
            rows = [point.__dict__ for point in points[-payload.limit :]]
            results.append(
                {
                    "symbol": symbol,
                    "bar_count": len(symbol_bars),
                    "first_timestamp": symbol_bars[0].timestamp.isoformat(),
                    "last_timestamp": symbol_bars[-1].timestamp.isoformat(),
                    "latest": latest,
                    "rows": rows,
                }
            )

        return {
            "bars_file": payload.bars_file,
            "symbol": payload.symbol,
            "parameters": {
                "fast_window": payload.fast_window,
                "slow_window": payload.slow_window,
                "rsi_window": payload.rsi_window,
                "bollinger_window": payload.bollinger_window,
                "atr_window": payload.atr_window,
                "macd_signal_window": payload.macd_signal_window,
            },
            "results": results,
        }

    @app.post("/api/data-sources/fetch")
    async def fetch_market_data(payload: FetchMarketDataRequest, _: dict = Depends(require(RUN_BACKTESTS))) -> dict:
        output_file = payload.output_file or default_output_path(payload.provider, payload.symbol, payload.interval)
        output_path = _safe_data_output_path(output_file)
        try:
            bars = await fetch_ohlcv(
                MarketDataRequest(
                    provider=payload.provider,
                    symbol=payload.symbol,
                    interval=payload.interval,
                    start=payload.start,
                    end=payload.end,
                    limit=payload.limit,
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"market_data_request_failed:{exc}") from exc
        if not bars:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no_market_data_returned")

        write_bars_csv(output_path, bars)
        return {
            "provider": payload.provider,
            "symbol": payload.symbol,
            "interval": payload.interval,
            "output_file": str(output_path.relative_to(PROJECT_ROOT)),
            "bar_count": len(bars),
            "first_bar": _bar_payload(bars[0]),
            "last_bar": _bar_payload(bars[-1]),
        }

    @app.get("/api/watchlists")
    def watchlists(
        all_users: bool = Query(False, alias="all"),
        user: dict = Depends(require_any(VIEW_STRATEGIES, RUN_BACKTESTS, MANAGE_OWN_STRATEGIES, MANAGE_ALL_STRATEGIES)),
    ) -> list[dict]:
        include_all = all_users and has_user_permission(user, MANAGE_ALL_STRATEGIES)
        return db.list_watchlists(user["id"], include_all=include_all)

    @app.post("/api/watchlists")
    def create_watchlist(payload: CreateWatchlistRequest, user: dict = Depends(require(MANAGE_OWN_STRATEGIES))) -> dict:
        try:
            return db.create_watchlist(
                user_id=user["id"],
                name=payload.name,
                symbols=payload.symbols,
                notes=payload.notes,
                is_default=payload.is_default,
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    @app.patch("/api/watchlists/{watchlist_id}")
    def update_watchlist(
        watchlist_id: int,
        payload: UpdateWatchlistRequest,
        user: dict = Depends(require(MANAGE_OWN_STRATEGIES)),
    ) -> dict:
        try:
            updated = db.update_watchlist(
                watchlist_id,
                user_id=user["id"],
                is_admin=has_user_permission(user, MANAGE_ALL_STRATEGIES),
                name=payload.name,
                symbols=payload.symbols,
                notes=payload.notes,
                is_default=payload.is_default,
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        if not updated:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="watchlist_not_found")
        return updated

    @app.delete("/api/watchlists/{watchlist_id}")
    def delete_watchlist(watchlist_id: int, user: dict = Depends(require(MANAGE_OWN_STRATEGIES))) -> dict:
        deleted = db.delete_watchlist(
            watchlist_id,
            user_id=user["id"],
            is_admin=has_user_permission(user, MANAGE_ALL_STRATEGIES),
        )
        if not deleted:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="watchlist_not_found")
        return {"ok": True}

    @app.get("/api/users")
    def users(_: dict = Depends(require(MANAGE_USERS))) -> list[dict]:
        return db.list_users()

    @app.post("/api/users")
    def create_user(payload: CreateUserRequest, request: Request, actor: dict = Depends(require(MANAGE_USERS))) -> dict:
        try:
            created = db.create_user(
                payload.email,
                payload.password,
                payload.role,
                payload.status,
                permission_grants=payload.permission_grants,
                permission_revokes=payload.permission_revokes,
            )
            db.create_audit_log(
                user_id=actor["id"],
                action="user.create",
                entity_type="user",
                entity_id=created["id"],
                status="ok",
                detail={"email": created["email"], "role": created["role"], "status": created["status"]},
                **_audit_context(request),
            )
            return created
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="user_create_failed") from exc

    @app.patch("/api/users/{user_id}")
    def update_user(
        user_id: int,
        payload: UpdateUserRequest,
        request: Request,
        user: dict = Depends(require(MANAGE_USERS)),
    ) -> dict:
        try:
            _assert_self_keeps_user_management(db, user, user_id, payload)
            updated = db.update_user(
                user_id,
                role=payload.role,
                status=payload.status,
                permission_grants=payload.permission_grants,
                permission_revokes=payload.permission_revokes,
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        if not updated:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user_not_found")
        db.create_audit_log(
            user_id=user["id"],
            action="user.update",
            entity_type="user",
            entity_id=user_id,
            status="ok",
            detail={
                "role": payload.role,
                "status": payload.status,
                "permission_grants": payload.permission_grants,
                "permission_revokes": payload.permission_revokes,
            },
            **_audit_context(request),
        )
        return updated

    @app.get("/api/api-keys")
    def api_keys(
        all_users: bool = Query(False, alias="all"),
        user: dict = Depends(require_any(MANAGE_OWN_API_KEYS, MANAGE_ALL_API_KEYS)),
    ) -> list[dict]:
        include_all = all_users and has_user_permission(user, MANAGE_ALL_API_KEYS)
        return db.list_api_keys(user["id"], include_all=include_all)

    @app.post("/api/api-keys")
    def create_api_key(
        payload: CreateApiKeyRequest,
        request: Request,
        user: dict = Depends(require_any(MANAGE_OWN_API_KEYS, MANAGE_ALL_API_KEYS)),
    ) -> dict:
        if not payload.api_key or not payload.api_secret:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="api_key_and_secret_required")
        key = db.create_api_key(
            user_id=user["id"],
            exchange=payload.exchange,
            label=payload.label,
            api_key=payload.api_key,
            api_secret=payload.api_secret,
            passphrase=payload.passphrase,
            scopes=payload.scopes,
            can_trade=payload.can_trade,
        )
        db.create_audit_log(
            user_id=user["id"],
            action="api_key.create",
            entity_type="api_key",
            entity_id=key["id"],
            status="ok",
            detail={"exchange": key["exchange"], "label": key["label"], "can_trade": key["can_trade"]},
            **_audit_context(request),
        )
        return key

    @app.delete("/api/api-keys/{api_key_id}")
    def delete_api_key(
        api_key_id: int,
        request: Request,
        user: dict = Depends(require_any(MANAGE_OWN_API_KEYS, MANAGE_ALL_API_KEYS)),
    ) -> dict:
        deleted = db.delete_api_key(api_key_id, user_id=user["id"], is_admin=has_user_permission(user, MANAGE_ALL_API_KEYS))
        if not deleted:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="api_key_not_found")
        db.create_audit_log(
            user_id=user["id"],
            action="api_key.delete",
            entity_type="api_key",
            entity_id=api_key_id,
            status="ok",
            **_audit_context(request),
        )
        return {"ok": True}

    @app.get("/api/strategies/catalog")
    def strategy_catalog(_: dict = Depends(require_any(VIEW_STRATEGIES, RUN_BACKTESTS, MANAGE_OWN_STRATEGIES, MANAGE_ALL_STRATEGIES))) -> list[dict]:
        return list_strategy_definitions()

    @app.get("/api/strategy-configs")
    def strategy_configs(
        all_users: bool = Query(False, alias="all"),
        user: dict = Depends(require_any(VIEW_STRATEGIES, MANAGE_OWN_STRATEGIES, MANAGE_ALL_STRATEGIES)),
    ) -> list[dict]:
        include_all = all_users and has_user_permission(user, MANAGE_ALL_STRATEGIES)
        return db.list_strategy_configs(user["id"], include_all=include_all)

    @app.get("/api/strategy-configs/{config_id}/versions")
    def strategy_config_versions(
        config_id: int,
        user: dict = Depends(require_any(VIEW_STRATEGIES, MANAGE_OWN_STRATEGIES, MANAGE_ALL_STRATEGIES)),
    ) -> list[dict]:
        versions = db.list_strategy_config_versions(
            config_id,
            user_id=user["id"],
            is_admin=has_user_permission(user, MANAGE_ALL_STRATEGIES),
        )
        if not versions:
            config = db.get_strategy_config(
                config_id,
                user_id=user["id"],
                is_admin=has_user_permission(user, MANAGE_ALL_STRATEGIES),
            )
            if not config:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="strategy_config_not_found")
        return versions

    @app.post("/api/strategy-configs")
    def create_strategy_config(
        payload: CreateStrategyConfigRequest,
        user: dict = Depends(require(MANAGE_OWN_STRATEGIES)),
    ) -> dict:
        try:
            parameters = validate_parameters(payload.strategy_type, payload.parameters)
            return db.create_strategy_config(
                user_id=user["id"],
                name=payload.name,
                strategy_type=payload.strategy_type,
                market_type=payload.market_type,
                parameters=parameters,
                is_default=payload.is_default,
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    @app.patch("/api/strategy-configs/{config_id}")
    def update_strategy_config(
        config_id: int,
        payload: UpdateStrategyConfigRequest,
        user: dict = Depends(require(MANAGE_OWN_STRATEGIES)),
    ) -> dict:
        try:
            strategy_type = payload.strategy_type
            parameters = payload.parameters
            if parameters is not None:
                current = db.get_strategy_config(
                    config_id,
                    user_id=user["id"],
                    is_admin=has_user_permission(user, MANAGE_ALL_STRATEGIES),
                )
                if not current:
                    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="strategy_config_not_found")
                strategy_type = strategy_type or current["strategy_type"]
                parameters = validate_parameters(strategy_type, parameters)

            updated = db.update_strategy_config(
                config_id,
                user_id=user["id"],
                is_admin=has_user_permission(user, MANAGE_ALL_STRATEGIES),
                name=payload.name,
                strategy_type=strategy_type,
                market_type=payload.market_type,
                parameters=parameters,
                is_default=payload.is_default,
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

        if not updated:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="strategy_config_not_found")
        return updated

    @app.delete("/api/strategy-configs/{config_id}")
    def delete_strategy_config(config_id: int, user: dict = Depends(require(MANAGE_OWN_STRATEGIES))) -> dict:
        deleted = db.delete_strategy_config(
            config_id,
            user_id=user["id"],
            is_admin=has_user_permission(user, MANAGE_ALL_STRATEGIES),
        )
        if not deleted:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="strategy_config_not_found")
        return {"ok": True}

    @app.get("/api/monitoring/summary")
    def monitoring_summary(
        all_users: bool = Query(False, alias="all"),
        user: dict = Depends(require(VIEW_MONITORING)),
    ) -> dict:
        include_all = all_users and has_user_permission(user, VIEW_ALL_TRADES)
        return db.monitoring_summary(user["id"], include_all=include_all)

    @app.get("/api/trade-runs")
    def trade_runs(
        all_users: bool = Query(False, alias="all"),
        limit: int = Query(50, ge=1, le=500),
        run_mode: Optional[str] = Query(None),
        user: dict = Depends(require_any(VIEW_OWN_TRADES, VIEW_ALL_TRADES)),
    ) -> list[dict]:
        include_all = all_users and has_user_permission(user, VIEW_ALL_TRADES)
        return db.list_trade_runs(user["id"], include_all=include_all, limit=limit, run_mode=run_mode)

    @app.get("/api/trade-runs/{run_id}")
    def trade_run_detail(run_id: int, user: dict = Depends(require_any(VIEW_OWN_TRADES, VIEW_ALL_TRADES))) -> dict:
        run = db.get_trade_run(run_id, user_id=user["id"], is_admin=has_user_permission(user, VIEW_ALL_TRADES))
        if not run:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="trade_run_not_found")
        return run

    @app.get("/api/trade-runs/{run_id}/records")
    def trade_run_records(
        run_id: int,
        limit: int = Query(500, ge=1, le=500),
        user: dict = Depends(require_any(VIEW_OWN_TRADES, VIEW_ALL_TRADES)),
    ) -> list[dict]:
        run = db.get_trade_run(run_id, user_id=user["id"], is_admin=has_user_permission(user, VIEW_ALL_TRADES))
        if not run:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="trade_run_not_found")
        return db.list_trade_records(user["id"], include_all=True, limit=limit, run_id=run_id)

    @app.get("/api/trade-runs/{run_id}/export")
    def export_trade_run(
        run_id: int,
        format: str = Query("json", pattern="^(json|csv)$"),
        user: dict = Depends(require_any(VIEW_OWN_TRADES, VIEW_ALL_TRADES)),
    ) -> StreamingResponse:
        run = db.get_trade_run(run_id, user_id=user["id"], is_admin=has_user_permission(user, VIEW_ALL_TRADES))
        if not run:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="trade_run_not_found")
        records = db.list_trade_records(user["id"], include_all=True, limit=500, run_id=run_id)
        if format == "csv":
            content = _trade_records_csv(records)
            return StreamingResponse(
                iter([content]),
                media_type="text/csv",
                headers={"Content-Disposition": f'attachment; filename="trade-run-{run_id}-records.csv"'},
            )

        content = json.dumps({"run": run, "records": records}, ensure_ascii=False, indent=2)
        return StreamingResponse(
            iter([content]),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="trade-run-{run_id}.json"'},
        )

    @app.get("/api/trade-records")
    def trade_records(
        all_users: bool = Query(False, alias="all"),
        limit: int = Query(100, ge=1, le=500),
        user: dict = Depends(require_any(VIEW_OWN_TRADES, VIEW_ALL_TRADES)),
    ) -> list[dict]:
        include_all = all_users and has_user_permission(user, VIEW_ALL_TRADES)
        return db.list_trade_records(user["id"], include_all=include_all, limit=limit)

    @app.get("/api/orders")
    def trade_orders(
        all_users: bool = Query(False, alias="all"),
        limit: int = Query(100, ge=1, le=500),
        run_mode: Optional[str] = Query(None),
        user: dict = Depends(require_any(VIEW_OWN_TRADES, VIEW_ALL_TRADES, EXECUTE_PAPER_TRADES)),
    ) -> list[dict]:
        include_all = all_users and has_user_permission(user, VIEW_ALL_TRADES)
        return db.list_trade_orders(user["id"], include_all=include_all, limit=limit, run_mode=run_mode)

    @app.get("/api/audit-logs")
    def audit_logs(
        all_users: bool = Query(False, alias="all"),
        limit: int = Query(100, ge=1, le=500),
        user: dict = Depends(require_any(MANAGE_USERS, MANAGE_SYSTEM)),
    ) -> list[dict]:
        include_all = all_users and (has_user_permission(user, MANAGE_USERS) or has_user_permission(user, MANAGE_SYSTEM))
        return db.list_audit_logs(user["id"], include_all=include_all, limit=limit)

    @app.get("/api/execution-events")
    def execution_events(
        all_users: bool = Query(False, alias="all"),
        limit: int = Query(100, ge=1, le=500),
        user: dict = Depends(require_any(VIEW_OWN_TRADES, VIEW_ALL_TRADES, EXECUTE_PAPER_TRADES)),
    ) -> list[dict]:
        include_all = all_users and has_user_permission(user, VIEW_ALL_TRADES)
        return db.list_execution_events(user["id"], include_all=include_all, limit=limit)

    @app.get("/api/realtime/snapshot")
    def realtime_snapshot(
        symbol: str = Query("BTCUSDT"),
        user: dict = Depends(require_any(VIEW_MONITORING, RUN_BACKTESTS, EXECUTE_PAPER_TRADES)),
    ) -> dict:
        snapshot = _generate_market_snapshot(symbol)
        return db.create_realtime_market_snapshot(**snapshot)

    @app.get("/api/realtime/snapshots")
    def realtime_snapshots(
        symbol: Optional[str] = Query(None),
        limit: int = Query(50, ge=1, le=500),
        _: dict = Depends(require_any(VIEW_MONITORING, RUN_BACKTESTS, EXECUTE_PAPER_TRADES)),
    ) -> list[dict]:
        return db.list_realtime_market_snapshots(symbol=symbol, limit=limit)

    @app.get("/api/realtime/signals")
    def realtime_signals(
        all_users: bool = Query(False, alias="all"),
        limit: int = Query(100, ge=1, le=500),
        user: dict = Depends(require_any(VIEW_MONITORING, RUN_BACKTESTS, EXECUTE_PAPER_TRADES)),
    ) -> list[dict]:
        include_all = all_users and has_user_permission(user, VIEW_ALL_TRADES)
        return db.list_signal_logs(user["id"], include_all=include_all, limit=limit)

    @app.post("/api/realtime/signals/evaluate")
    def evaluate_realtime_signal(
        payload: RealtimeSignalRequest,
        request: Request,
        user: dict = Depends(require_any(RUN_BACKTESTS, EXECUTE_PAPER_TRADES)),
    ) -> dict:
        strategy_config = None
        parameters = dict(payload.parameters)
        strategy_type = payload.strategy_type
        if payload.strategy_config_id is not None:
            strategy_config = db.get_strategy_config(
                payload.strategy_config_id,
                user_id=user["id"],
                is_admin=has_user_permission(user, MANAGE_ALL_STRATEGIES),
            )
            if not strategy_config:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="strategy_config_not_found")
            strategy_type = strategy_config["strategy_type"]
            parameters = strategy_config["parameters"]

        snapshot = db.create_realtime_market_snapshot(**_generate_market_snapshot(payload.symbol))
        signal = _evaluate_simple_signal(snapshot, parameters)
        signal_log = db.create_signal_log(
            user_id=user["id"],
            strategy_config_id=strategy_config["id"] if strategy_config else None,
            strategy_type=strategy_type,
            symbol=snapshot["symbol"],
            signal=signal["signal"],
            score=signal["score"],
            parameters=parameters,
            market_snapshot=snapshot,
            reason=signal["reason"],
        )
        db.create_audit_log(
            user_id=user["id"],
            action="signal.evaluate",
            entity_type="signal_log",
            entity_id=signal_log["id"],
            status=signal_log["signal"],
            detail={"symbol": snapshot["symbol"], "score": signal_log["score"], "strategy_type": strategy_type},
            **_audit_context(request),
        )
        return signal_log

    @app.websocket("/ws/market-feed")
    async def market_feed_socket(websocket: WebSocket) -> None:
        await websocket.accept()
        symbol = websocket.query_params.get("symbol", "BTCUSDT")
        try:
            for _ in range(30):
                snapshot = db.create_realtime_market_snapshot(**_generate_market_snapshot(symbol))
                await websocket.send_json(snapshot)
                await asyncio.sleep(1)
        except WebSocketDisconnect:
            return

    @app.get("/api/exchange-sync/runs")
    def exchange_sync_runs(
        all_users: bool = Query(False, alias="all"),
        limit: int = Query(50, ge=1, le=500),
        user: dict = Depends(require_any(EXECUTE_PAPER_TRADES, EXECUTE_LIVE_TRADES, VIEW_ALL_TRADES)),
    ) -> list[dict]:
        include_all = all_users and has_user_permission(user, VIEW_ALL_TRADES)
        return db.list_exchange_sync_runs(user["id"], include_all=include_all, limit=limit)

    @app.post("/api/exchange-sync/run")
    def run_exchange_sync(
        payload: ExchangeSyncRequest,
        request: Request,
        user: dict = Depends(require_any(EXECUTE_PAPER_TRADES, EXECUTE_LIVE_TRADES)),
    ) -> dict:
        run_mode = payload.run_mode.strip().lower()
        exchange = payload.exchange.strip().lower()
        if run_mode == "live" and not has_user_permission(user, EXECUTE_LIVE_TRADES):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="live_trading_permission_required")
        if run_mode == "live":
            raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="exchange_private_sync_not_configured")

        local_snapshot = _local_trading_snapshot(db, user)
        exchange_snapshot = payload.exchange_snapshot or local_snapshot
        differences = _snapshot_differences(local_snapshot, exchange_snapshot)
        sync_run = db.create_exchange_sync_run(
            user_id=user["id"],
            exchange=exchange,
            run_mode=run_mode,
            status="matched" if not differences else "mismatch",
            local_snapshot=local_snapshot,
            exchange_snapshot=exchange_snapshot,
            differences=differences,
        )
        db.create_audit_log(
            user_id=user["id"],
            action="exchange_sync.run",
            entity_type="exchange_sync_run",
            entity_id=sync_run["id"],
            status=sync_run["status"],
            detail={"exchange": exchange, "run_mode": run_mode, "difference_count": len(differences)},
            **_audit_context(request),
        )
        return sync_run

    @app.get("/api/symbol-rules/sources")
    def symbol_rule_sources(_: dict = Depends(require_any(RUN_BACKTESTS, EXECUTE_PAPER_TRADES, MANAGE_SYSTEM))) -> list[dict]:
        return list_rule_sources()

    @app.post("/api/symbol-rules/sync")
    async def sync_symbol_rules(
        payload: SymbolRuleSyncRequest,
        request: Request,
        user: dict = Depends(require_any(RUN_BACKTESTS, EXECUTE_PAPER_TRADES, MANAGE_SYSTEM)),
    ) -> dict:
        try:
            fetched_rules = await fetch_exchange_symbol_rules(
                provider=payload.provider,
                symbol=payload.symbol,
                limit=payload.limit,
            )
        except (ValueError, httpx.HTTPError) as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

        saved_rules = [
            db.upsert_symbol_rule(
                exchange=rule.exchange,
                market_type=rule.market_type,
                symbol=rule.symbol,
                min_quantity=rule.min_quantity,
                quantity_step=rule.quantity_step,
                price_tick=rule.price_tick,
                min_notional=rule.min_notional,
                max_notional=rule.max_notional,
                max_leverage=rule.max_leverage,
                is_active=True,
                source=rule.source,
            )
            for rule in fetched_rules
        ]
        db.create_audit_log(
            user_id=user["id"],
            action="symbol_rule.sync",
            entity_type="symbol_rule",
            status="ok",
            detail={
                "provider": payload.provider,
                "symbol": payload.symbol,
                "requested_limit": payload.limit,
                "saved_count": len(saved_rules),
            },
            **_audit_context(request),
        )
        return {
            "provider": payload.provider,
            "saved_count": len(saved_rules),
            "rules": saved_rules[:50],
        }

    @app.get("/api/symbol-rules")
    def symbol_rules(
        exchange: Optional[str] = None,
        market_type: Optional[str] = None,
        symbol: Optional[str] = None,
        limit: int = Query(100, ge=1, le=500),
        _: dict = Depends(require_any(RUN_BACKTESTS, EXECUTE_PAPER_TRADES, MANAGE_SYSTEM)),
    ) -> list[dict]:
        return db.list_symbol_rules(exchange=exchange, market_type=market_type, symbol=symbol, limit=limit)

    @app.post("/api/symbol-rules")
    def upsert_symbol_rule(
        payload: SymbolRuleRequest,
        request: Request,
        user: dict = Depends(require_any(RUN_BACKTESTS, EXECUTE_PAPER_TRADES, MANAGE_SYSTEM)),
    ) -> dict:
        try:
            rule = db.upsert_symbol_rule(
                exchange=payload.exchange,
                market_type=payload.market_type,
                symbol=payload.symbol,
                min_quantity=payload.min_quantity,
                quantity_step=payload.quantity_step,
                price_tick=payload.price_tick,
                min_notional=payload.min_notional,
                max_notional=payload.max_notional,
                max_leverage=payload.max_leverage,
                is_active=payload.is_active,
                source=payload.source,
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        db.create_audit_log(
            user_id=user["id"],
            action="symbol_rule.upsert",
            entity_type="symbol_rule",
            entity_id=rule["id"],
            status="ok",
            detail={
                "exchange": rule["exchange"],
                "market_type": rule["market_type"],
                "symbol": rule["symbol"],
                "is_active": rule["is_active"],
                "source": rule["source"],
            },
            **_audit_context(request),
        )
        return rule

    @app.post("/api/strategy-tools/pmm/quote-plan")
    def pmm_quote_plan(
        payload: PmmQuotePlanRequest,
        request: Request,
        user: dict = Depends(require_any(RUN_BACKTESTS, EXECUTE_PAPER_TRADES, MANAGE_SYSTEM)),
    ) -> dict:
        symbol_rule = db.get_symbol_rule(
            payload.symbol,
            exchange=payload.exchange,
            market_type=payload.market_type,
        )
        quote_payload = _model_dump(payload)
        quote_payload.pop("exchange", None)
        quote_payload.pop("market_type", None)
        try:
            plan = build_pmm_quote_plan(
                **quote_payload,
                price_tick=float(symbol_rule["price_tick"]) if symbol_rule else 0.0,
                quantity_step=float(symbol_rule["quantity_step"]) if symbol_rule else 0.0,
                min_notional=float(symbol_rule["min_notional"]) if symbol_rule else 0.0,
                max_notional=float(symbol_rule["max_notional"]) if symbol_rule else 0.0,
                allow_short=payload.market_type.startswith("perpetual"),
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        plan["symbol_rule"] = symbol_rule
        db.create_audit_log(
            user_id=user["id"],
            action="strategy_tool.pmm_quote_plan",
            entity_type="strategy_tool",
            status="ok",
            detail={"symbol": plan["symbol"], "ready_orders": sum(1 for order in plan["orders"] if order["status"] == "ready")},
            **_audit_context(request),
        )
        return plan

    @app.post("/api/strategy-tools/xemm/opportunity")
    def xemm_opportunity(
        payload: XemmOpportunityRequest,
        request: Request,
        user: dict = Depends(require_any(RUN_BACKTESTS, EXECUTE_PAPER_TRADES, MANAGE_SYSTEM)),
    ) -> dict:
        try:
            result = calculate_xemm_opportunity(**_model_dump(payload))
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        db.create_audit_log(
            user_id=user["id"],
            action="strategy_tool.xemm_opportunity",
            entity_type="strategy_tool",
            status="approved" if result["approved"] else "rejected",
            detail={
                "symbol": result["symbol"],
                "maker_exchange": result["maker_exchange"],
                "taker_exchange": result["taker_exchange"],
                "approved": result["approved"],
            },
            **_audit_context(request),
        )
        return result

    @app.post("/api/strategy-tools/funding-arbitrage")
    def funding_arbitrage(
        payload: FundingArbitrageRequest,
        request: Request,
        user: dict = Depends(require_any(RUN_BACKTESTS, EXECUTE_PAPER_TRADES, MANAGE_SYSTEM)),
    ) -> dict:
        try:
            result = calculate_spot_perp_arbitrage(**_model_dump(payload))
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        db.create_audit_log(
            user_id=user["id"],
            action="strategy_tool.funding_arbitrage",
            entity_type="strategy_tool",
            status="approved" if result["approved"] else "rejected",
            detail={
                "symbol": result["symbol"],
                "spot_exchange": result["spot_exchange"],
                "perp_exchange": result["perp_exchange"],
                "net_edge_bps": result["net_edge_bps"],
                "approved": result["approved"],
            },
            **_audit_context(request),
        )
        return result

    @app.get("/api/automation/status")
    def automation_status(
        all_users: bool = Query(False, alias="all"),
        user: dict = Depends(require_any(EXECUTE_PAPER_TRADES, EXECUTE_LIVE_TRADES, MANAGE_SYSTEM, VIEW_ALL_TRADES)),
    ) -> dict:
        include_all = all_users and has_user_permission(user, VIEW_ALL_TRADES)
        return {
            "emergency_stop": db.get_system_flag("emergency_stop", "0"),
            "controls": db.list_automation_controls(user["id"], include_all=include_all),
            "risk_settings": db.get_or_create_risk_settings(user["id"]),
        }

    @app.post("/api/automation/control")
    def automation_control(
        payload: AutomationControlRequest,
        request: Request,
        user: dict = Depends(require(EXECUTE_PAPER_TRADES)),
    ) -> dict:
        action = payload.action.strip().lower()
        if action not in {"start", "pause", "stop"}:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unsupported_automation_action")
        if payload.strategy_config_id is not None:
            config = db.get_strategy_config(
                payload.strategy_config_id,
                user_id=user["id"],
                is_admin=has_user_permission(user, MANAGE_ALL_STRATEGIES),
            )
            if not config:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="strategy_config_not_found")
        status_value = {"start": "running", "pause": "paused", "stop": "stopped"}[action]
        control = db.upsert_automation_control(
            user_id=user["id"],
            strategy_config_id=payload.strategy_config_id,
            run_mode=payload.run_mode,
            status=status_value,
            trading_enabled=action == "start",
            last_action=action,
        )
        db.create_audit_log(
            user_id=user["id"],
            action=f"automation.{action}",
            entity_type="automation_control",
            entity_id=control["id"],
            status=status_value,
            detail={"strategy_config_id": payload.strategy_config_id, "run_mode": payload.run_mode},
            **_audit_context(request),
        )
        _emit_notification(
            db,
            user["id"],
            "automation_state",
            "自动交易状态变化",
            f"{payload.run_mode} strategy {payload.strategy_config_id or 'all'} {status_value}",
            {"control": control},
        )
        return control

    @app.post("/api/automation/run-once")
    def automation_run_once(
        payload: AutomationRunOnceRequest,
        request: Request,
        user: dict = Depends(require(EXECUTE_PAPER_TRADES)),
    ) -> dict:
        try:
            result = _run_automation_once(db, user, payload)
            db.create_audit_log(
                user_id=user["id"],
                action="automation.run_once",
                entity_type="signal_log",
                entity_id=result["signal_log"]["id"] if result.get("signal_log") else None,
                status=result["status"],
                detail={
                    "symbol": payload.symbol.strip().upper(),
                    "run_mode": payload.run_mode,
                    "strategy_config_id": payload.strategy_config_id,
                    "signal": result.get("signal", {}).get("signal"),
                    "order_id": ((result.get("order_result") or {}).get("order") or {}).get("id"),
                },
                **_audit_context(request),
            )
            return result
        except ValueError as exc:
            db.create_audit_log(
                user_id=user["id"],
                action="automation.run_once",
                entity_type="automation_control",
                status="rejected",
                detail={"reason": str(exc), "symbol": payload.symbol.strip().upper(), "run_mode": payload.run_mode},
                **_audit_context(request),
            )
            _emit_notification(
                db,
                user["id"],
                "strategy_exception",
                "自动策略运行失败",
                str(exc),
                {"symbol": payload.symbol, "run_mode": payload.run_mode, "strategy_config_id": payload.strategy_config_id},
            )
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    @app.post("/api/automation/emergency-stop")
    def automation_emergency_stop(request: Request, user: dict = Depends(require(MANAGE_SYSTEM))) -> dict:
        flag = db.set_system_flag("emergency_stop", "1", updated_by=user["id"])
        db.create_audit_log(
            user_id=user["id"],
            action="automation.emergency_stop",
            entity_type="system_flag",
            entity_id="emergency_stop",
            status="active",
            **_audit_context(request),
        )
        _emit_notification(db, user["id"], "risk_trigger", "紧急停止已触发", "全局自动交易紧急停止已开启。", {"flag": flag})
        return flag

    @app.post("/api/automation/clear-emergency-stop")
    def automation_clear_emergency_stop(request: Request, user: dict = Depends(require(MANAGE_SYSTEM))) -> dict:
        flag = db.set_system_flag("emergency_stop", "0", updated_by=user["id"])
        db.create_audit_log(
            user_id=user["id"],
            action="automation.clear_emergency_stop",
            entity_type="system_flag",
            entity_id="emergency_stop",
            status="inactive",
            **_audit_context(request),
        )
        return flag

    @app.post("/api/automation/users/{target_user_id}/trading-switch")
    def user_trading_switch(
        target_user_id: int,
        payload: UserTradingSwitchRequest,
        request: Request,
        user: dict = Depends(require(MANAGE_USERS)),
    ) -> dict:
        target = db.get_user(target_user_id)
        if not target:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user_not_found")
        updated = db.update_risk_settings(
            user_id=target_user_id,
            paper_trading_enabled=payload.paper_trading_enabled,
            live_trading_enabled=payload.live_trading_enabled,
        )
        db.create_audit_log(
            user_id=user["id"],
            action="automation.user_trading_switch",
            entity_type="risk_settings",
            entity_id=target_user_id,
            status="ok",
            detail=_model_dump(payload),
            **_audit_context(request),
        )
        return updated

    @app.post("/api/automation/close-all")
    def close_all_positions(
        payload: CloseAllPositionsRequest,
        request: Request,
        user: dict = Depends(require(EXECUTE_PAPER_TRADES)),
    ) -> dict:
        run_mode = payload.run_mode.strip().lower()
        if run_mode == "live":
            raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="live_close_all_not_configured")
        result = _close_all_paper_positions(db, user, payload)
        db.create_audit_log(
            user_id=user["id"],
            action="automation.close_all",
            entity_type="trade_run",
            entity_id=result["run"]["id"] if result.get("run") else None,
            status="ok",
            detail={"closed_count": result["closed_count"], "run_mode": run_mode},
            **_audit_context(request),
        )
        _emit_notification(
            db,
            user["id"],
            "trade_filled",
            "Paper 持仓已全部平仓",
            f"已平掉 {result['closed_count']} 个 paper 持仓。",
            result,
        )
        return result

    @app.get("/api/notification-channels")
    def notification_channels(
        all_users: bool = Query(False, alias="all"),
        user: dict = Depends(require_any(EXECUTE_PAPER_TRADES, MANAGE_USERS)),
    ) -> list[dict]:
        include_all = all_users and has_user_permission(user, MANAGE_USERS)
        return db.list_notification_channels(user["id"], include_all=include_all)

    @app.post("/api/notification-channels")
    def create_notification_channel(
        payload: CreateNotificationChannelRequest,
        user: dict = Depends(require_any(EXECUTE_PAPER_TRADES, MANAGE_USERS)),
    ) -> dict:
        return db.create_notification_channel(
            user_id=user["id"],
            channel_type=payload.channel_type,
            label=payload.label,
            destination=payload.destination,
            is_active=payload.is_active,
        )

    @app.get("/api/notifications")
    def notifications(
        all_users: bool = Query(False, alias="all"),
        limit: int = Query(100, ge=1, le=500),
        user: dict = Depends(require_any(EXECUTE_PAPER_TRADES, MANAGE_USERS, VIEW_ALL_TRADES)),
    ) -> list[dict]:
        include_all = all_users and (has_user_permission(user, MANAGE_USERS) or has_user_permission(user, VIEW_ALL_TRADES))
        return db.list_notifications(user["id"], include_all=include_all, limit=limit)

    @app.post("/api/notifications/dispatch")
    def dispatch_notifications(
        payload: NotificationDispatchRequest,
        request: Request,
        user: dict = Depends(require_any(EXECUTE_PAPER_TRADES, MANAGE_SYSTEM)),
    ) -> dict:
        result = _dispatch_queued_notifications(db, user, limit=payload.limit, dry_run=payload.dry_run)
        db.create_audit_log(
            user_id=user["id"],
            action="notifications.dispatch",
            entity_type="notifications",
            status="ok",
            detail={
                "dry_run": payload.dry_run,
                "dispatched_count": result["dispatched_count"],
                "remaining_queued": result["remaining_queued"],
            },
            **_audit_context(request),
        )
        return result

    @app.post("/api/trading/preflight")
    def trading_preflight(
        payload: TradePreflightRequest,
        request: Request,
        user: dict = Depends(require_any(EXECUTE_PAPER_TRADES, EXECUTE_LIVE_TRADES)),
    ) -> dict:
        run_mode = payload.run_mode.strip().lower()
        if run_mode == "live" and not has_user_permission(user, EXECUTE_LIVE_TRADES):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="live_trading_permission_required")
        try:
            plan = _manual_order_plan(db, user, payload)
            result = _manual_order_preflight(db, user, payload, plan, run_mode=run_mode)
            db.create_audit_log(
                user_id=user["id"],
                action="trading.preflight",
                entity_type="trade_order",
                status="approved" if result["approved"] else "rejected",
                detail={
                    "symbol": result["symbol"],
                    "side": result["side"],
                    "quantity": result["quantity"],
                    "reason": result["reason"],
                    "run_mode": result["run_mode"],
                },
                **_audit_context(request),
            )
            return result
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    @app.post("/api/orders/{order_id}/cancel")
    def cancel_order(order_id: int, request: Request, user: dict = Depends(require(EXECUTE_PAPER_TRADES))) -> dict:
        order = db.get_trade_order(order_id, user_id=user["id"], is_admin=has_user_permission(user, VIEW_ALL_TRADES))
        if not order:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="order_not_found")
        if order["status"] not in {OrderStatus.CREATED.value, OrderStatus.SUBMITTED.value, OrderStatus.PARTIALLY_FILLED.value}:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="order_not_cancelable")
        updated = db.update_trade_order(
            order_id,
            status=OrderStatus.CANCELED.value,
            raw_response={"cancel_requested": True, "previous_status": order["status"]},
            finished=True,
        )
        db.create_audit_log(
            user_id=user["id"],
            action="order.cancel",
            entity_type="trade_order",
            entity_id=order_id,
            status="ok",
            detail={"previous_status": order["status"]},
            **_audit_context(request),
        )
        return updated

    @app.get("/api/risk-settings")
    def risk_settings(user: dict = Depends(require(EXECUTE_PAPER_TRADES))) -> dict:
        return db.get_or_create_risk_settings(user["id"])

    @app.patch("/api/risk-settings")
    def update_risk_settings(
        payload: UpdateRiskSettingsRequest,
        request: Request,
        user: dict = Depends(require(EXECUTE_PAPER_TRADES)),
    ) -> dict:
        updated = db.update_risk_settings(
            user_id=user["id"],
            paper_trading_enabled=payload.paper_trading_enabled,
            live_trading_enabled=payload.live_trading_enabled,
            max_order_notional=payload.max_order_notional,
            max_daily_loss=payload.max_daily_loss,
            max_symbol_weight=payload.max_symbol_weight,
            max_gross_exposure=payload.max_gross_exposure,
            max_open_positions=payload.max_open_positions,
            max_leverage=payload.max_leverage,
            max_consecutive_losses=payload.max_consecutive_losses,
            require_order_confirmation=payload.require_order_confirmation,
            allow_reverse_position=payload.allow_reverse_position,
            allow_duplicate_position=payload.allow_duplicate_position,
            auto_disable_on_daily_loss=payload.auto_disable_on_daily_loss,
            require_live_confirmation=payload.require_live_confirmation,
        )
        db.create_audit_log(
            user_id=user["id"],
            action="risk.update",
            entity_type="risk_settings",
            entity_id=user["id"],
            status="ok",
            detail=_model_dump(payload),
            **_audit_context(request),
        )
        return updated

    @app.get("/api/paper-trading/account")
    def paper_account(user: dict = Depends(require(EXECUTE_PAPER_TRADES))) -> dict:
        return db.get_or_create_paper_account(user["id"])

    @app.get("/api/paper-trading/positions")
    def paper_positions(
        all_users: bool = Query(False, alias="all"),
        user: dict = Depends(require(EXECUTE_PAPER_TRADES)),
    ) -> list[dict]:
        include_all = all_users and has_user_permission(user, VIEW_ALL_TRADES)
        return db.list_paper_positions(user["id"], include_all=include_all)

    @app.post("/api/paper-trading/order")
    def paper_order(
        payload: ManualPaperOrderRequest,
        request: Request,
        user: dict = Depends(require(EXECUTE_PAPER_TRADES)),
    ) -> dict:
        try:
            result = _execute_manual_paper_order(db, user, payload)
            db.create_audit_log(
                user_id=user["id"],
                action="trading.paper_order",
                entity_type="trade_order",
                entity_id=result["order"]["id"] if result.get("order") else None,
                status=result["order"]["status"] if result.get("order") else "ok",
                detail={
                    "symbol": payload.symbol.strip().upper(),
                    "action": payload.action.strip().lower(),
                    "quantity": payload.quantity,
                    "price": payload.price,
                },
                **_audit_context(request),
            )
            if result.get("order"):
                order_status = result["order"]["status"]
                _emit_notification(
                    db,
                    user["id"],
                    "trade_filled" if order_status == OrderStatus.FILLED.value else "order_state",
                    "Paper 订单状态更新",
                    f"{result['order']['symbol']} {result['order']['side']} {order_status}",
                    {"order": result["order"], "trade": result.get("trade")},
                )
            return result
        except ValueError as exc:
            db.create_audit_log(
                user_id=user["id"],
                action="trading.paper_order",
                entity_type="trade_order",
                status="rejected",
                detail={
                    "symbol": payload.symbol.strip().upper(),
                    "action": payload.action.strip().lower(),
                    "quantity": payload.quantity,
                    "price": payload.price,
                    "reason": str(exc),
                },
                **_audit_context(request),
            )
            _emit_notification(
                db,
                user["id"],
                "order_failed",
                "Paper 订单失败",
                str(exc),
                {"symbol": payload.symbol, "action": payload.action, "reason": str(exc)},
            )
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    @app.post("/api/paper-trading/auto-execute")
    def paper_auto_execute(
        payload: AutoPaperExecuteRequest,
        request: Request,
        user: dict = Depends(require(EXECUTE_PAPER_TRADES)),
    ) -> dict:
        try:
            result = _execute_strategy_paper(db, user, payload)
            db.create_audit_log(
                user_id=user["id"],
                action="trading.paper_auto_execute",
                entity_type="trade_run",
                entity_id=result["run"]["id"],
                status="ok",
                detail={"trade_count": result["trade_count"], "strategy": result["strategy"]["strategy_type"]},
                **_audit_context(request),
            )
            return result
        except ValueError as exc:
            db.create_audit_log(
                user_id=user["id"],
                action="trading.paper_auto_execute",
                entity_type="trade_run",
                status="rejected",
                detail={"reason": str(exc), "strategy_type": payload.strategy_type},
                **_audit_context(request),
            )
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    @app.post("/api/live-trading/order")
    def live_order(
        payload: ManualPaperOrderRequest,
        request: Request,
        user: dict = Depends(require(EXECUTE_LIVE_TRADES)),
    ) -> dict:
        if os.environ.get("CRYPTO_LAB_ENABLE_LIVE_TRADING", "0") != "1":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="live_trading_disabled")
        try:
            plan = _manual_order_plan(db, user, payload)
            preflight = _manual_order_preflight(db, user, payload, plan, run_mode="live")
            db.create_audit_log(
                user_id=user["id"],
                action="trading.live_preflight",
                entity_type="trade_order",
                status="approved" if preflight["approved"] else "rejected",
                detail={
                    "symbol": preflight["symbol"],
                    "side": preflight["side"],
                    "quantity": preflight["quantity"],
                    "reason": preflight["reason"],
                    "order_type": preflight["order_type"],
                },
                **_audit_context(request),
            )
            if not preflight["approved"]:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=preflight["reason"])
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        risk_settings = db.get_or_create_risk_settings(user["id"])
        if not risk_settings["live_trading_enabled"]:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="live_trading_paused_by_risk_settings")
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="exchange_connector_not_configured")

    @app.post("/api/backtests/run")
    def run_backtest(payload: BacktestRunRequest, user: dict = Depends(require(RUN_BACKTESTS))) -> dict:
        started_at = utc_now()
        bars_file = _safe_project_path(payload.bars_file)
        events_file = _safe_project_path(payload.events_file) if payload.events_file else None

        sources = [CsvBarDataSource(bars_file)]
        if events_file and events_file.exists():
            sources.append(CsvExternalEventSource(events_file))
        feed_events = list(MergedEventFeed(sources))

        strategy_type, parameters, strategy_config = _resolve_strategy_request(db, user, payload)
        strategy_config_id = strategy_config["id"] if strategy_config else None
        strategy_version = strategy_config["current_version"] if strategy_config else None
        market_type = strategy_config["market_type"] if strategy_config else payload.market_type

        result = _run_backtest_result(strategy_type, parameters, payload, feed_events, market_type)
        metrics_payload = _metrics_payload(result.metrics)
        if strategy_version is not None:
            metrics_payload["strategy_version"] = strategy_version
        validation_payload = _sample_validation_payload(strategy_type, parameters, payload, feed_events, market_type)
        if validation_payload:
            metrics_payload["validation"] = validation_payload

        run_record = db.create_trade_run(
            user_id=user["id"],
            strategy_config_id=strategy_config_id,
            strategy_type=strategy_type,
            run_mode="backtest",
            status="completed",
            market_type=market_type,
            bars_file=str(payload.bars_file),
            events_file=str(payload.events_file) if payload.events_file else None,
            parameters=parameters,
            metrics=metrics_payload,
            strategy_version=strategy_version,
            started_at=started_at,
            finished_at=utc_now(),
        )
        db.create_trade_records(
            run_id=run_record["id"],
            user_id=user["id"],
            strategy_config_id=strategy_config_id,
            run_mode="backtest",
            exchange="simulated",
            market_type=market_type,
            trades=[
                {
                    "symbol": fill.symbol,
                    "side": fill.side.value,
                    "quantity": fill.quantity,
                    "price": fill.price,
                    "fee": fill.fee,
                    "order_id": fill.order_id,
                    "reason": fill.reason,
                    "traded_at": fill.timestamp.isoformat(),
                }
                for fill in result.fills
            ],
        )
        return {
            "run": run_record,
            "strategy": {
                "strategy_type": strategy_type,
                "parameters": parameters,
                "config": strategy_config,
            },
            "metrics": metrics_payload,
            "fills": [
                {
                    "timestamp": fill.timestamp.isoformat(),
                    "symbol": fill.symbol,
                    "side": fill.side.value,
                    "quantity": fill.quantity,
                    "price": fill.price,
                    "fee": fill.fee,
                    "reason": fill.reason,
                }
                for fill in result.fills[-25:]
            ],
            "rejected_orders": [{"reason": item.reason} for item in result.rejected_orders[-25:]],
            "validation": validation_payload,
        }

    @app.post("/api/backtests/walk-forward")
    def walk_forward_backtest(payload: WalkForwardBacktestRequest, user: dict = Depends(require(RUN_BACKTESTS))) -> dict:
        started_at = utc_now()
        bars_file = _safe_project_path(payload.bars_file)
        events_file = _safe_project_path(payload.events_file) if payload.events_file else None

        sources = [CsvBarDataSource(bars_file)]
        if events_file and events_file.exists():
            sources.append(CsvExternalEventSource(events_file))
        feed_events = list(MergedEventFeed(sources))

        strategy_type, parameters, strategy_config = _resolve_strategy_request(db, user, payload)
        strategy_config_id = strategy_config["id"] if strategy_config else None
        strategy_version = strategy_config["current_version"] if strategy_config else None
        market_type = strategy_config["market_type"] if strategy_config else payload.market_type
        walk_forward = _walk_forward_payload(strategy_type, parameters, payload, feed_events, market_type)
        metrics_payload = {
            "strategy_version": strategy_version,
            "walk_forward": walk_forward,
            "window_count": walk_forward["window_count"],
            "average_test_return": walk_forward.get("average_test_return", 0.0),
            "worst_test_drawdown": walk_forward.get("worst_test_drawdown", 0.0),
            "consistency_ratio": walk_forward.get("consistency_ratio", 0.0),
            "total_test_trades": walk_forward.get("total_test_trades", 0),
        }
        run_record = db.create_trade_run(
            user_id=user["id"],
            strategy_config_id=strategy_config_id,
            strategy_version=strategy_version,
            strategy_type=strategy_type,
            run_mode="walk_forward",
            status=walk_forward["status"],
            market_type=market_type,
            bars_file=str(payload.bars_file),
            events_file=str(payload.events_file) if payload.events_file else None,
            parameters=parameters,
            metrics=metrics_payload,
            started_at=started_at,
            finished_at=utc_now(),
        )
        return {
            "run": run_record,
            "strategy": {
                "strategy_type": strategy_type,
                "parameters": parameters,
                "config": strategy_config,
                "market_type": market_type,
                "strategy_version": strategy_version,
            },
            "metrics": metrics_payload,
            "walk_forward": walk_forward,
        }

    @app.post("/api/backtests/grid-search")
    def grid_search_backtest(payload: GridSearchRequest, user: dict = Depends(require(RUN_BACKTESTS))) -> dict:
        bars_file = _safe_project_path(payload.bars_file)
        events_file = _safe_project_path(payload.events_file) if payload.events_file else None

        sources = [CsvBarDataSource(bars_file)]
        if events_file and events_file.exists():
            sources.append(CsvExternalEventSource(events_file))
        feed_events = list(MergedEventFeed(sources))

        strategy_type, parameters, strategy_config = _resolve_strategy_request(db, user, payload)
        market_type = strategy_config["market_type"] if strategy_config else payload.market_type
        keys, combinations = _parameter_combinations(payload.parameter_grid, payload.max_combinations)

        ranked_results = []
        for values in combinations:
            candidate_parameters = {**parameters, **dict(zip(keys, values))}
            try:
                validated_parameters = validate_parameters(strategy_type, candidate_parameters)
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"invalid_parameter_grid:{exc}",
                ) from exc

            result = _run_backtest_result(strategy_type, validated_parameters, payload, feed_events, market_type)
            ranked_results.append(
                {
                    "parameters": validated_parameters,
                    "metrics": _metrics_payload(result.metrics),
                    "fill_count": len(result.fills),
                    "rejected_order_count": len(result.rejected_orders),
                }
            )

        ranked_results.sort(key=lambda item: item["metrics"]["total_return"], reverse=True)
        for index, item in enumerate(ranked_results, start=1):
            item["rank"] = index

        return {
            "strategy": {
                "strategy_type": strategy_type,
                "base_parameters": parameters,
                "config": strategy_config,
                "market_type": market_type,
            },
            "combination_count": len(ranked_results),
            "results": ranked_results,
        }

    return app


def _execute_manual_paper_order(db: Database, user: dict, payload: ManualPaperOrderRequest) -> dict:
    plan = _manual_order_plan(db, user, payload)
    order = Order(
        timestamp=_now_datetime(),
        symbol=plan["symbol"],
        side=plan["side"],
        quantity=plan["quantity"],
        order_type=plan["order_type"],
        reason=f"{payload.reason}:{plan['action']}",
    )
    trade_order = db.create_trade_order(
        user_id=user["id"],
        client_order_id=order.id,
        symbol=plan["symbol"],
        side=plan["side"].value,
        quantity=plan["quantity"],
        requested_price=plan["price"],
        order_type=plan["order_type"].value,
        time_in_force=plan["time_in_force"].value,
        post_only=plan["post_only"],
        reduce_only=plan["reduce_only"],
        run_mode="paper",
        exchange="paper",
        market_type=payload.market_type,
        limit_price=plan["limit_price"],
        stop_price=plan["stop_price"],
        trailing_delta_pct=plan["trailing_delta_pct"],
        leverage=plan["leverage"],
        reason=order.reason,
    )

    preflight = _manual_order_preflight(db, user, payload, plan, run_mode="paper")
    if not preflight["approved"]:
        db.update_trade_order(
            trade_order["id"],
            status=OrderStatus.PREFLIGHT_REJECTED.value,
            error_message=preflight["reason"],
            raw_response={"preflight": preflight},
            finished=True,
        )
        db.create_execution_event(
            user_id=user["id"],
            order_id=trade_order["id"],
            event_type="preflight",
            status=OrderStatus.PREFLIGHT_REJECTED.value,
            price_before=plan["price"],
            risk_checks=preflight["checks"],
            raw_response={"preflight": preflight},
            error_message=preflight["reason"],
        )
        raise ValueError(preflight["reason"])

    decision = _paper_order_decision(plan)
    if decision["status"] != OrderStatus.FILLED:
        updated = db.update_trade_order(
            trade_order["id"],
            status=decision["status"].value,
            error_message=decision.get("reason"),
            raw_response={"preflight": preflight, "paper_decision": _order_decision_payload(decision)},
            submitted=decision["status"] == OrderStatus.SUBMITTED,
            finished=decision["status"] in {OrderStatus.EXPIRED, OrderStatus.REJECTED},
        )
        db.create_execution_event(
            user_id=user["id"],
            order_id=trade_order["id"],
            event_type="order_state",
            status=decision["status"].value,
            price_before=plan["price"],
            risk_checks=preflight["checks"],
            raw_response={"preflight": preflight, "paper_decision": _order_decision_payload(decision)},
            error_message=decision.get("reason"),
        )
        return {
            "order": updated,
            "run": None,
            "trade": None,
            "account": db.get_or_create_paper_account(user["id"]),
            "position": db.get_paper_position(user["id"], plan["symbol"]),
        }

    db.update_trade_order(
        trade_order["id"],
        status=OrderStatus.SUBMITTED.value,
        raw_response={"preflight": preflight},
        submitted=True,
    )
    execution = PaperExchangeConnector(fee_bps=payload.fee_bps, slippage_bps=payload.slippage_bps).submit_market_order(
        order,
        decision["reference_price"],
    )
    if execution.status != OrderStatus.FILLED or execution.fill is None:
        db.update_trade_order(
            trade_order["id"],
            status=execution.status.value,
            exchange_order_id=execution.exchange_order_id,
            error_message=execution.error_message or execution.status.value,
            raw_response=execution.raw_response or {},
            finished=True,
        )
        db.create_execution_event(
            user_id=user["id"],
            order_id=trade_order["id"],
            event_type="order_error",
            status=execution.status.value,
            price_before=plan["price"],
            risk_checks=preflight["checks"],
            raw_response=execution.raw_response or {},
            error_message=execution.error_message or execution.status.value,
        )
        raise ValueError(execution.error_message or execution.status.value)

    fill = execution.fill
    try:
        paper_state = db.apply_paper_fill(
            user_id=user["id"],
            symbol=fill.symbol,
            side=fill.side.value,
            quantity=fill.quantity,
            price=fill.price,
            fee=fill.fee,
        )
    except ValueError as exc:
        db.update_trade_order(
            trade_order["id"],
            status=OrderStatus.REJECTED.value,
            exchange_order_id=execution.exchange_order_id,
            error_message=str(exc),
            raw_response={"connector": execution.raw_response or {}, "preflight": preflight},
            finished=True,
        )
        db.create_execution_event(
            user_id=user["id"],
            order_id=trade_order["id"],
            event_type="order_error",
            status=OrderStatus.REJECTED.value,
            price_before=plan["price"],
            price_after=fill.price,
            risk_checks=preflight["checks"],
            raw_response={"connector": execution.raw_response or {}, "preflight": preflight},
            error_message=str(exc),
        )
        raise

    metrics = {
        "cash": paper_state["account"]["cash"],
        "trade_count": 1,
        "notional": fill.quantity * fill.price,
        "total_fees": fill.fee,
        "realized_pnl": paper_state.get("realized_pnl_delta", 0.0),
    }
    run = db.create_trade_run(
        user_id=user["id"],
        strategy_type="manual_order",
        parameters={
            "action": plan["action"],
            "symbol": plan["symbol"],
            "quantity": plan["quantity"],
            "price": plan["price"],
            "fee_bps": payload.fee_bps,
            "slippage_bps": payload.slippage_bps,
            "order_id": trade_order["id"],
            "order_type": plan["order_type"].value,
            "time_in_force": plan["time_in_force"].value,
            "post_only": plan["post_only"],
            "reduce_only": plan["reduce_only"],
            "leverage": plan["leverage"],
        },
        metrics=metrics,
        run_mode="paper",
        status="completed",
        market_type=payload.market_type,
        started_at=fill.timestamp.isoformat(),
        finished_at=fill.timestamp.isoformat(),
    )
    records = db.create_trade_records(
        run_id=run["id"],
        user_id=user["id"],
        strategy_config_id=None,
        run_mode="paper",
        exchange="paper",
        market_type=payload.market_type,
        trades=[
            {
                "symbol": fill.symbol,
                "side": fill.side.value,
                "quantity": fill.quantity,
                "price": fill.price,
                "fee": fill.fee,
                "order_id": fill.order_id,
                "reason": fill.reason,
                "realized_pnl": paper_state.get("realized_pnl_delta"),
                "traded_at": fill.timestamp.isoformat(),
            }
        ],
    )
    updated_order = db.update_trade_order(
        trade_order["id"],
        status=OrderStatus.FILLED.value,
        exchange_order_id=execution.exchange_order_id,
        run_id=run["id"],
        filled_quantity=fill.quantity,
        average_fill_price=fill.price,
        raw_response={"connector": execution.raw_response or {}, "preflight": preflight},
        finished=True,
    )
    db.create_execution_event(
        user_id=user["id"],
        order_id=trade_order["id"],
        run_id=run["id"],
        event_type="order_filled",
        status=OrderStatus.FILLED.value,
        price_before=plan["price"],
        price_after=fill.price,
        risk_checks=preflight["checks"],
        strategy_snapshot={"strategy_type": "manual_order", "parameters": run["parameters"]},
        raw_response={"connector": execution.raw_response or {}, "preflight": preflight},
    )
    return {
        "order": updated_order,
        "run": run,
        "trade": records[0] if records else None,
        "account": paper_state["account"],
        "position": paper_state["position"],
    }


def _execute_strategy_paper(db: Database, user: dict, payload: AutoPaperExecuteRequest) -> dict:
    risk_settings = db.get_or_create_risk_settings(user["id"])
    if not risk_settings["paper_trading_enabled"]:
        raise ValueError("paper_trading_paused_by_risk_settings")

    started_at = utc_now()
    bars_file = _safe_project_path(payload.bars_file)
    events_file = _safe_project_path(payload.events_file) if payload.events_file else None
    strategy_type, parameters, strategy_config = _resolve_strategy_request(db, user, payload)

    account = db.get_or_create_paper_account(user["id"])
    sources = [CsvBarDataSource(bars_file)]
    if events_file and events_file.exists():
        sources.append(CsvExternalEventSource(events_file))

    engine = BacktestEngine(
        strategy=create_strategy(strategy_type, parameters),
        broker=SimulatedBroker(fee_bps=payload.fee_bps, slippage_bps=payload.slippage_bps),
        risk_manager=RiskManager(
            RiskConfig(
                max_order_notional=min(payload.max_order_notional, float(risk_settings["max_order_notional"])),
                max_symbol_weight=min(payload.max_symbol_weight, float(risk_settings["max_symbol_weight"])),
                max_gross_exposure=min(payload.max_gross_exposure, float(risk_settings["max_gross_exposure"])),
            )
        ),
        config=BacktestConfig(initial_cash=float(account["cash"])),
    )
    result = engine.run(MergedEventFeed(sources))
    strategy_config_id = strategy_config["id"] if strategy_config else None
    strategy_version = strategy_config["current_version"] if strategy_config else None
    market_type = strategy_config["market_type"] if strategy_config else "spot"

    applied = []
    for fill in result.fills:
        applied.append(
            db.apply_paper_fill(
                user_id=user["id"],
                symbol=fill.symbol,
                side=fill.side.value,
                quantity=fill.quantity,
                price=fill.price,
                fee=fill.fee,
                initial_cash=float(account["cash"]),
            )
        )

    metrics = {
        "trade_count": len(result.fills),
        "total_fees": sum(fill.fee for fill in result.fills),
        "ending_cash": db.get_or_create_paper_account(user["id"])["cash"],
    }
    if strategy_version is not None:
        metrics["strategy_version"] = strategy_version
    run = db.create_trade_run(
        user_id=user["id"],
        strategy_config_id=strategy_config_id,
        strategy_version=strategy_version,
        strategy_type=strategy_type,
        run_mode="paper",
        status="completed",
        market_type=market_type,
        bars_file=str(payload.bars_file),
        events_file=str(payload.events_file) if payload.events_file else None,
        parameters=parameters,
        metrics=metrics,
        started_at=started_at,
        finished_at=utc_now(),
    )
    records = db.create_trade_records(
        run_id=run["id"],
        user_id=user["id"],
        strategy_config_id=strategy_config_id,
        run_mode="paper",
        exchange="paper",
        market_type=market_type,
        trades=[
            {
                "symbol": fill.symbol,
                "side": fill.side.value,
                "quantity": fill.quantity,
                "price": fill.price,
                "fee": fill.fee,
                "order_id": fill.order_id,
                "reason": fill.reason,
                "traded_at": fill.timestamp.isoformat(),
            }
            for fill in result.fills
        ],
    )
    orders = [
        _record_filled_paper_order(
            db=db,
            user_id=user["id"],
            fill=fill,
            run_id=run["id"],
            strategy_config_id=strategy_config_id,
            market_type=market_type,
        )
        for fill in result.fills
    ]
    return {
        "run": run,
        "strategy": {
            "strategy_type": strategy_type,
            "parameters": parameters,
            "config": strategy_config,
        },
        "trade_count": len(records),
        "orders": orders,
        "records": records,
        "account": db.get_or_create_paper_account(user["id"]),
        "positions": db.list_paper_positions(user["id"]),
    }


def _manual_order_plan(db: Database, user: dict, payload: ManualPaperOrderRequest) -> dict[str, Any]:
    action = payload.action.strip().lower()
    side = _side_from_action(action)
    symbol = payload.symbol.strip().upper()
    if not symbol:
        raise ValueError("symbol_required")

    order_type = _normalize_order_type(payload.order_type)
    time_in_force = _normalize_time_in_force(payload.time_in_force)
    price = float(payload.price)
    if price <= 0:
        raise ValueError("price_required")

    limit_price = payload.limit_price
    if order_type is OrderType.LIMIT and (limit_price is None or limit_price <= 0):
        raise ValueError("limit_price_required")
    stop_price = payload.stop_price
    if order_type in {OrderType.STOP_LOSS, OrderType.TAKE_PROFIT} and (stop_price is None or stop_price <= 0):
        raise ValueError("stop_price_required")
    trailing_delta_pct = payload.trailing_delta_pct
    if order_type is OrderType.TRAILING_STOP and (trailing_delta_pct is None or trailing_delta_pct <= 0):
        raise ValueError("trailing_delta_required")

    quantity = payload.quantity
    if action == "close_long" and quantity is None:
        position = db.get_paper_position(user["id"], symbol)
        quantity = float(position["quantity"]) if position else 0.0
    if quantity is None or quantity <= 0:
        raise ValueError("quantity_required")

    return {
        "action": action,
        "side": side,
        "symbol": symbol,
        "quantity": float(quantity),
        "price": price,
        "order_type": order_type,
        "time_in_force": time_in_force,
        "limit_price": float(limit_price) if limit_price is not None else None,
        "stop_price": float(stop_price) if stop_price is not None else None,
        "trailing_delta_pct": float(trailing_delta_pct) if trailing_delta_pct is not None else None,
        "post_only": bool(payload.post_only),
        "reduce_only": bool(payload.reduce_only),
        "leverage": float(payload.leverage),
        "confirm_order": bool(payload.confirm_order),
        "market_type": payload.market_type.strip().lower(),
    }


def _normalize_order_type(value: str) -> OrderType:
    normalized = value.strip().lower().replace("-", "_")
    try:
        return OrderType(normalized)
    except ValueError as exc:
        raise ValueError(f"unsupported_order_type:{value}") from exc


def _normalize_time_in_force(value: str) -> TimeInForce:
    normalized = value.strip().upper()
    try:
        return TimeInForce(normalized)
    except ValueError as exc:
        raise ValueError(f"unsupported_time_in_force:{value}") from exc


def _paper_order_decision(plan: dict[str, Any]) -> dict[str, Any]:
    order_type = plan["order_type"]
    side = plan["side"]
    reference_price = float(plan["price"])

    if order_type is OrderType.MARKET:
        return {"status": OrderStatus.FILLED, "reason": "market_order", "reference_price": reference_price}

    if order_type is OrderType.LIMIT:
        limit_price = float(plan["limit_price"])
        marketable = (side is Side.BUY and limit_price >= reference_price) or (side is Side.SELL and limit_price <= reference_price)
        if plan["post_only"] and marketable:
            return {
                "status": OrderStatus.REJECTED,
                "reason": "post_only_would_take_liquidity",
                "reference_price": reference_price,
            }
        if marketable:
            return {"status": OrderStatus.FILLED, "reason": "marketable_limit", "reference_price": reference_price}
        if plan["time_in_force"] in {TimeInForce.IOC, TimeInForce.FOK}:
            return {"status": OrderStatus.EXPIRED, "reason": "not_filled_immediately", "reference_price": reference_price}
        return {"status": OrderStatus.SUBMITTED, "reason": "resting_limit_order", "reference_price": reference_price}

    if order_type in {OrderType.STOP_LOSS, OrderType.TAKE_PROFIT, OrderType.TRAILING_STOP}:
        return {"status": OrderStatus.SUBMITTED, "reason": "conditional_order_waiting_trigger", "reference_price": reference_price}

    return {"status": OrderStatus.REJECTED, "reason": "unsupported_order_type", "reference_price": reference_price}


def _order_decision_payload(decision: dict[str, Any]) -> dict[str, Any]:
    payload = dict(decision)
    if isinstance(payload.get("status"), OrderStatus):
        payload["status"] = payload["status"].value
    return payload


def _generate_market_snapshot(symbol: str) -> dict[str, Any]:
    normalized_symbol = symbol.strip().upper() or "BTCUSDT"
    base_prices = {
        "BTCUSDT": 64_000.0,
        "ETHUSDT": 3_200.0,
        "SOLUSDT": 145.0,
        "BNBUSDT": 590.0,
        "XRPUSDT": 0.55,
        "DOGEUSDT": 0.13,
    }
    base_price = base_prices.get(normalized_symbol, max(0.05, sum(ord(char) for char in normalized_symbol) % 500 + 1.0))
    now = _now_datetime()
    phase = now.timestamp() / 60 + (sum(ord(char) for char in normalized_symbol) % 97)
    drift = math.sin(phase / 3) * 0.003 + math.cos(phase / 11) * 0.0015
    last_price = max(base_price * (1 + drift), 0.0001)
    spread_bps = 4.0 + abs(math.sin(phase)) * 5.0
    half_spread = last_price * spread_bps / 20_000
    best_bid = max(last_price - half_spread, 0.0001)
    best_ask = last_price + half_spread
    timestamp = now.isoformat()

    bids = []
    asks = []
    for level in range(5):
        depth_step = (level + 1) * last_price * 0.0007
        size = round(0.8 + (level + 1) * 0.35 + abs(math.sin(phase + level)), 6)
        bids.append({"price": round(max(best_bid - depth_step, 0.0001), 8), "quantity": size})
        asks.append({"price": round(best_ask + depth_step, 8), "quantity": round(size * 0.92, 6)})

    recent_trades = []
    for index in range(8):
        trade_wave = math.sin(phase + index / 2)
        recent_trades.append(
            {
                "price": round(max(last_price * (1 + trade_wave * 0.0009), 0.0001), 8),
                "quantity": round(0.05 + abs(math.cos(phase + index)) * 0.45, 6),
                "side": "buy" if trade_wave >= 0 else "sell",
                "timestamp": timestamp,
            }
        )

    candle_open = last_price * (1 - drift / 2)
    candle_high = max(candle_open, last_price) * 1.002
    candle_low = min(candle_open, last_price) * 0.998
    return {
        "symbol": normalized_symbol,
        "source": "local_simulated_ws",
        "best_bid": round(best_bid, 8),
        "best_ask": round(best_ask, 8),
        "last_price": round(last_price, 8),
        "order_book": {"bids": bids, "asks": asks, "depth": 5, "timestamp": timestamp},
        "recent_trades": recent_trades,
        "kline": {
            "interval": "1m",
            "open": round(candle_open, 8),
            "high": round(candle_high, 8),
            "low": round(candle_low, 8),
            "close": round(last_price, 8),
            "volume": round(500 + abs(math.sin(phase)) * 2_500, 4),
            "timestamp": timestamp,
        },
    }


def _evaluate_simple_signal(snapshot: dict[str, Any], parameters: dict[str, Any]) -> dict[str, Any]:
    params = parameters or {}
    kline = snapshot.get("kline", {}) or {}
    open_price = float(kline.get("open") or snapshot["last_price"])
    close_price = float(kline.get("close") or snapshot["last_price"])
    momentum_bps = ((close_price - open_price) / open_price * 10_000) if open_price else 0.0
    order_book = snapshot.get("order_book", {}) or {}
    bids = order_book.get("bids", []) or []
    asks = order_book.get("asks", []) or []
    bid_depth = sum(float(level.get("quantity", 0.0)) for level in bids[:5])
    ask_depth = sum(float(level.get("quantity", 0.0)) for level in asks[:5])
    imbalance = (bid_depth - ask_depth) / max(bid_depth + ask_depth, 1e-9)
    momentum_threshold = abs(float(params.get("momentum_threshold_bps", params.get("entry_z", 8.0)))) or 8.0
    imbalance_threshold = float(params.get("imbalance_threshold", 0.08))
    score = momentum_bps / max(abs(momentum_threshold), 1e-9) + imbalance
    if momentum_bps >= momentum_threshold and imbalance >= -imbalance_threshold:
        signal = "buy"
    elif momentum_bps <= -momentum_threshold and imbalance <= imbalance_threshold:
        signal = "sell"
    else:
        signal = "hold"
    return {
        "signal": signal,
        "score": round(score, 6),
        "reason": (
            f"momentum_bps={momentum_bps:.2f}, imbalance={imbalance:.4f}, "
            f"spread_bps={float(snapshot.get('spread_bps', 0.0)):.2f}"
        ),
    }


def _emit_notification(
    db: Database,
    user_id: int,
    event_type: str,
    title: str,
    message: str,
    payload: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    notifications = [
        db.create_notification(
            user_id=user_id,
            channel_type="local",
            event_type=event_type,
            title=title,
            message=message,
            payload=payload or {},
            status="stored",
            destination="notification_center",
            delivered_at=utc_now(),
        )
    ]
    for channel in db.list_notification_channels(user_id):
        if not channel["is_active"]:
            continue
        channel_type = channel["channel_type"]
        if channel_type == "local":
            continue
        notifications.append(
            db.create_notification(
                user_id=user_id,
                channel_type=channel_type,
                event_type=event_type,
                title=title,
                message=message,
                payload=payload or {},
                status="queued",
                destination=channel["destination"],
            )
        )
    return notifications


def _run_automation_once(db: Database, user: dict, payload: AutomationRunOnceRequest) -> dict[str, Any]:
    run_mode = payload.run_mode.strip().lower()
    if run_mode == "live":
        raise ValueError("live_automation_not_configured")
    if run_mode != "paper":
        raise ValueError("unsupported_run_mode")

    if db.get_system_flag("emergency_stop", "0")["value"] == "1":
        raise ValueError("global_emergency_stop_active")

    control = _active_automation_control(db, user["id"], payload.strategy_config_id, run_mode)
    if not control:
        raise ValueError("automation_not_running")

    strategy_config = None
    parameters = dict(payload.parameters)
    strategy_type = payload.strategy_type
    if payload.strategy_config_id is not None:
        strategy_config = db.get_strategy_config(
            payload.strategy_config_id,
            user_id=user["id"],
            is_admin=has_user_permission(user, MANAGE_ALL_STRATEGIES),
        )
        if not strategy_config:
            raise ValueError("strategy_config_not_found")
        parameters = strategy_config["parameters"]
        strategy_type = strategy_config["strategy_type"]

    snapshot = db.create_realtime_market_snapshot(**_generate_market_snapshot(payload.symbol))
    signal = _evaluate_simple_signal(snapshot, parameters)
    signal_log = db.create_signal_log(
        user_id=user["id"],
        strategy_config_id=strategy_config["id"] if strategy_config else None,
        strategy_type=strategy_type,
        symbol=snapshot["symbol"],
        signal=signal["signal"],
        score=signal["score"],
        parameters=parameters,
        market_snapshot=snapshot,
        reason=signal["reason"],
    )

    if signal["signal"] == "hold":
        return {
            "status": "no_order",
            "reason": "signal_hold",
            "control": control,
            "snapshot": snapshot,
            "signal": signal,
            "signal_log": signal_log,
            "order_result": None,
        }

    last_price = float(snapshot["last_price"])
    if signal["signal"] == "buy":
        quantity = payload.quantity or payload.order_notional / max(last_price, 1e-9)
        order_payload = ManualPaperOrderRequest(
            symbol=snapshot["symbol"],
            action="open_long",
            quantity=quantity,
            price=last_price,
            fee_bps=payload.fee_bps,
            slippage_bps=payload.slippage_bps,
            confirm_order=payload.confirm_order,
            reason="automation_run_once",
            market_type="spot",
        )
    else:
        position = db.get_paper_position(user["id"], snapshot["symbol"])
        position_quantity = float(position["quantity"]) if position else 0.0
        if position_quantity <= 1e-12:
            return {
                "status": "no_order",
                "reason": "no_position_to_close",
                "control": control,
                "snapshot": snapshot,
                "signal": signal,
                "signal_log": signal_log,
                "order_result": None,
            }
        quantity = min(payload.quantity or position_quantity, position_quantity)
        order_payload = ManualPaperOrderRequest(
            symbol=snapshot["symbol"],
            action="close_long",
            quantity=quantity,
            price=last_price,
            reduce_only=True,
            fee_bps=payload.fee_bps,
            slippage_bps=payload.slippage_bps,
            confirm_order=payload.confirm_order,
            reason="automation_run_once",
            market_type="spot",
        )

    order_result = _execute_manual_paper_order(db, user, order_payload)
    if order_result.get("order"):
        _emit_notification(
            db,
            user["id"],
            "trade_filled" if order_result["order"]["status"] == OrderStatus.FILLED.value else "order_state",
            "自动策略 Paper 订单状态更新",
            f"{order_result['order']['symbol']} {order_result['order']['side']} {order_result['order']['status']}",
            {"signal": signal_log, "order": order_result["order"], "trade": order_result.get("trade")},
        )
    return {
        "status": "order_submitted",
        "reason": signal["signal"],
        "control": control,
        "snapshot": snapshot,
        "signal": signal,
        "signal_log": signal_log,
        "order_result": order_result,
    }


def _active_automation_control(
    db: Database,
    user_id: int,
    strategy_config_id: int | None,
    run_mode: str,
) -> dict[str, Any] | None:
    controls = db.list_automation_controls(user_id, include_all=False)
    exact_match = None
    global_match = None
    for control in controls:
        if control["run_mode"] != run_mode:
            continue
        if control["strategy_config_id"] == strategy_config_id:
            exact_match = control
        if control["strategy_config_id"] is None:
            global_match = control
    control = exact_match or global_match
    if not control:
        return None
    if control["status"] != "running" or not control["trading_enabled"]:
        return None
    return control


def _dispatch_queued_notifications(db: Database, user: dict, limit: int = 20, dry_run: bool = True) -> dict[str, Any]:
    queued = [item for item in db.list_notifications(user["id"], include_all=False, limit=500) if item["status"] == "queued"]
    selected = queued[:limit]
    results = []
    for notification in selected:
        updated_payload = dict(notification["payload"] or {})
        attempts = list(updated_payload.get("dispatch_attempts") or [])
        attempt = {
            "at": utc_now(),
            "dry_run": bool(dry_run),
            "channel_type": notification["channel_type"],
            "destination": notification["destination"],
        }
        if dry_run:
            attempt["status"] = "dry_run"
            next_status = "dry_run"
            delivered_at = utc_now()
        elif os.environ.get("CRYPTO_LAB_ENABLE_OUTBOUND_NOTIFICATIONS", "0") != "1":
            attempt["status"] = "blocked_outbound_disabled"
            next_status = "blocked_outbound_disabled"
            delivered_at = None
        else:
            next_status, delivered_at = _deliver_notification(notification, attempt)
        attempts.append(attempt)
        updated_payload["dispatch_attempts"] = attempts
        updated = db.update_notification_status(
            notification_id=notification["id"],
            user_id=user["id"],
            status=next_status,
            payload=updated_payload,
            delivered_at=delivered_at,
        )
        results.append(updated or notification)
    remaining_queued = len([item for item in db.list_notifications(user["id"], include_all=False, limit=500) if item["status"] == "queued"])
    return {
        "dry_run": dry_run,
        "requested_count": len(selected),
        "dispatched_count": len(results),
        "remaining_queued": remaining_queued,
        "results": results,
    }


def _deliver_notification(notification: dict[str, Any], attempt: dict[str, Any]) -> tuple[str, str | None]:
    channel_type = notification["channel_type"]
    destination = notification["destination"]
    body = {
        "event_type": notification["event_type"],
        "title": notification["title"],
        "message": notification["message"],
        "payload": notification["payload"],
        "created_at": notification["created_at"],
    }
    try:
        if channel_type == "webhook":
            if not destination.startswith(("https://", "http://")):
                attempt["status"] = "invalid_webhook_url"
                return "failed", None
            response = httpx.post(destination, json=body, timeout=5.0)
            attempt["status"] = "delivered" if response.status_code < 400 else "failed"
            attempt["response_status_code"] = response.status_code
            return ("delivered" if response.status_code < 400 else "failed", utc_now() if response.status_code < 400 else None)
        if channel_type == "telegram":
            token = os.environ.get("CRYPTO_LAB_TELEGRAM_BOT_TOKEN", "")
            chat_id = destination
            if not token or not chat_id:
                attempt["status"] = "missing_telegram_config"
                return "failed", None
            response = httpx.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": f"{notification['title']}\n{notification['message']}"},
                timeout=5.0,
            )
            attempt["status"] = "delivered" if response.status_code < 400 else "failed"
            attempt["response_status_code"] = response.status_code
            return ("delivered" if response.status_code < 400 else "failed", utc_now() if response.status_code < 400 else None)
        if channel_type == "email":
            email_webhook = os.environ.get("CRYPTO_LAB_EMAIL_WEBHOOK_URL", "")
            if not email_webhook:
                attempt["status"] = "missing_email_webhook"
                return "failed", None
            response = httpx.post(email_webhook, json={**body, "to": destination}, timeout=5.0)
            attempt["status"] = "delivered" if response.status_code < 400 else "failed"
            attempt["response_status_code"] = response.status_code
            return ("delivered" if response.status_code < 400 else "failed", utc_now() if response.status_code < 400 else None)
        attempt["status"] = "unsupported_channel"
        return "failed", None
    except httpx.HTTPError as exc:
        attempt["status"] = "failed"
        attempt["error"] = str(exc)
        return "failed", None


def _close_all_paper_positions(db: Database, user: dict, payload: CloseAllPositionsRequest) -> dict[str, Any]:
    positions = [
        position
        for position in db.list_paper_positions(user["id"], include_closed=False)
        if abs(float(position["quantity"])) > 1e-12
    ]
    started_at = utc_now()
    price_map = {symbol.strip().upper(): float(price) for symbol, price in (payload.price_map or {}).items() if float(price) > 0}
    fills: list[dict[str, Any]] = []
    for position in positions:
        symbol = position["symbol"]
        quantity = float(position["quantity"])
        if quantity <= 0:
            continue
        price = price_map.get(symbol) or float(position["average_price"])
        if price <= 0:
            continue
        client_order_id = f"close-all-{symbol}-{int(_now_datetime().timestamp() * 1000)}"
        paper_state = db.apply_paper_fill(
            user_id=user["id"],
            symbol=symbol,
            side=Side.SELL.value,
            quantity=quantity,
            price=price,
            fee=0.0,
        )
        fills.append(
            {
                "client_order_id": client_order_id,
                "symbol": symbol,
                "side": Side.SELL.value,
                "quantity": quantity,
                "price": price,
                "fee": 0.0,
                "reason": payload.reason,
                "realized_pnl": paper_state.get("realized_pnl_delta", 0.0),
                "traded_at": utc_now(),
                "paper_state": paper_state,
            }
        )

    metrics = {
        "trade_count": len(fills),
        "total_notional": sum(fill["quantity"] * fill["price"] for fill in fills),
        "total_fees": sum(fill["fee"] for fill in fills),
        "realized_pnl": sum(float(fill.get("realized_pnl") or 0.0) for fill in fills),
    }
    run = db.create_trade_run(
        user_id=user["id"],
        strategy_type="close_all_positions",
        parameters={"run_mode": payload.run_mode, "price_map": price_map, "reason": payload.reason},
        metrics=metrics,
        run_mode="paper",
        status="completed",
        market_type="spot",
        started_at=started_at,
        finished_at=utc_now(),
    )

    orders = []
    for fill in fills:
        order = db.create_trade_order(
            user_id=user["id"],
            client_order_id=fill["client_order_id"],
            symbol=fill["symbol"],
            side=fill["side"],
            quantity=fill["quantity"],
            requested_price=fill["price"],
            order_type=OrderType.MARKET.value,
            time_in_force=TimeInForce.GTC.value,
            reduce_only=True,
            run_mode="paper",
            exchange="paper",
            market_type="spot",
            run_id=run["id"],
            reason=payload.reason,
            status=OrderStatus.SUBMITTED.value,
            raw_response={"source": "automation.close_all"},
        )
        updated_order = db.update_trade_order(
            order["id"],
            status=OrderStatus.FILLED.value,
            exchange_order_id=f"paper-{fill['client_order_id']}",
            filled_quantity=fill["quantity"],
            average_fill_price=fill["price"],
            raw_response={"source": "automation.close_all", "simulated": True},
            submitted=True,
            finished=True,
        )
        if updated_order:
            orders.append(updated_order)
            db.create_execution_event(
                user_id=user["id"],
                order_id=updated_order["id"],
                run_id=run["id"],
                event_type="order_filled",
                status=OrderStatus.FILLED.value,
                price_before=fill["price"],
                price_after=fill["price"],
                risk_checks=[{"name": "close_all_positions", "passed": True, "detail": payload.reason}],
                strategy_snapshot={"strategy_type": "close_all_positions", "parameters": run["parameters"]},
                raw_response={"source": "automation.close_all", "simulated": True},
            )

    records = db.create_trade_records(
        run_id=run["id"],
        user_id=user["id"],
        strategy_config_id=None,
        run_mode="paper",
        exchange="paper",
        market_type="spot",
        trades=[
            {
                "symbol": fill["symbol"],
                "side": fill["side"],
                "quantity": fill["quantity"],
                "price": fill["price"],
                "fee": fill["fee"],
                "order_id": fill["client_order_id"],
                "reason": fill["reason"],
                "realized_pnl": fill["realized_pnl"],
                "traded_at": fill["traded_at"],
            }
            for fill in fills
        ],
    )
    return {
        "run": run,
        "closed_count": len(fills),
        "orders": orders,
        "records": records,
        "account": db.get_or_create_paper_account(user["id"]),
        "positions": db.list_paper_positions(user["id"]),
    }


def _local_trading_snapshot(db: Database, user: dict) -> dict[str, Any]:
    account = db.get_or_create_paper_account(user["id"])
    positions = db.list_paper_positions(user["id"], include_closed=False)
    orders = [
        order
        for order in db.list_trade_orders(user["id"], include_all=False, limit=200, run_mode="paper")
        if order["status"] in {OrderStatus.CREATED.value, OrderStatus.SUBMITTED.value, OrderStatus.PARTIALLY_FILLED.value}
    ]
    return {
        "cash": float(account["cash"]),
        "positions": {
            position["symbol"]: {
                "quantity": float(position["quantity"]),
                "average_price": float(position["average_price"]),
                "realized_pnl": float(position["realized_pnl"]),
            }
            for position in positions
        },
        "open_orders": {
            str(order["id"]): {
                "client_order_id": order["client_order_id"],
                "symbol": order["symbol"],
                "side": order["side"],
                "order_type": order["order_type"],
                "quantity": float(order["quantity"]),
                "filled_quantity": float(order["filled_quantity"]),
                "status": order["status"],
            }
            for order in orders
        },
    }


def _snapshot_differences(local_snapshot: dict[str, Any], exchange_snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    differences: list[dict[str, Any]] = []
    local_cash = float(local_snapshot.get("cash", 0.0))
    exchange_cash = float(exchange_snapshot.get("cash", 0.0))
    if abs(local_cash - exchange_cash) > 1e-8:
        differences.append({"type": "cash_mismatch", "local": local_cash, "exchange": exchange_cash})

    local_positions = local_snapshot.get("positions", {}) or {}
    exchange_positions = exchange_snapshot.get("positions", {}) or {}
    for symbol in sorted(set(local_positions) | set(exchange_positions)):
        local_position = local_positions.get(symbol, {})
        exchange_position = exchange_positions.get(symbol, {})
        local_quantity = float(local_position.get("quantity", 0.0))
        exchange_quantity = float(exchange_position.get("quantity", 0.0))
        if abs(local_quantity - exchange_quantity) > 1e-8:
            differences.append(
                {
                    "type": "position_quantity_mismatch",
                    "symbol": symbol,
                    "local": local_quantity,
                    "exchange": exchange_quantity,
                }
            )

    local_orders = local_snapshot.get("open_orders", {}) or {}
    exchange_orders = exchange_snapshot.get("open_orders", {}) or {}
    for order_id in sorted(set(local_orders) | set(exchange_orders)):
        if order_id not in local_orders:
            differences.append({"type": "order_missing_locally", "order_id": order_id, "exchange": exchange_orders[order_id]})
        elif order_id not in exchange_orders:
            differences.append({"type": "order_missing_on_exchange", "order_id": order_id, "local": local_orders[order_id]})
        elif local_orders[order_id].get("status") != exchange_orders[order_id].get("status"):
            differences.append(
                {
                    "type": "order_status_mismatch",
                    "order_id": order_id,
                    "local": local_orders[order_id].get("status"),
                    "exchange": exchange_orders[order_id].get("status"),
                }
            )
    return differences


def _manual_order_preflight(
    db: Database,
    user: dict,
    payload: ManualPaperOrderRequest,
    plan: dict[str, Any],
    run_mode: str = "paper",
) -> dict[str, Any]:
    run_mode = run_mode.strip().lower()
    risk_settings = db.get_or_create_risk_settings(user["id"])
    quantity = float(plan["quantity"])
    reference_price = float(plan["price"])
    notional = quantity * reference_price
    slippage = payload.slippage_bps / 10_000
    estimated_fill_price = reference_price * (1 + slippage if plan["side"] is Side.BUY else 1 - slippage)
    estimated_fill_notional = quantity * estimated_fill_price
    estimated_fee = quantity * estimated_fill_price * payload.fee_bps / 10_000
    estimated_slippage_cost = abs(estimated_fill_price - reference_price) * quantity
    leverage = max(float(plan["leverage"]), 1e-9)
    maintenance_margin_ratio = float(payload.maintenance_margin_ratio)
    initial_margin_required = estimated_fill_notional / leverage
    maintenance_margin_required = estimated_fill_notional * maintenance_margin_ratio
    estimated_funding_8h = estimated_fill_notional * float(payload.funding_bps_per_8h) / 10_000
    liquidation_buffer = max((1 / leverage) - maintenance_margin_ratio, 0.0)
    if plan["market_type"] == "spot":
        estimated_liquidation_price = None
    elif plan["side"] is Side.BUY:
        estimated_liquidation_price = max(0.0, estimated_fill_price * (1 - liquidation_buffer))
    else:
        estimated_liquidation_price = estimated_fill_price * (1 + liquidation_buffer)
    rule_exchange = "paper" if run_mode == "paper" else os.environ.get("CRYPTO_LAB_LIVE_EXCHANGE", "live")
    symbol_rule = db.get_symbol_rule(plan["symbol"], exchange=rule_exchange, market_type=plan["market_type"])
    checks: list[dict[str, Any]] = []

    def add_check(name: str, passed: bool, detail: str = "") -> None:
        checks.append({"name": name, "passed": bool(passed), "detail": detail})

    emergency_stop = db.get_system_flag("emergency_stop", "0")
    add_check(
        "global_emergency_stop",
        emergency_stop["value"] != "1",
        "inactive" if emergency_stop["value"] != "1" else "global emergency stop active",
    )

    if run_mode == "paper":
        add_check(
            "paper_trading_enabled",
            bool(risk_settings["paper_trading_enabled"]),
            "paper trading enabled" if risk_settings["paper_trading_enabled"] else "paper trading paused",
        )
    elif run_mode == "live":
        live_enabled = (
            os.environ.get("CRYPTO_LAB_ENABLE_LIVE_TRADING", "0") == "1"
            and bool(risk_settings["live_trading_enabled"])
            and has_user_permission(user, EXECUTE_LIVE_TRADES)
        )
        add_check(
            "live_trading_enabled",
            live_enabled,
            "live trading gate passed" if live_enabled else "live trading disabled or missing permission",
        )
    else:
        add_check("supported_run_mode", False, f"unsupported run_mode: {run_mode}")

    add_check(
        "max_leverage",
        float(plan["leverage"]) <= float(risk_settings["max_leverage"]) + 1e-9,
        f"{float(plan['leverage']):.2f} <= {float(risk_settings['max_leverage']):.2f}",
    )
    if symbol_rule:
        min_quantity = float(symbol_rule["min_quantity"])
        quantity_step = float(symbol_rule["quantity_step"])
        price_tick = float(symbol_rule["price_tick"])
        min_notional = float(symbol_rule["min_notional"])
        max_symbol_notional = float(symbol_rule["max_notional"])
        max_symbol_leverage = float(symbol_rule["max_leverage"])
        add_check(
            "symbol_min_quantity",
            min_quantity <= 0 or quantity >= min_quantity - 1e-12,
            f"{quantity:.12f} >= {min_quantity:.12f}",
        )
        add_check(
            "symbol_quantity_step",
            _is_step_aligned(quantity, quantity_step),
            f"quantity={quantity:.12f}, step={quantity_step:.12f}",
        )
        price_fields: list[tuple[str, float]] = []
        if plan["order_type"] is OrderType.LIMIT and payload.limit_price is not None:
            price_fields.append(("limit_price", float(payload.limit_price)))
        elif plan["order_type"] in {OrderType.STOP_LOSS, OrderType.TAKE_PROFIT} and payload.stop_price is not None:
            price_fields.append(("stop_price", float(payload.stop_price)))
        elif plan["order_type"] not in {OrderType.MARKET, OrderType.TRAILING_STOP}:
            price_fields.append(("reference_price", reference_price))
        price_aligned = all(_is_step_aligned(price, price_tick) for _, price in price_fields)
        price_detail = ", ".join(f"{name}={price:.12f}" for name, price in price_fields) or "market order has no fixed price"
        add_check(
            "symbol_price_tick",
            price_aligned,
            f"{price_detail}, tick={price_tick:.12f}",
        )
        add_check(
            "symbol_min_notional",
            min_notional <= 0 or estimated_fill_notional >= min_notional - 1e-9,
            f"{estimated_fill_notional:.4f} >= {min_notional:.4f}",
        )
        add_check(
            "symbol_max_notional",
            max_symbol_notional <= 0 or estimated_fill_notional <= max_symbol_notional + 1e-9,
            f"{estimated_fill_notional:.4f} <= {max_symbol_notional:.4f}",
        )
        add_check(
            "symbol_max_leverage",
            max_symbol_leverage <= 0 or float(plan["leverage"]) <= max_symbol_leverage + 1e-9,
            f"{float(plan['leverage']):.2f} <= {max_symbol_leverage:.2f}",
        )
    if plan["post_only"]:
        add_check(
            "post_only_limit_order",
            plan["order_type"] is OrderType.LIMIT,
            "post-only requires a limit order",
        )
    if plan["market_type"] != "spot" and plan["action"] in {"close_long", "sell"}:
        add_check(
            "reduce_only_required",
            plan["reduce_only"],
            "contract close orders must be reduce-only",
        )
    if bool(risk_settings["require_order_confirmation"]):
        add_check(
            "order_confirmation",
            bool(plan["confirm_order"]),
            "manual confirmation required by risk settings",
        )

    max_notional = float(risk_settings["max_order_notional"])
    add_check(
        "max_order_notional",
        notional <= max_notional + 1e-9,
        f"{notional:.4f} <= {max_notional:.4f}",
    )

    positions = db.list_paper_positions(user["id"], include_closed=True)
    realized_pnl = sum(float(position["realized_pnl"]) for position in positions)
    max_daily_loss = float(risk_settings["max_daily_loss"])
    add_check(
        "realized_loss_limit",
        max_daily_loss <= 0 or realized_pnl > -max_daily_loss - 1e-9,
        f"realized_pnl={realized_pnl:.4f}, limit=-{max_daily_loss:.4f}",
    )
    if max_daily_loss > 0 and realized_pnl <= -max_daily_loss - 1e-9 and bool(risk_settings["auto_disable_on_daily_loss"]):
        db.update_risk_settings(
            user["id"],
            paper_trading_enabled=False if run_mode == "paper" else None,
            live_trading_enabled=False if run_mode == "live" else None,
        )

    consecutive_losses = _recent_consecutive_paper_losses(db, user["id"])
    max_consecutive_losses = int(risk_settings["max_consecutive_losses"])
    add_check(
        "consecutive_loss_limit",
        max_consecutive_losses <= 0 or consecutive_losses < max_consecutive_losses,
        f"consecutive_losses={consecutive_losses}, limit={max_consecutive_losses}",
    )

    if run_mode == "paper":
        account = db.get_or_create_paper_account(user["id"])
        current_position = next((position for position in positions if position["symbol"] == plan["symbol"]), None)
        current_quantity = float(current_position["quantity"]) if current_position else 0.0
        open_positions = [position for position in positions if abs(float(position["quantity"])) > 1e-12]
        has_position = current_quantity > 1e-12
        opening_long = plan["side"] is Side.BUY and not plan["reduce_only"]
        max_open_positions = int(risk_settings["max_open_positions"])
        add_check(
            "max_open_positions",
            not opening_long or has_position or max_open_positions <= 0 or len(open_positions) < max_open_positions,
            f"open_positions={len(open_positions)}, limit={max_open_positions}",
        )
        add_check(
            "duplicate_position",
            not opening_long or not has_position or bool(risk_settings["allow_duplicate_position"]),
            "duplicate long position allowed" if risk_settings["allow_duplicate_position"] else "duplicate long position blocked",
        )
        reverse_order = plan["side"] is Side.SELL and not plan["reduce_only"] and current_quantity <= 1e-12
        add_check(
            "reverse_position",
            not reverse_order or bool(risk_settings["allow_reverse_position"]),
            "reverse/open-short allowed" if risk_settings["allow_reverse_position"] else "reverse/open-short blocked",
        )
        if plan["side"] is Side.BUY:
            cash_required = quantity * estimated_fill_price + estimated_fee
            if plan["market_type"] != "spot":
                cash_required = initial_margin_required + estimated_fee + max(estimated_funding_8h, 0.0)
            add_check(
                "paper_cash",
                cash_required <= float(account["cash"]) + 1e-9,
                f"required={cash_required:.4f}, cash={float(account['cash']):.4f}",
            )
        else:
            add_check(
                "paper_position",
                quantity <= current_quantity + 1e-9,
                f"requested={quantity:.8f}, available={current_quantity:.8f}",
            )

        if plan["market_type"] != "spot":
            add_check(
                "margin_requirement",
                initial_margin_required + maintenance_margin_required + max(estimated_funding_8h, 0.0)
                <= float(account["cash"]) + abs(current_quantity) * estimated_fill_price + 1e-9,
                (
                    f"initial={initial_margin_required:.4f}, maintenance={maintenance_margin_required:.4f}, "
                    f"funding_8h={estimated_funding_8h:.4f}"
                ),
            )

        exposure = _paper_exposure_projection(
            account_cash=float(account["cash"]),
            positions=positions,
            symbol=plan["symbol"],
            side=plan["side"],
            quantity=quantity,
            fill_price=estimated_fill_price,
        )
        max_symbol_exposure = exposure["equity_proxy"] * float(risk_settings["max_symbol_weight"])
        max_gross_exposure = exposure["equity_proxy"] * float(risk_settings["max_gross_exposure"])
        add_check(
            "max_symbol_weight",
            exposure["symbol_exposure_after"] <= max_symbol_exposure + 1e-9,
            f"after={exposure['symbol_exposure_after']:.4f}, limit={max_symbol_exposure:.4f}",
        )
        add_check(
            "max_gross_exposure",
            exposure["gross_exposure_after"] <= max_gross_exposure + 1e-9,
            f"after={exposure['gross_exposure_after']:.4f}, limit={max_gross_exposure:.4f}",
        )
    else:
        exposure = None

    failed = [check for check in checks if not check["passed"]]
    reason = "approved" if not failed else _preflight_reason(failed[0]["name"])
    result = {
        "approved": not failed,
        "reason": reason,
        "run_mode": run_mode,
        "symbol": plan["symbol"],
        "action": plan["action"],
        "side": plan["side"].value,
        "order_type": plan["order_type"].value,
        "time_in_force": plan["time_in_force"].value,
        "post_only": plan["post_only"],
        "reduce_only": plan["reduce_only"],
        "leverage": plan["leverage"],
        "quantity": quantity,
        "reference_price": reference_price,
        "estimated_fill_price": estimated_fill_price,
        "estimated_fill_notional": estimated_fill_notional,
        "estimated_notional": notional,
        "estimated_fee": estimated_fee,
        "estimated_slippage_bps": payload.slippage_bps,
        "estimated_slippage_cost": estimated_slippage_cost,
        "initial_margin_required": initial_margin_required,
        "maintenance_margin_required": maintenance_margin_required,
        "maintenance_margin_ratio": maintenance_margin_ratio,
        "estimated_funding_8h": estimated_funding_8h,
        "funding_bps_per_8h": payload.funding_bps_per_8h,
        "estimated_liquidation_price": estimated_liquidation_price,
        "symbol_rule": symbol_rule,
        "will_trigger_risk_limit": bool(failed),
        "checks": checks,
    }
    if exposure is not None:
        result["equity_proxy"] = exposure["equity_proxy"]
        result["symbol_exposure_after"] = exposure["symbol_exposure_after"]
        result["gross_exposure_after"] = exposure["gross_exposure_after"]
    return result


def _is_step_aligned(value: float, step: float) -> bool:
    if step <= 0:
        return True
    quotient = value / step
    return math.isclose(quotient, round(quotient), rel_tol=1e-12, abs_tol=1e-9)


def _preflight_reason(check_name: str) -> str:
    return {
        "global_emergency_stop": "global_emergency_stop_active",
        "paper_trading_enabled": "paper_trading_paused_by_risk_settings",
        "live_trading_enabled": "live_trading_disabled",
        "max_order_notional": "max_order_notional_exceeded",
        "max_symbol_weight": "max_symbol_weight_exceeded",
        "max_gross_exposure": "max_gross_exposure_exceeded",
        "max_open_positions": "max_open_positions_exceeded",
        "max_leverage": "max_leverage_exceeded",
        "symbol_min_quantity": "symbol_min_quantity_not_met",
        "symbol_quantity_step": "symbol_quantity_step_mismatch",
        "symbol_price_tick": "symbol_price_tick_mismatch",
        "symbol_min_notional": "symbol_min_notional_not_met",
        "symbol_max_notional": "symbol_max_notional_exceeded",
        "symbol_max_leverage": "symbol_max_leverage_exceeded",
        "consecutive_loss_limit": "max_consecutive_losses_exceeded",
        "order_confirmation": "order_confirmation_required",
        "post_only_limit_order": "post_only_requires_limit_order",
        "reduce_only_required": "reduce_only_required",
        "duplicate_position": "duplicate_position_blocked",
        "reverse_position": "reverse_position_blocked",
        "realized_loss_limit": "max_daily_loss_exceeded",
        "paper_cash": "insufficient_paper_cash",
        "paper_position": "insufficient_paper_position",
        "margin_requirement": "insufficient_margin",
        "supported_run_mode": "unsupported_run_mode",
    }.get(check_name, check_name)


def _recent_consecutive_paper_losses(db: Database, user_id: int) -> int:
    records = db.list_trade_records(user_id=user_id, include_all=False, limit=100)
    count = 0
    for record in records:
        realized_pnl = record.get("realized_pnl")
        if realized_pnl is None:
            continue
        pnl = float(realized_pnl)
        if pnl < 0:
            count += 1
            continue
        if pnl > 0:
            break
    return count


def _paper_exposure_projection(
    account_cash: float,
    positions: list[dict[str, Any]],
    symbol: str,
    side: Side,
    quantity: float,
    fill_price: float,
) -> dict[str, float]:
    current_gross = 0.0
    current_symbol_exposure = 0.0
    for position in positions:
        position_quantity = abs(float(position["quantity"]))
        exposure = position_quantity * float(position["average_price"])
        current_gross += exposure
        if position["symbol"] == symbol:
            current_symbol_exposure = exposure

    trade_exposure = quantity * fill_price
    if side is Side.BUY:
        symbol_after = current_symbol_exposure + trade_exposure
        gross_after = current_gross + trade_exposure
    else:
        reduction = min(current_symbol_exposure, trade_exposure)
        symbol_after = max(0.0, current_symbol_exposure - reduction)
        gross_after = max(0.0, current_gross - reduction)

    equity_proxy = max(account_cash + current_gross, 1e-9)
    return {
        "equity_proxy": equity_proxy,
        "symbol_exposure_after": symbol_after,
        "gross_exposure_after": gross_after,
    }


def _record_filled_paper_order(
    db: Database,
    user_id: int,
    fill: Any,
    run_id: int,
    strategy_config_id: int | None,
    market_type: str,
) -> dict[str, Any]:
    order = db.create_trade_order(
        user_id=user_id,
        client_order_id=fill.order_id,
        symbol=fill.symbol,
        side=fill.side.value,
        quantity=fill.quantity,
        requested_price=fill.price,
        run_mode="paper",
        exchange="paper",
        market_type=market_type,
        strategy_config_id=strategy_config_id,
        run_id=run_id,
        reason=fill.reason,
        status=OrderStatus.SUBMITTED.value,
        raw_response={"source": "strategy_paper_execution"},
    )
    updated = db.update_trade_order(
        order["id"],
        status=OrderStatus.FILLED.value,
        exchange_order_id=f"paper-{fill.order_id}",
        filled_quantity=fill.quantity,
        average_fill_price=fill.price,
        raw_response={
            "connector": "paper",
            "status": OrderStatus.FILLED.value,
            "source": "strategy_paper_execution",
        },
        submitted=True,
        finished=True,
    )
    if updated is None:
        raise KeyError(order["id"])
    return updated


def _run_backtest_result(
    strategy_type: str,
    parameters: dict[str, Any],
    payload: Any,
    feed_events: list,
    market_type: str,
) -> Any:
    engine = BacktestEngine(
        strategy=create_strategy(strategy_type, parameters),
        broker=SimulatedBroker(fee_bps=payload.fee_bps, slippage_bps=payload.slippage_bps),
        risk_manager=RiskManager(
            RiskConfig(
                max_order_notional=payload.max_order_notional,
                max_symbol_weight=payload.max_symbol_weight,
                max_gross_exposure=payload.max_gross_exposure,
            )
        ),
        config=_backtest_config(payload, market_type),
    )
    return engine.run(feed_events)


def _backtest_config(payload: Any, market_type: str) -> BacktestConfig:
    return BacktestConfig(
        initial_cash=payload.cash,
        execution_delay_bars=payload.execution_delay_bars,
        funding_bps_per_bar=payload.funding_bps_per_bar,
        stop_loss_pct=payload.stop_loss_pct,
        take_profit_pct=payload.take_profit_pct,
        max_holding_bars=payload.max_holding_bars,
        leverage=payload.leverage,
        maintenance_margin_ratio=payload.maintenance_margin_ratio,
        market_type=market_type,
    )


def _metrics_payload(metrics: Any) -> dict[str, Any]:
    return {
        "starting_equity": metrics.starting_equity,
        "ending_equity": metrics.ending_equity,
        "total_return": metrics.total_return,
        "max_drawdown": metrics.max_drawdown,
        "sharpe": metrics.sharpe,
        "trade_count": metrics.trade_count,
        "total_fees": metrics.total_fees,
        "total_funding": metrics.total_funding,
        "liquidation_count": metrics.liquidation_count,
        "forced_exit_count": metrics.forced_exit_count,
    }


def _sample_validation_payload(
    strategy_type: str,
    parameters: dict[str, Any],
    payload: BacktestRunRequest,
    feed_events: list,
    market_type: str,
) -> dict[str, Any] | None:
    if payload.out_of_sample_ratio <= 0:
        return None

    if len(feed_events) < 4:
        return {
            "status": "skipped",
            "reason": "not_enough_events",
            "out_of_sample_ratio": payload.out_of_sample_ratio,
        }

    split_index = int(len(feed_events) * (1 - payload.out_of_sample_ratio))
    split_index = max(1, min(split_index, len(feed_events) - 1))
    in_sample_events = feed_events[:split_index]
    out_of_sample_events = feed_events[split_index:]

    in_sample = _run_backtest_result(strategy_type, parameters, payload, in_sample_events, market_type)
    out_of_sample = _run_backtest_result(strategy_type, parameters, payload, out_of_sample_events, market_type)
    return {
        "status": "completed",
        "split_index": split_index,
        "event_count": len(feed_events),
        "in_sample_event_count": len(in_sample_events),
        "out_of_sample_event_count": len(out_of_sample_events),
        "out_of_sample_ratio": payload.out_of_sample_ratio,
        "in_sample": _metrics_payload(in_sample.metrics),
        "out_of_sample": _metrics_payload(out_of_sample.metrics),
    }


def _walk_forward_payload(
    strategy_type: str,
    parameters: dict[str, Any],
    payload: WalkForwardBacktestRequest,
    feed_events: list,
    market_type: str,
) -> dict[str, Any]:
    train_events = int(payload.train_events)
    test_events = int(payload.test_events)
    step_events = int(payload.step_events)
    if len(feed_events) < train_events + test_events:
        return {
            "status": "skipped",
            "reason": "not_enough_events",
            "event_count": len(feed_events),
            "train_events": train_events,
            "test_events": test_events,
            "step_events": step_events,
            "window_count": 0,
            "windows": [],
        }

    windows: list[dict[str, Any]] = []
    start = 0
    while len(windows) < int(payload.max_windows):
        train_start = start
        train_end = train_start + train_events
        test_end = train_end + test_events
        if test_end > len(feed_events):
            break
        train_result = _run_backtest_result(strategy_type, parameters, payload, feed_events[train_start:train_end], market_type)
        test_result = _run_backtest_result(strategy_type, parameters, payload, feed_events[train_end:test_end], market_type)
        windows.append(
            {
                "index": len(windows) + 1,
                "train_start": train_start,
                "train_end": train_end,
                "test_start": train_end,
                "test_end": test_end,
                "train": _metrics_payload(train_result.metrics),
                "test": _metrics_payload(test_result.metrics),
                "test_fill_count": len(test_result.fills),
                "test_rejected_order_count": len(test_result.rejected_orders),
            }
        )
        start += step_events

    if not windows:
        return {
            "status": "skipped",
            "reason": "no_complete_windows",
            "event_count": len(feed_events),
            "train_events": train_events,
            "test_events": test_events,
            "step_events": step_events,
            "window_count": 0,
            "windows": [],
        }

    test_returns = [float(window["test"]["total_return"]) for window in windows]
    test_drawdowns = [float(window["test"]["max_drawdown"]) for window in windows]
    positive_windows = sum(1 for value in test_returns if value > 0)
    return {
        "status": "completed",
        "event_count": len(feed_events),
        "train_events": train_events,
        "test_events": test_events,
        "step_events": step_events,
        "window_count": len(windows),
        "average_test_return": sum(test_returns) / len(test_returns),
        "worst_test_return": min(test_returns),
        "best_test_return": max(test_returns),
        "worst_test_drawdown": max(test_drawdowns),
        "consistency_ratio": positive_windows / len(windows),
        "total_test_trades": sum(int(window["test"]["trade_count"]) for window in windows),
        "windows": windows,
    }


def _parameter_combinations(parameter_grid: dict[str, list[Any]], max_combinations: int) -> tuple[list[str], list[tuple[Any, ...]]]:
    if not parameter_grid:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="parameter_grid_required")

    keys: list[str] = []
    values: list[list[Any]] = []
    total = 1
    for key, choices in parameter_grid.items():
        if not isinstance(choices, list) or not choices:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"parameter_grid_empty:{key}")
        keys.append(key)
        values.append(choices)
        total *= len(choices)

    if total > max_combinations:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"parameter_grid_too_large:{total}>{max_combinations}",
        )

    return keys, list(product(*values))


def _trade_records_csv(records: list[dict[str, Any]]) -> str:
    output = io.StringIO()
    fieldnames = [
        "id",
        "run_id",
        "owner_email",
        "run_mode",
        "market_type",
        "symbol",
        "side",
        "quantity",
        "price",
        "notional",
        "fee",
        "reason",
        "traded_at",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(records)
    return output.getvalue()


def _side_from_action(action: str) -> Side:
    if action in {"buy", "open_long"}:
        return Side.BUY
    if action in {"sell", "close_long"}:
        return Side.SELL
    raise ValueError("unsupported_order_action")


def _assert_self_keeps_user_management(
    db: Database,
    actor: dict,
    target_user_id: int,
    payload: UpdateUserRequest,
) -> None:
    if actor["id"] != target_user_id:
        return
    if payload.status == "disabled":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="cannot_disable_self")
    current = db.get_user(target_user_id)
    if not current:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user_not_found")
    role = payload.role if payload.role is not None else current["role"]
    grants = payload.permission_grants if payload.permission_grants is not None else current["permission_grants"]
    revokes = payload.permission_revokes if payload.permission_revokes is not None else current["permission_revokes"]
    if MANAGE_USERS not in permissions_for_user(role, grants, revokes):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="cannot_remove_own_user_management_permission",
        )


def _resolve_strategy_request(db: Database, user: dict, payload: Any) -> tuple[str, dict[str, Any], dict | None]:
    if payload.strategy_config_id is not None:
        config = db.get_strategy_config(
            payload.strategy_config_id,
            user_id=user["id"],
            is_admin=has_user_permission(user, MANAGE_ALL_STRATEGIES),
        )
        if not config:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="strategy_config_not_found")
        return config["strategy_type"], config["parameters"], config

    parameters = dict(payload.strategy_parameters or {})
    if not parameters and payload.strategy_type == "statistical_event":
        parameters = {
            "lookback": getattr(payload, "lookback", 20),
            "entry_z": getattr(payload, "entry_z", -1.2),
            "exit_z": getattr(payload, "exit_z", 0.2),
            "base_weight": getattr(payload, "base_weight", 0.20),
            "event_weight": getattr(payload, "event_weight", 0.25),
            "event_threshold": getattr(payload, "event_threshold", 0.60),
        }

    try:
        return payload.strategy_type, validate_parameters(payload.strategy_type, parameters), None
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


def _now_datetime():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


def _audit_context(request: Request) -> dict[str, str]:
    forwarded_for = request.headers.get("x-forwarded-for", "")
    ip_address = forwarded_for.split(",")[0].strip() if forwarded_for else ""
    if not ip_address and request.client:
        ip_address = request.client.host
    return {
        "ip_address": ip_address,
        "user_agent": request.headers.get("user-agent", ""),
    }


def _model_dump(model: BaseModel) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump(exclude_none=True)
    return model.dict(exclude_none=True)


def _user_payload(user: dict | None) -> dict:
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="not_authenticated")
    return {
        "user": user,
        "permissions": sorted(permissions_for_user(user["role"], user["permission_grants"], user["permission_revokes"])),
        "permission_catalog": sorted(ALL_PERMISSIONS),
        "role_permissions": {role: sorted(perms) for role, perms in ROLE_PERMISSIONS.items()},
    }


def _bootstrap_admin(db: Database) -> None:
    if db.user_count() > 0:
        return
    email = os.environ.get("CRYPTO_LAB_ADMIN_EMAIL", "admin@example.com")
    password = os.environ.get("CRYPTO_LAB_ADMIN_PASSWORD", "change-me-now")
    db.create_user(email=email, password=password, role="admin", status="active")


def _safe_project_path(value: str | Path | None) -> Path:
    if value is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="path_required")
    candidate = (PROJECT_ROOT / value).resolve()
    if candidate != PROJECT_ROOT and PROJECT_ROOT not in candidate.parents:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="path_outside_project")
    if not candidate.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"file_not_found:{value}")
    return candidate


def _safe_data_output_path(value: str | Path) -> Path:
    raw = Path(value)
    candidate = (PROJECT_ROOT / raw).resolve()
    data_root = (PROJECT_ROOT / "data").resolve()
    if candidate != data_root and data_root not in candidate.parents:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="output_path_must_be_under_data")
    if candidate.suffix.lower() != ".csv":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="output_file_must_be_csv")
    return candidate


def _bar_payload(bar) -> dict:
    return {
        "timestamp": bar.timestamp.isoformat(),
        "symbol": bar.symbol,
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "volume": bar.volume,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="crypto-lab-web")
    parser.add_argument("--host", default=os.environ.get("CRYPTO_LAB_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("CRYPTO_LAB_PORT", "8092")))
    parser.add_argument("--db", default=os.environ.get("CRYPTO_LAB_DB", str(DEFAULT_DB_PATH)))
    args = parser.parse_args(argv)

    import uvicorn

    app = create_app(db_path=args.db)
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
