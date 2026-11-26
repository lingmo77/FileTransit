#!/bin/sh
# 容器启动脚本。
# 以 root 启动：把挂载进来的数据目录交给 app 用户（宿主机目录第一次挂载时属于 root），
# 然后降为 app 用户重新执行本脚本。带参数时执行该命令（例如迁移工具），否则启动网盘。
# 数据库迁移由应用自身的启动流程（app.core.bootstrap）完成，多 worker 之间用文件锁串行化。
set -e

APP_UID=10001
APP_GID=10001

if [ "$(id -u)" = 0 ]; then
    for dir in "${FT_DATA_DIR:-/app/data}" ${FT_OFFLINE_DIR:+"$FT_OFFLINE_DIR"}; do
        mkdir -p "$dir"
        # 只在目录属主不对时递归修正，已有站点重启不会每次遍历全部文件
        if [ "$(stat -c %u "$dir")" != "$APP_UID" ]; then
            echo "[entrypoint] 修正 $dir 的属主为 app 用户"
            chown -R "$APP_UID:$APP_GID" "$dir"
        fi
    done
    # HOME 仍是 /root 的话，asyncpg 会去读 /root/.postgresql/ 下的客户端证书而报无权限
    HOME=/app
    export HOME
    exec setpriv --reuid="$APP_UID" --regid="$APP_GID" --init-groups --inh-caps=-all "$0" "$@"
fi

if [ "$#" -gt 0 ]; then
    exec "$@"
fi

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

echo "[entrypoint] 启动 uvicorn：workers=${WORKERS} port=${PORT} data=${FT_DATA_DIR:-/app/data} db=${FT_DB_MODE:-sqlite}"
exec "$@"
