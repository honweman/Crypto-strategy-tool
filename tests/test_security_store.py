import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from crypto_strategy_lab.security import SecretBox, hash_password, mask_secret, verify_password
from crypto_strategy_lab.store import Database


class SecurityStoreTests(unittest.TestCase):
    def test_password_hash_and_verify(self) -> None:
        password_hash = hash_password("correct horse battery staple")
        self.assertTrue(verify_password("correct horse battery staple", password_hash))
        self.assertFalse(verify_password("wrong", password_hash))

    def test_secret_box_round_trip(self) -> None:
        box = SecretBox("test-secret")
        encrypted = box.encrypt("abc123")
        self.assertNotIn("abc123", encrypted)
        self.assertEqual(box.decrypt(encrypted), "abc123")

    def test_user_session_and_api_key_storage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "crypto_lab.sqlite3", SecretBox("test-secret"))
            db.init_schema()
            user = db.create_user("Trader@Example.com", "password", "trader")
            token = db.create_session(user["id"])

            session_user = db.get_session_user(token)
            self.assertIsNotNone(session_user)
            self.assertEqual(session_user["email"], "trader@example.com")
            self.assertIn("trading:execute_paper", session_user["permissions"])

            updated_user = db.update_user(
                user["id"],
                permission_grants=["api_keys:manage_all"],
                permission_revokes=["trading:execute_paper"],
            )
            self.assertIn("api_keys:manage_all", updated_user["permissions"])
            self.assertNotIn("trading:execute_paper", updated_user["permissions"])
            session_user = db.get_session_user(token)
            self.assertIn("api_keys:manage_all", session_user["permissions"])
            self.assertNotIn("trading:execute_paper", session_user["permissions"])

            api_key = db.create_api_key(
                user_id=user["id"],
                exchange="binance",
                label="readonly",
                api_key="abcd1234efgh5678",
                api_secret="super-secret",
                scopes=["read"],
                can_trade=False,
            )
            self.assertEqual(api_key["api_key_masked"], mask_secret("abcd1234efgh5678"))
            self.assertFalse(api_key["can_trade"])
            self.assertEqual(len(db.list_api_keys(user["id"])), 1)
            self.assertTrue(db.delete_api_key(api_key["id"], user_id=user["id"]))
            self.assertEqual(db.list_api_keys(user["id"]), [])

            config = db.create_strategy_config(
                user_id=user["id"],
                name="event config",
                strategy_type="event_score",
                market_type="spot",
                parameters={"positive_threshold": 0.7, "negative_threshold": -0.7, "target_weight": 0.15},
                is_default=True,
            )
            self.assertEqual(config["name"], "event config")
            self.assertTrue(config["is_default"])
            self.assertEqual(config["current_version"], 1)
            self.assertEqual(db.list_strategy_configs(user["id"])[0]["strategy_type"], "event_score")
            versions = db.list_strategy_config_versions(config["id"], user_id=user["id"])
            self.assertEqual(len(versions), 1)
            self.assertEqual(versions[0]["version"], 1)

            updated = db.update_strategy_config(
                config["id"],
                user_id=user["id"],
                parameters={"positive_threshold": 0.65, "negative_threshold": -0.65, "target_weight": 0.2},
            )
            self.assertEqual(updated["parameters"]["target_weight"], 0.2)
            self.assertEqual(updated["current_version"], 2)
            versions = db.list_strategy_config_versions(config["id"], user_id=user["id"])
            self.assertEqual([item["version"] for item in versions], [2, 1])
            self.assertTrue(db.delete_strategy_config(config["id"], user_id=user["id"]))
            self.assertEqual(db.list_strategy_configs(user["id"]), [])

            run = db.create_trade_run(
                user_id=user["id"],
                strategy_type="fixed_weight",
                parameters={"target_weight": 0.2},
                metrics={"ending_equity": 10100, "total_return": 0.01, "trade_count": 1},
                bars_file="data/sample_bars.csv",
                strategy_version=2,
            )
            self.assertEqual(run["strategy_version"], 2)
            records = db.create_trade_records(
                run_id=run["id"],
                user_id=user["id"],
                trades=[
                    {
                        "symbol": "BTCUSDT",
                        "side": "buy",
                        "quantity": 0.1,
                        "price": 100.0,
                        "fee": 0.01,
                        "reason": "test",
                        "traded_at": "2026-01-01T00:00:00+00:00",
                    }
                ],
            )
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["notional"], 10.0)
            self.assertEqual(len(db.list_trade_runs(user["id"])), 1)
            self.assertEqual(len(db.list_trade_records(user["id"])), 1)
            trade_order = db.create_trade_order(
                user_id=user["id"],
                client_order_id="client-1",
                symbol="BTCUSDT",
                side="buy",
                quantity=0.1,
                requested_price=100.0,
                order_type="limit",
                time_in_force="GTC",
                post_only=True,
                limit_price=99.0,
                leverage=2.0,
                run_id=run["id"],
                reason="test_order",
            )
            self.assertEqual(trade_order["status"], "created")
            self.assertEqual(trade_order["order_type"], "limit")
            self.assertTrue(trade_order["post_only"])
            self.assertEqual(trade_order["time_in_force"], "GTC")
            filled_order = db.update_trade_order(
                trade_order["id"],
                status="filled",
                exchange_order_id="paper-client-1",
                filled_quantity=0.1,
                average_fill_price=100.0,
                raw_response={"ok": True},
                submitted=True,
                finished=True,
            )
            self.assertEqual(filled_order["status"], "filled")
            self.assertEqual(filled_order["raw_response"], {"ok": True})
            self.assertEqual(len(db.list_trade_orders(user["id"])), 1)
            audit_log = db.create_audit_log(
                user_id=user["id"],
                action="trading.paper_order",
                entity_type="trade_order",
                entity_id=trade_order["id"],
                status="filled",
                detail={"symbol": "BTCUSDT"},
                ip_address="127.0.0.1",
            )
            self.assertEqual(audit_log["detail"], {"symbol": "BTCUSDT"})
            self.assertEqual(db.list_audit_logs(user["id"])[0]["action"], "trading.paper_order")
            sync_run = db.create_exchange_sync_run(
                user_id=user["id"],
                exchange="paper",
                run_mode="paper",
                status="matched",
                local_snapshot={"cash": 100},
                exchange_snapshot={"cash": 100},
                differences=[],
            )
            self.assertEqual(sync_run["status"], "matched")
            self.assertEqual(db.list_exchange_sync_runs(user["id"])[0]["exchange"], "paper")

            symbol_rule = db.upsert_symbol_rule(
                exchange="paper",
                market_type="spot",
                symbol="ruleusdt",
                min_quantity=0.5,
                quantity_step=0.1,
                price_tick=0.01,
                min_notional=10.0,
                max_notional=500.0,
                max_leverage=2.0,
                source="unit-test",
            )
            self.assertEqual(symbol_rule["symbol"], "RULEUSDT")
            self.assertTrue(symbol_rule["is_active"])
            self.assertEqual(symbol_rule["quantity_step"], 0.1)
            self.assertEqual(db.get_symbol_rule("RULEUSDT")["min_notional"], 10.0)
            listed_rules = db.list_symbol_rules(exchange="paper", market_type="spot", symbol="RULEUSDT")
            self.assertEqual(len(listed_rules), 1)
            disabled_rule = db.upsert_symbol_rule(
                exchange="paper",
                market_type="spot",
                symbol="RULEUSDT",
                is_active=False,
            )
            self.assertFalse(disabled_rule["is_active"])
            self.assertIsNone(db.get_symbol_rule("RULEUSDT"))

            market_snapshot = db.create_realtime_market_snapshot(
                symbol="BTCUSDT",
                best_bid=99.0,
                best_ask=101.0,
                last_price=100.0,
                order_book={"bids": [{"price": 99.0, "quantity": 1.0}], "asks": [{"price": 101.0, "quantity": 1.0}]},
                recent_trades=[{"price": 100.0, "quantity": 0.1, "side": "buy"}],
                kline={"open": 98.0, "close": 100.0},
            )
            self.assertEqual(market_snapshot["symbol"], "BTCUSDT")
            self.assertEqual(len(db.list_realtime_market_snapshots("BTCUSDT")), 1)

            signal_log = db.create_signal_log(
                user_id=user["id"],
                strategy_type="manual_signal",
                symbol="BTCUSDT",
                signal="buy",
                score=1.2,
                parameters={"threshold": 1},
                market_snapshot=market_snapshot,
                reason="test signal",
            )
            self.assertEqual(signal_log["signal"], "buy")
            self.assertEqual(db.list_signal_logs(user["id"])[0]["reason"], "test signal")

            control = db.upsert_automation_control(
                user_id=user["id"],
                strategy_config_id=None,
                run_mode="paper",
                status="running",
                trading_enabled=True,
                last_action="start",
            )
            self.assertTrue(control["trading_enabled"])
            updated_control = db.upsert_automation_control(
                user_id=user["id"],
                strategy_config_id=None,
                run_mode="paper",
                status="paused",
                trading_enabled=False,
                last_action="pause",
            )
            self.assertEqual(updated_control["id"], control["id"])
            self.assertEqual(updated_control["status"], "paused")

            flag = db.set_system_flag("emergency_stop", "1", updated_by=user["id"])
            self.assertEqual(flag["value"], "1")
            self.assertEqual(db.get_system_flag("emergency_stop")["value"], "1")

            execution_event = db.create_execution_event(
                user_id=user["id"],
                order_id=trade_order["id"],
                run_id=run["id"],
                event_type="order_filled",
                status="filled",
                risk_checks=[{"name": "paper_cash", "passed": True}],
                raw_response={"ok": True},
            )
            self.assertEqual(execution_event["risk_checks"][0]["name"], "paper_cash")
            self.assertEqual(db.list_execution_events(user["id"])[0]["event_type"], "order_filled")

            notification_channel = db.create_notification_channel(
                user_id=user["id"],
                channel_type="webhook",
                label="hook",
                destination="https://example.invalid/hook",
            )
            self.assertTrue(notification_channel["is_active"])
            notification = db.create_notification(
                user_id=user["id"],
                channel_type="webhook",
                event_type="trade_filled",
                title="filled",
                message="order filled",
                payload={"order_id": trade_order["id"]},
                status="queued",
            )
            self.assertEqual(notification["payload"]["order_id"], trade_order["id"])
            self.assertEqual(db.list_notifications(user["id"])[0]["status"], "queued")
            updated_notification = db.update_notification_status(
                notification["id"],
                user_id=user["id"],
                status="dry_run",
                payload={**notification["payload"], "dispatch_attempts": [{"dry_run": True}]},
                delivered_at="2026-01-01T00:00:00+00:00",
            )
            self.assertEqual(updated_notification["status"], "dry_run")
            self.assertEqual(updated_notification["payload"]["dispatch_attempts"][0]["dry_run"], True)

            summary = db.monitoring_summary(user["id"])
            self.assertEqual(summary["total_runs"], 1)
            self.assertEqual(summary["total_trades"], 1)
            self.assertEqual(summary["total_notional"], 10.0)

            watchlist = db.create_watchlist(
                user_id=user["id"],
                name="majors",
                symbols=["btcusdt", "ethusdt", "BTCUSDT"],
                notes="test universe",
                is_default=True,
            )
            self.assertEqual(watchlist["symbols"], ["BTCUSDT", "ETHUSDT"])
            watchlists = db.list_watchlists(user["id"])
            self.assertEqual(len(watchlists), 1)
            updated_watchlist = db.update_watchlist(watchlist["id"], user_id=user["id"], symbols=["SOLUSDT"])
            self.assertEqual(updated_watchlist["symbols"], ["SOLUSDT"])
            self.assertTrue(db.delete_watchlist(watchlist["id"], user_id=user["id"]))
            self.assertEqual(db.list_watchlists(user["id"]), [])

            account = db.get_or_create_paper_account(user["id"])
            self.assertEqual(account["cash"], 10_000.0)
            buy_state = db.apply_paper_fill(
                user_id=user["id"],
                symbol="BTCUSDT",
                side="buy",
                quantity=1.0,
                price=100.0,
                fee=1.0,
            )
            self.assertEqual(buy_state["position"]["quantity"], 1.0)
            self.assertEqual(buy_state["account"]["cash"], 9_899.0)
            sell_state = db.apply_paper_fill(
                user_id=user["id"],
                symbol="BTCUSDT",
                side="sell",
                quantity=0.4,
                price=110.0,
                fee=0.5,
            )
            self.assertAlmostEqual(sell_state["position"]["quantity"], 0.6)
            self.assertAlmostEqual(sell_state["position"]["realized_pnl"], 3.5)
            self.assertEqual(len(db.list_paper_positions(user["id"])), 1)


if __name__ == "__main__":
    unittest.main()
