#!/bin/bash
set -euo pipefail

# Only run in Claude Code on the web (remote) environments.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "$CLAUDE_PROJECT_DIR"

# The base image ships a Debian-managed cryptography (v41) that panics under
# this Python build and cannot be uninstalled by pip. Install a supported
# version first with --ignore-installed so the editable install below sees the
# requirement satisfied and never attempts the failing uninstall.
python3 -m pip install --ignore-installed "cryptography>=42,<47"

# Editable install pulls in fastapi / httpx / uvicorn and exposes the package.
python3 -m pip install -e .
