"""Single-process, loopback-only entry point for a temporary HTTPS tunnel."""

from __future__ import annotations

import argparse
import asyncio
from collections import deque
import json
import os
from pathlib import Path
import stat
from time import monotonic

from fastapi import Request
from fastapi.responses import JSONResponse
import uvicorn

from .radar_sources import PublicData
from .security import SecretBox, verify_password
from .store import Database
from .web import create_app


def load_config(path: Path) -> dict:
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError("Public configuration must be private: chmod 600 <config>")
    config = json.loads(path.read_text())
    secret = config.get("secret_key", "")
    if not isinstance(secret, str) or len(secret) < 40:
        raise ValueError("A strong, randomly generated secret_key is required")
    db_path = Path(config["db_path"]).resolve()
    if not db_path.is_file():
        raise ValueError("An existing database is required; no default user is created")
    db = Database(db_path, SecretBox(secret))
    with db.connect() as conn:
        users = conn.execute("SELECT password_hash FROM users WHERE status = 'active'").fetchall()
        if not users:
            raise ValueError("At least one active user is required")
        for user in users:
            if verify_password("change-me-now", user["password_hash"]):
                raise ValueError("Change all default passwords before public access")
        # A new encryption key must not silently strand existing exchange credentials.
        for row in conn.execute("SELECT api_key_enc, api_secret_enc, passphrase_enc FROM exchange_api_keys"):
            for value in row:
                db.secret_box.decrypt(value)
    config["db_path"] = str(db_path)
    return config


def secure_app(config: dict):
    os.environ["CRYPTO_LAB_COOKIE_SECURE"] = "1"
    os.environ["CRYPTO_LAB_ENABLE_LIVE_TRADING"] = "0"
    app = create_app(db_path=config["db_path"], secret_key=config["secret_key"])
    attempts = deque()

    @app.middleware("http")
    async def public_guards(request: Request, call_next):
        path = request.url.path.rstrip("/")
        origin = request.headers.get("origin")
        expected_origin = "https://" + request.headers.get("host", "")
        if request.method not in {"GET", "HEAD", "OPTIONS"} and origin and origin != expected_origin:
            return JSONResponse({"detail": "cross_origin_request_rejected"}, status_code=403)
        if path in {"/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"}:
            return JSONResponse({"detail": "not_found"}, status_code=404)
        if path == "/api/auth/login" and request.method == "POST":
            now = monotonic()
            while attempts and attempts[0] <= now - 60:
                attempts.popleft()
            # A bounded global budget avoids trusting spoofable forwarding headers.
            if len(attempts) >= 30:
                return JSONResponse({"detail": "login_rate_limited"}, status_code=429,
                                    headers={"Retry-After": "60", "Cache-Control": "no-store"})
            attempts.append(now)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        response.headers["X-Crypto-Public-Mode"] = "secured-demo"
        if path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    return app


async def serve(config_path: Path, port: int):
    config = load_config(config_path)
    # Construct network clients and locks on the serving loop (including Python 3.9).
    app = secure_app(config)
    app.state.radar.collector = PublicData()
    app.state.radar.collector.blocked_host.update(config.get("source_cooldowns", {}))
    try:
        await uvicorn.Server(uvicorn.Config(
            app, host="127.0.0.1", port=port, proxy_headers=False, access_log=False,
        )).serve()
    finally:
        await app.state.radar.collector.close()
        config["source_cooldowns"] = app.state.radar.collector.blocked_host
        temporary = config_path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(config, handle)
        temporary.replace(config_path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("output/public-runtime.json"))
    parser.add_argument("--port", type=int, default=8093)
    args = parser.parse_args()
    asyncio.run(serve(args.config.resolve(), args.port))


if __name__ == "__main__":
    main()
