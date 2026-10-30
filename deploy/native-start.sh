#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
set --
if [ "${FT_TRUST_PROXY:-false}" = true ]; then
    set -- --proxy-headers --forwarded-allow-ips '*'
else
    set -- --no-proxy-headers
fi
exec .venv/bin/uvicorn app.main:app --host "${FT_HOST:-0.0.0.0}" \
  --port "${FT_PORT:-8000}" --workers "${FT_WORKERS:-4}" --backlog 2048 \
  --timeout-keep-alive 30 --no-server-header \
  "$@"
