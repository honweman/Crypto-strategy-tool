#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${CRYPTO_LAB_PORT:-8093}"
CLOUDFLARED="${CLOUDFLARED:-$(command -v cloudflared || true)}"
CONFIG="${CRYPTO_LAB_PUBLIC_CONFIG:-$ROOT_DIR/output/public-runtime.json}"

if [[ ! -x "$CLOUDFLARED" ]]; then
  echo "cloudflared not found or not executable: $CLOUDFLARED" >&2
  exit 1
fi

cd "$ROOT_DIR"

if [[ ! -f "$CONFIG" ]]; then
  echo "Private public-runtime config missing. See docs/deployment.md." >&2
  exit 1
fi

cleanup() {
  if [[ -n "${WEB_PID:-}" ]]; then
    kill "$WEB_PID" 2>/dev/null || true
  fi
  if [[ -n "${TUNNEL_PID:-}" ]]; then
    kill "$TUNNEL_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

echo "Starting secured temporary demo on http://127.0.0.1:${PORT}"
.venv/bin/python -m crypto_strategy_lab.public_server --config "$CONFIG" --port "$PORT" &
WEB_PID=$!

READY=0
for ((i=0; i<30; i++)); do
  sleep 1
  if ! kill -0 "$WEB_PID" 2>/dev/null; then
    echo "Secure server failed; tunnel will not start." >&2
    exit 1
  fi
  if curl --max-time 2 -fsS -D - -o /dev/null "http://127.0.0.1:${PORT}/api/health" | rg -qi '^x-crypto-public-mode: secured-demo'; then
    READY=1
    break
  fi
done
if [[ "$READY" != 1 ]]; then
  echo "Secure server readiness check failed; tunnel will not start." >&2
  exit 1
fi
echo "Starting Cloudflare quick tunnel to http://127.0.0.1:${PORT}"
"$CLOUDFLARED" tunnel --no-autoupdate --url "http://127.0.0.1:${PORT}" &
TUNNEL_PID=$!

while kill -0 "$WEB_PID" 2>/dev/null && kill -0 "$TUNNEL_PID" 2>/dev/null; do
  sleep 2
done
exit 1
