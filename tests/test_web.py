import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient

from crypto_strategy_lab.web import create_app


class WebTests(unittest.TestCase):
    def test_login_api_key_and_backtest_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(db_path=Path(tmp) / "web.sqlite3", secret_key="test-secret")
            client = TestClient(app)

            unauthenticated = client.get("/api/me")
            self.assertEqual(unauthenticated.status_code, 401)

            login = client.post(
                "/api/auth/login",
                json={"email": "admin@example.com", "password": "change-me-now"},
            )
            self.assertEqual(login.status_code, 200)
            self.assertEqual(login.json()["user"]["role"], "admin")

            bad_password_change = client.post(
                "/api/me/password",
                json={"current_password": "wrong", "new_password": "new-password-123"},
            )
            self.assertEqual(bad_password_change.status_code, 400)
            self.assertEqual(bad_password_change.json()["detail"], "invalid_current_password")

            password_change = client.post(
                "/api/me/password",
                json={"current_password": "change-me-now", "new_password": "new-password-123"},
            )
            self.assertEqual(password_change.status_code, 200)
            self.assertEqual(password_change.json()["user"]["email"], "admin@example.com")

            client.post("/api/auth/logout")
            old_login = client.post(
                "/api/auth/login",
                json={"email": "admin@example.com", "password": "change-me-now"},
            )
            self.assertEqual(old_login.status_code, 401)
            login = client.post(
                "/api/auth/login",
                json={"email": "admin@example.com", "password": "new-password-123"},
            )
            self.assertEqual(login.status_code, 200)

            created_key = client.post(
                "/api/api-keys",
                json={
                    "exchange": "binance",
                    "label": "test",
                    "api_key": "abcd1234efgh5678",
                    "api_secret": "secret",
                    "scopes": ["read"],
                    "can_trade": False,
                },
            )
            self.assertEqual(created_key.status_code, 200)
            self.assertEqual(created_key.json()["api_key_masked"], "abcd...5678")

            keys = client.get("/api/api-keys?all=1")
            self.assertEqual(keys.status_code, 200)
            self.assertEqual(len(keys.json()), 1)

            catalog = client.get("/api/strategies/catalog")
            self.assertEqual(catalog.status_code, 200)
            self.assertGreaterEqual(len(catalog.json()), 3)

            data_sources = client.get("/api/data-sources/catalog")
            self.assertEqual(data_sources.status_code, 200)
            self.assertIn("binance_spot", {source["id"] for source in data_sources.json()})

            indicator_catalog = client.get("/api/analysis/indicator-catalog")
            self.assertEqual(indicator_catalog.status_code, 200)
            self.assertIn("RSI", {item["name"] for item in indicator_catalog.json()})

            indicators = client.post(
                "/api/analysis/indicators",
                json={"bars_file": "data/sample_bars.csv", "symbol": "BTCUSDT", "limit": 10},
            )
            self.assertEqual(indicators.status_code, 200)
            self.assertGreaterEqual(indicators.json()["results"][0]["bar_count"], 1)
            self.assertIn("rsi", indicators.json()["results"][0]["latest"])

            watchlist = client.post(
                "/api/watchlists",
                json={
                    "name": "majors",
                    "symbols": ["BTCUSDT", "ETHUSDT", "BTCUSDT"],
                    "notes": "test universe",
                    "is_default": True,
                },
            )
            self.assertEqual(watchlist.status_code, 200)
            self.assertEqual(watchlist.json()["symbols"], ["BTCUSDT", "ETHUSDT"])
            watchlists = client.get("/api/watchlists?all=1")
            self.assertEqual(watchlists.status_code, 200)
            self.assertGreaterEqual(len(watchlists.json()), 1)
            patched_watchlist = client.patch(
                f"/api/watchlists/{watchlist.json()['id']}",
                json={"symbols": ["SOLUSDT"]},
            )
            self.assertEqual(patched_watchlist.status_code, 200)
            self.assertEqual(patched_watchlist.json()["symbols"], ["SOLUSDT"])

            bad_data_fetch = client.post(
                "/api/data-sources/fetch",
                json={
                    "provider": "binance_spot",
                    "symbol": "BTCUSDT",
                    "interval": "1h",
                    "output_file": "../bad.csv",
                },
            )
            self.assertEqual(bad_data_fetch.status_code, 400)
            self.assertEqual(bad_data_fetch.json()["detail"], "output_path_must_be_under_data")

            custom_user = client.post(
                "/api/users",
                json={
                    "email": "custom@example.com",
                    "password": "custom-pass-123",
                    "role": "viewer",
                    "permission_grants": ["backtests:run"],
                    "permission_revokes": ["monitoring:view"],
                },
            )
            self.assertEqual(custom_user.status_code, 200)
            self.assertIn("backtests:run", custom_user.json()["permissions"])
            self.assertNotIn("monitoring:view", custom_user.json()["permissions"])

            self_revoke = client.patch(
                "/api/users/1",
                json={"permission_revokes": ["users:manage"]},
            )
            self.assertEqual(self_revoke.status_code, 400)
            self.assertEqual(self_revoke.json()["detail"], "cannot_remove_own_user_management_permission")

            client.post("/api/auth/logout")
            custom_login = client.post(
                "/api/auth/login",
                json={"email": "custom@example.com", "password": "custom-pass-123"},
            )
            self.assertEqual(custom_login.status_code, 200)
            self.assertIn("backtests:run", custom_login.json()["permissions"])
            custom_users = client.get("/api/users")
            self.assertEqual(custom_users.status_code, 403)
            custom_monitoring = client.get("/api/monitoring/summary")
            self.assertEqual(custom_monitoring.status_code, 403)
            custom_grid = client.post(
                "/api/backtests/grid-search",
                json={
                    "cash": 10_000,
                    "strategy_type": "fixed_weight",
                    "strategy_parameters": {"target_weight": 0.2},
                    "parameter_grid": {"target_weight": [0.1, 0.2]},
                },
            )
            self.assertEqual(custom_grid.status_code, 200)
            self.assertEqual(custom_grid.json()["combination_count"], 2)

            client.post("/api/auth/logout")
            login = client.post(
                "/api/auth/login",
                json={"email": "admin@example.com", "password": "new-password-123"},
            )
            self.assertEqual(login.status_code, 200)

            strategy_config = client.post(
                "/api/strategy-configs",
                json={
                    "name": "browser event strategy",
                    "strategy_type": "event_score",
                    "market_type": "spot",
                    "parameters": {
                        "positive_threshold": 0.6,
                        "negative_threshold": -0.6,
                        "target_weight": 0.2,
                        "event_ttl_bars": 4,
                    },
                    "is_default": True,
                },
            )
            self.assertEqual(strategy_config.status_code, 200)
            self.assertTrue(strategy_config.json()["is_default"])

            configs = client.get("/api/strategy-configs?all=1")
            self.assertEqual(configs.status_code, 200)
            self.assertEqual(len(configs.json()), 1)

            snapshot = client.get("/api/realtime/snapshot?symbol=ETHUSDT")
            self.assertEqual(snapshot.status_code, 200)
            self.assertEqual(snapshot.json()["symbol"], "ETHUSDT")
            self.assertIn("bids", snapshot.json()["order_book"])

            signal = client.post(
                "/api/realtime/signals/evaluate",
                json={"symbol": "ETHUSDT", "strategy_config_id": strategy_config.json()["id"]},
            )
            self.assertEqual(signal.status_code, 200)
            self.assertIn(signal.json()["signal"], {"buy", "sell", "hold"})
            signal_logs = client.get("/api/realtime/signals?all=1")
            self.assertEqual(signal_logs.status_code, 200)
            self.assertGreaterEqual(len(signal_logs.json()), 1)

            backtest = client.post(
                "/api/backtests/run",
                json={
                    "cash": 10_000,
                    "strategy_config_id": strategy_config.json()["id"],
                    "fee_bps": 5.0,
                    "slippage_bps": 1.0,
                    "out_of_sample_ratio": 0.3,
                },
            )
            self.assertEqual(backtest.status_code, 200)
            self.assertGreater(backtest.json()["metrics"]["ending_equity"], 0)
            self.assertIn("total_funding", backtest.json()["metrics"])
            self.assertIn("liquidation_count", backtest.json()["metrics"])
            self.assertEqual(backtest.json()["validation"]["status"], "completed")
            self.assertEqual(backtest.json()["strategy"]["config"]["name"], "browser event strategy")
            self.assertGreater(backtest.json()["run"]["id"], 0)
            run_id = backtest.json()["run"]["id"]

            grid_search = client.post(
                "/api/backtests/grid-search",
                json={
                    "cash": 10_000,
                    "strategy_config_id": strategy_config.json()["id"],
                    "parameter_grid": {"target_weight": [0.1, 0.2]},
                    "max_combinations": 10,
                },
            )
            self.assertEqual(grid_search.status_code, 200)
            self.assertEqual(grid_search.json()["combination_count"], 2)
            self.assertEqual(grid_search.json()["results"][0]["rank"], 1)

            run_detail = client.get(f"/api/trade-runs/{run_id}")
            self.assertEqual(run_detail.status_code, 200)
            self.assertEqual(run_detail.json()["id"], run_id)
            self.assertIn("validation", run_detail.json()["metrics"])

            run_records = client.get(f"/api/trade-runs/{run_id}/records")
            self.assertEqual(run_records.status_code, 200)
            self.assertGreaterEqual(len(run_records.json()), 1)

            json_export = client.get(f"/api/trade-runs/{run_id}/export?format=json")
            self.assertEqual(json_export.status_code, 200)
            self.assertEqual(json_export.json()["run"]["id"], run_id)
            csv_export = client.get(f"/api/trade-runs/{run_id}/export?format=csv")
            self.assertEqual(csv_export.status_code, 200)
            self.assertIn("symbol,side,quantity", csv_export.text)

            summary = client.get("/api/monitoring/summary?all=1")
            self.assertEqual(summary.status_code, 200)
            self.assertEqual(summary.json()["total_runs"], 1)
            self.assertGreaterEqual(summary.json()["total_trades"], 1)

            runs = client.get("/api/trade-runs?all=1")
            self.assertEqual(runs.status_code, 200)
            self.assertEqual(len(runs.json()), 1)
            backtest_runs = client.get("/api/trade-runs?all=1&run_mode=backtest")
            self.assertEqual(backtest_runs.status_code, 200)
            self.assertEqual(len(backtest_runs.json()), 1)

            trades = client.get("/api/trade-records?all=1")
            self.assertEqual(trades.status_code, 200)
            self.assertGreaterEqual(len(trades.json()), 1)

            paper_account = client.get("/api/paper-trading/account")
            self.assertEqual(paper_account.status_code, 200)
            self.assertEqual(paper_account.json()["cash"], 10_000.0)

            risk_settings = client.get("/api/risk-settings")
            self.assertEqual(risk_settings.status_code, 200)
            self.assertTrue(risk_settings.json()["paper_trading_enabled"])
            self.assertIn("max_open_positions", risk_settings.json())
            self.assertIn("require_order_confirmation", risk_settings.json())
            patched_risk = client.patch(
                "/api/risk-settings",
                json={"paper_trading_enabled": False, "max_order_notional": 50},
            )
            self.assertEqual(patched_risk.status_code, 200)
            self.assertFalse(patched_risk.json()["paper_trading_enabled"])

            paused_preflight = client.post(
                "/api/trading/preflight",
                json={
                    "symbol": "BTCUSDT",
                    "action": "open_long",
                    "quantity": 1.0,
                    "price": 100.0,
                    "fee_bps": 0.0,
                    "slippage_bps": 0.0,
                    "run_mode": "paper",
                },
            )
            self.assertEqual(paused_preflight.status_code, 200)
            self.assertFalse(paused_preflight.json()["approved"])
            self.assertEqual(paused_preflight.json()["reason"], "paper_trading_paused_by_risk_settings")

            paused_order = client.post(
                "/api/paper-trading/order",
                json={
                    "symbol": "BTCUSDT",
                    "action": "open_long",
                    "quantity": 1.0,
                    "price": 100.0,
                    "fee_bps": 0.0,
                    "slippage_bps": 0.0,
                },
            )
            self.assertEqual(paused_order.status_code, 400)
            self.assertEqual(paused_order.json()["detail"], "paper_trading_paused_by_risk_settings")
            rejected_orders = client.get("/api/orders?all=1")
            self.assertEqual(rejected_orders.status_code, 200)
            self.assertEqual(rejected_orders.json()[0]["status"], "preflight_rejected")

            client.patch("/api/risk-settings", json={"paper_trading_enabled": True, "max_order_notional": 5000})

            symbol_rule = client.post(
                "/api/symbol-rules",
                json={
                    "exchange": "paper",
                    "market_type": "spot",
                    "symbol": "RULEUSDT",
                    "min_quantity": 0.5,
                    "quantity_step": 0.5,
                    "price_tick": 0.05,
                    "min_notional": 100.0,
                    "max_notional": 1000.0,
                    "max_leverage": 2.0,
                    "source": "test",
                },
            )
            self.assertEqual(symbol_rule.status_code, 200)
            self.assertEqual(symbol_rule.json()["symbol"], "RULEUSDT")
            symbol_rules = client.get("/api/symbol-rules?exchange=paper&market_type=spot&symbol=RULEUSDT")
            self.assertEqual(symbol_rules.status_code, 200)
            self.assertEqual(len(symbol_rules.json()), 1)

            rule_quantity_preflight = client.post(
                "/api/trading/preflight",
                json={
                    "symbol": "RULEUSDT",
                    "action": "open_long",
                    "quantity": 0.25,
                    "price": 100.0,
                    "fee_bps": 0.0,
                    "slippage_bps": 0.0,
                    "run_mode": "paper",
                },
            )
            self.assertEqual(rule_quantity_preflight.status_code, 200)
            self.assertFalse(rule_quantity_preflight.json()["approved"])
            self.assertEqual(rule_quantity_preflight.json()["reason"], "symbol_min_quantity_not_met")
            self.assertEqual(rule_quantity_preflight.json()["symbol_rule"]["symbol"], "RULEUSDT")

            rule_step_preflight = client.post(
                "/api/trading/preflight",
                json={
                    "symbol": "RULEUSDT",
                    "action": "open_long",
                    "quantity": 0.75,
                    "price": 200.0,
                    "fee_bps": 0.0,
                    "slippage_bps": 0.0,
                    "run_mode": "paper",
                },
            )
            self.assertEqual(rule_step_preflight.status_code, 200)
            self.assertFalse(rule_step_preflight.json()["approved"])
            self.assertEqual(rule_step_preflight.json()["reason"], "symbol_quantity_step_mismatch")

            rule_tick_preflight = client.post(
                "/api/trading/preflight",
                json={
                    "symbol": "RULEUSDT",
                    "action": "open_long",
                    "quantity": 1.0,
                    "price": 100.0,
                    "order_type": "limit",
                    "time_in_force": "GTC",
                    "limit_price": 100.03,
                    "fee_bps": 0.0,
                    "slippage_bps": 0.0,
                    "run_mode": "paper",
                },
            )
            self.assertEqual(rule_tick_preflight.status_code, 200)
            self.assertFalse(rule_tick_preflight.json()["approved"])
            self.assertEqual(rule_tick_preflight.json()["reason"], "symbol_price_tick_mismatch")

            approved_preflight = client.post(
                "/api/trading/preflight",
                json={
                    "symbol": "BTCUSDT",
                    "action": "open_long",
                    "quantity": 1.0,
                    "price": 100.0,
                    "fee_bps": 0.0,
                    "slippage_bps": 0.0,
                    "run_mode": "paper",
                },
            )
            self.assertEqual(approved_preflight.status_code, 200)
            self.assertTrue(approved_preflight.json()["approved"])
            self.assertEqual(approved_preflight.json()["reason"], "approved")
            self.assertIn("estimated_fee", approved_preflight.json())
            self.assertIn("initial_margin_required", approved_preflight.json())
            self.assertIn("estimated_funding_8h", approved_preflight.json())

            client.patch("/api/risk-settings", json={"max_symbol_weight": 0.001, "max_gross_exposure": 0.80})
            exposure_preflight = client.post(
                "/api/trading/preflight",
                json={
                    "symbol": "BTCUSDT",
                    "action": "open_long",
                    "quantity": 1.0,
                    "price": 100.0,
                    "fee_bps": 0.0,
                    "slippage_bps": 0.0,
                    "run_mode": "paper",
                },
            )
            self.assertEqual(exposure_preflight.status_code, 200)
            self.assertFalse(exposure_preflight.json()["approved"])
            self.assertEqual(exposure_preflight.json()["reason"], "max_symbol_weight_exceeded")
            self.assertIn("symbol_exposure_after", exposure_preflight.json())
            client.patch("/api/risk-settings", json={"max_symbol_weight": 0.35, "max_gross_exposure": 0.80})

            client.patch("/api/risk-settings", json={"require_order_confirmation": True})
            confirmation_preflight = client.post(
                "/api/trading/preflight",
                json={
                    "symbol": "ETHUSDT",
                    "action": "open_long",
                    "quantity": 1.0,
                    "price": 100.0,
                    "run_mode": "paper",
                },
            )
            self.assertEqual(confirmation_preflight.status_code, 200)
            self.assertFalse(confirmation_preflight.json()["approved"])
            self.assertEqual(confirmation_preflight.json()["reason"], "order_confirmation_required")
            client.patch("/api/risk-settings", json={"require_order_confirmation": False})

            paper_order = client.post(
                "/api/paper-trading/order",
                json={
                    "symbol": "BTCUSDT",
                    "action": "open_long",
                    "quantity": 1.0,
                    "price": 100.0,
                    "fee_bps": 0.0,
                    "slippage_bps": 0.0,
                },
            )
            self.assertEqual(paper_order.status_code, 200)
            self.assertEqual(paper_order.json()["position"]["quantity"], 1.0)
            self.assertEqual(paper_order.json()["order"]["status"], "filled")
            self.assertEqual(paper_order.json()["order"]["filled_quantity"], 1.0)
            execution_events = client.get("/api/execution-events?all=1")
            self.assertEqual(execution_events.status_code, 200)
            self.assertIn("order_filled", {event["event_type"] for event in execution_events.json()})

            notification_channel = client.post(
                "/api/notification-channels",
                json={
                    "channel_type": "webhook",
                    "label": "test hook",
                    "destination": "https://example.invalid/hook",
                    "is_active": True,
                },
            )
            self.assertEqual(notification_channel.status_code, 200)
            self.assertEqual(notification_channel.json()["channel_type"], "webhook")

            cancel_filled = client.post(f"/api/orders/{paper_order.json()['order']['id']}/cancel")
            self.assertEqual(cancel_filled.status_code, 400)
            self.assertEqual(cancel_filled.json()["detail"], "order_not_cancelable")

            resting_limit = client.post(
                "/api/paper-trading/order",
                json={
                    "symbol": "ETHUSDT",
                    "action": "open_long",
                    "quantity": 1.0,
                    "price": 100.0,
                    "order_type": "limit",
                    "time_in_force": "GTC",
                    "limit_price": 90.0,
                    "post_only": True,
                    "confirm_order": True,
                },
            )
            self.assertEqual(resting_limit.status_code, 200)
            self.assertEqual(resting_limit.json()["order"]["status"], "submitted")
            self.assertIsNone(resting_limit.json()["trade"])

            post_only_reject = client.post(
                "/api/paper-trading/order",
                json={
                    "symbol": "SOLUSDT",
                    "action": "open_long",
                    "quantity": 1.0,
                    "price": 100.0,
                    "order_type": "limit",
                    "time_in_force": "GTC",
                    "limit_price": 100.0,
                    "post_only": True,
                },
            )
            self.assertEqual(post_only_reject.status_code, 200)
            self.assertEqual(post_only_reject.json()["order"]["status"], "rejected")
            self.assertEqual(post_only_reject.json()["order"]["error_message"], "post_only_would_take_liquidity")
            paper_runs = client.get("/api/trade-runs?all=1&run_mode=paper")
            self.assertEqual(paper_runs.status_code, 200)
            self.assertEqual(len(paper_runs.json()), 1)

            paper_close = client.post(
                "/api/paper-trading/order",
                json={
                    "symbol": "BTCUSDT",
                    "action": "close_long",
                    "quantity": 0.5,
                    "price": 110.0,
                    "fee_bps": 0.0,
                    "slippage_bps": 0.0,
                },
            )
            self.assertEqual(paper_close.status_code, 200)
            self.assertEqual(paper_close.json()["position"]["quantity"], 0.5)
            notifications = client.get("/api/notifications?all=1")
            self.assertEqual(notifications.status_code, 200)
            self.assertIn("trade_filled", {item["event_type"] for item in notifications.json()})
            self.assertIn("queued", {item["status"] for item in notifications.json()})

            paper_positions = client.get("/api/paper-trading/positions?all=1")
            self.assertEqual(paper_positions.status_code, 200)
            self.assertEqual(len(paper_positions.json()), 1)
            paper_orders = client.get("/api/orders?all=1&run_mode=paper")
            self.assertEqual(paper_orders.status_code, 200)
            self.assertIn("filled", {order["status"] for order in paper_orders.json()})
            self.assertIn("preflight_rejected", {order["status"] for order in paper_orders.json()})
            audit_logs = client.get("/api/audit-logs?all=1")
            self.assertEqual(audit_logs.status_code, 200)
            audit_actions = {item["action"] for item in audit_logs.json()}
            self.assertIn("risk.update", audit_actions)
            self.assertIn("trading.preflight", audit_actions)
            self.assertIn("trading.paper_order", audit_actions)
            sync_run = client.post("/api/exchange-sync/run", json={"exchange": "paper", "run_mode": "paper"})
            self.assertEqual(sync_run.status_code, 200)
            self.assertEqual(sync_run.json()["status"], "matched")
            sync_runs = client.get("/api/exchange-sync/runs?all=1")
            self.assertEqual(sync_runs.status_code, 200)
            self.assertGreaterEqual(len(sync_runs.json()), 1)

            automation_start = client.post("/api/automation/control", json={"action": "start", "run_mode": "paper"})
            self.assertEqual(automation_start.status_code, 200)
            self.assertEqual(automation_start.json()["status"], "running")
            automation_status = client.get("/api/automation/status?all=1")
            self.assertEqual(automation_status.status_code, 200)
            self.assertEqual(automation_status.json()["emergency_stop"]["value"], "0")
            self.assertGreaterEqual(len(automation_status.json()["controls"]), 1)

            automation_run_once = client.post(
                "/api/automation/run-once",
                json={
                    "symbol": "BTCUSDT",
                    "run_mode": "paper",
                    "order_notional": 100,
                    "parameters": {"momentum_threshold_bps": 999999, "imbalance_threshold": 1},
                },
            )
            self.assertEqual(automation_run_once.status_code, 200)
            self.assertEqual(automation_run_once.json()["status"], "no_order")
            self.assertEqual(automation_run_once.json()["reason"], "signal_hold")

            emergency_stop = client.post("/api/automation/emergency-stop")
            self.assertEqual(emergency_stop.status_code, 200)
            stopped_preflight = client.post(
                "/api/trading/preflight",
                json={
                    "symbol": "BTCUSDT",
                    "action": "open_long",
                    "quantity": 1.0,
                    "price": 100.0,
                    "run_mode": "paper",
                },
            )
            self.assertEqual(stopped_preflight.status_code, 200)
            self.assertFalse(stopped_preflight.json()["approved"])
            self.assertEqual(stopped_preflight.json()["reason"], "global_emergency_stop_active")
            clear_stop = client.post("/api/automation/clear-emergency-stop")
            self.assertEqual(clear_stop.status_code, 200)

            close_all = client.post("/api/automation/close-all", json={"run_mode": "paper", "reason": "test_close_all"})
            self.assertEqual(close_all.status_code, 200)
            self.assertEqual(close_all.json()["closed_count"], 1)

            notification_dispatch = client.post("/api/notifications/dispatch", json={"limit": 50, "dry_run": True})
            self.assertEqual(notification_dispatch.status_code, 200)
            self.assertGreaterEqual(notification_dispatch.json()["dispatched_count"], 1)
            dispatched_notifications = client.get("/api/notifications?all=1")
            self.assertEqual(dispatched_notifications.status_code, 200)
            self.assertIn("dry_run", {item["status"] for item in dispatched_notifications.json()})

            auto_execute = client.post(
                "/api/paper-trading/auto-execute",
                json={"strategy_config_id": strategy_config.json()["id"]},
            )
            self.assertEqual(auto_execute.status_code, 200)
            self.assertGreaterEqual(auto_execute.json()["trade_count"], 1)
            self.assertEqual(auto_execute.json()["trade_count"], len(auto_execute.json()["orders"]))

            live_order = client.post(
                "/api/live-trading/order",
                json={"symbol": "BTCUSDT", "action": "open_long", "quantity": 1.0, "price": 100.0},
            )
            self.assertEqual(live_order.status_code, 403)
            self.assertEqual(live_order.json()["detail"], "live_trading_disabled")


if __name__ == "__main__":
    unittest.main()
