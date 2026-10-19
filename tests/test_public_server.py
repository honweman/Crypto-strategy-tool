import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from crypto_strategy_lab.public_server import load_config, secure_app, serve
from crypto_strategy_lab.security import SecretBox
from crypto_strategy_lab.store import Database


class PublicServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.environment = patch.dict(os.environ)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        root = Path(self.temp.name)
        self.config = {"db_path": str(root / "db.sqlite3"), "secret_key": "test-only-" + "a" * 50}
        self.db = Database(Path(self.config["db_path"]), SecretBox(self.config["secret_key"]))
        self.db.init_schema()
        self.user = self.db.create_user("owner@example.com", "random-test-password", "admin")
        self.path = root / "public.json"
        self.path.write_text(json.dumps(self.config))
        self.path.chmod(0o600)

    def test_config_rejects_permissions_default_password_and_wrong_key(self):
        self.assertEqual(load_config(self.path)["secret_key"], self.config["secret_key"])
        self.path.chmod(0o644)
        with self.assertRaises(ValueError):
            load_config(self.path)
        self.path.chmod(0o600)
        self.db.update_user_password(self.user["id"], "change-me-now")
        with self.assertRaises(ValueError):
            load_config(self.path)
        self.db.update_user_password(self.user["id"], "random-test-password")
        self.db.create_api_key(self.user["id"], "binance", "test", "key", "secret")
        self.config["secret_key"] = "b" * 50
        self.path.write_text(json.dumps(self.config))
        with self.assertRaises(ValueError):
            load_config(self.path)

    def test_secure_login_and_cross_origin_guard(self):
        app = secure_app(self.config)
        client = TestClient(app, base_url="https://demo.example.com")
        self.assertEqual(client.get("/api/me").status_code, 401)
        self.assertEqual(client.get("/openapi.json").status_code, 404)
        payload = {"email": "owner@example.com", "password": "random-test-password"}
        blocked = client.post("/api/auth/login", json=payload, headers={"Origin": "https://other.example"})
        self.assertEqual(blocked.status_code, 403)
        login = client.post("/api/auth/login", json=payload, headers={"Origin": "https://demo.example.com"})
        self.assertEqual(login.status_code, 200)
        cookie = login.headers["set-cookie"]
        for attribute in ("Secure", "HttpOnly", "SameSite=lax"):
            self.assertIn(attribute, cookie)
        self.assertEqual(login.headers["cache-control"], "no-store")
        self.assertEqual(client.get("/api/me").status_code, 200)
        self.assertEqual(os.environ["CRYPTO_LAB_ENABLE_LIVE_TRADING"], "0")

    def test_login_attempts_are_bounded_and_expire(self):
        client = TestClient(secure_app(self.config), base_url="https://demo.example.com")
        with patch("crypto_strategy_lab.public_server.monotonic", return_value=100):
            for _ in range(30):
                self.assertEqual(client.post("/api/auth/login", json={}).status_code, 422)
            self.assertEqual(client.post("/api/auth/login", json={}).status_code, 429)
        with patch("crypto_strategy_lab.public_server.monotonic", return_value=161):
            self.assertEqual(client.post("/api/auth/login", json={}).status_code, 422)

    def test_server_preserves_source_cooldowns_on_exit(self):
        self.config["source_cooldowns"] = {"example.com": 1900000000}
        self.path.write_text(json.dumps(self.config))
        with patch("crypto_strategy_lab.public_server.uvicorn.Server") as server:
            async def simulate_serve():
                config = server.call_args.args[0]
                self.assertEqual(config.host, "127.0.0.1")
                collector = config.app.state.radar.collector
                self.assertEqual(collector.blocked_host["example.com"], 1900000000)
                collector.blocked_host["another.example"] = 1900000100
            server.return_value.serve = AsyncMock(side_effect=simulate_serve)
            asyncio.run(serve(self.path, 8093))
        saved = load_config(self.path)
        self.assertEqual(saved["source_cooldowns"]["another.example"], 1900000100)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
