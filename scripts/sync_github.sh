#!/usr/bin/env bash
set -euo pipefail

message="${1:-Update crypto strategy lab}"

if [[ -x ".venv/bin/python" ]]; then
  .venv/bin/python -m unittest discover -s tests
else
  python -m unittest discover -s tests
fi

git add -A
if git diff --cached --quiet; then
  echo "No changes to sync."
  exit 0
fi

git commit -m "$message"
git push
