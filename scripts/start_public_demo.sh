#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST="${CRYPTO_LAB_HOST:-0.0.0.0}"
PORT="${CRYPTO_LAB_PORT:-8092}"
CLOUDFLARED="${CLOUDFLARED:-/private/tmp/crypto-tunnel/cloudflared}"

if [[ ! -x "$CLOUDFLARED" ]]; then
  echo "cloudflared not found or not executable: $CLOUDFLARED" >&2
  exit 1
fi

cd "$ROOT_DIR"

export CRYPTO_LAB_SECRET_KEY="${CRYPTO_LAB_SECRET_KEY:-local-dev-secret}"

cleanup() {
  if [[ -n "${WEB_PID:-}" ]]; then
    kill "$WEB_PID" 2>/dev/null || true
  fi
  if [[ -n "${TUNNEL_PID:-}" ]]; then
    kill "$TUNNEL_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

echo "Starting Crypto Strategy Lab on http://${HOST}:${PORT}"
.venv/bin/crypto-lab-web --host "$HOST" --port "$PORT" &
WEB_PID=$!

sleep 2
echo "Starting Cloudflare quick tunnel to http://127.0.0.1:${PORT}"
"$CLOUDFLARED" tunnel --url "http://127.0.0.1:${PORT}" &
TUNNEL_PID=$!

wait "$WEB_PID" "$TUNNEL_PID"
