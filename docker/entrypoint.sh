#!/bin/sh
# 容器启动脚本：把环境变量翻译成 uvicorn 参数。
# 数据库迁移由应用自身的启动流程（app.core.bootstrap）完成，多 worker 之间用文件锁串行化。
set -e

WORKERS="${FT_WORKERS:-4}"
PORT="${FT_PORT:-8000}"

# 并发下载的关键：多个 worker 进程 + 足够大的 backlog，
# 让 IDM / 迅雷 的多条连接能同时被接纳。
set -- uvicorn app.main:app \
    --host "${FT_HOST:-0.0.0.0}" \
    --port "${PORT}" \
    --workers "${WORKERS}" \
    --backlog 2048 \
    --timeout-keep-alive 30 \
    --no-server-header

# 只有在反向代理后面才信任 X-Forwarded-*（否则客户端可以伪造真实 IP）
if [ "${FT_TRUST_PROXY:-false}" = "true" ]; then
    set -- "$@" --proxy-headers --forwarded-allow-ips "*"
fi

echo "[entrypoint] 启动 uvicorn：workers=${WORKERS} port=${PORT} data=${FT_DATA_DIR:-/app/data}"
exec "$@"
