#!/bin/sh
# 内置 PostgreSQL 的启动脚本：准备好数据库密码，再交给官方镜像的 docker-entrypoint.sh。
#
# 密码只在第一次初始化数据库时生效。之后若密码文件丢失，重新生成的密码和库里的对不上，
# 所以库已存在而密码文件不在时直接报错，提示从备份恢复，而不是悄悄生成一个新的。
set -eu

FILE="$FT_DB_PASSWORD_FILE"
DIR=$(dirname "$FILE")
APP_UID=10001  # 网盘容器里的 app 用户，需要能读到密码

mkdir -p "$DIR"
chmod 0711 "$DIR"

if [ ! -s "$FILE" ]; then
    if [ -s "$PGDATA/PG_VERSION" ]; then
        echo "[ft-postgres] 数据库已存在，但找不到密码文件 $FILE。" >&2
        echo "[ft-postgres] 请从备份恢复 data/secrets/postgres-password 后再启动。" >&2
        exit 1
    fi
    # 子 shell 里收紧 umask，不能影响后面官方脚本创建数据目录
    (
        umask 077
        # 只用字母和数字，写进连接串不需要转义
        tr -dc 'A-Za-z0-9' </dev/urandom | head -c 40 >"$FILE.tmp"
    )
    mv "$FILE.tmp" "$FILE"
    echo "[ft-postgres] 已生成数据库密码：$FILE"
fi
chown "$APP_UID:$APP_UID" "$FILE"
chmod 0400 "$FILE"

# 宿主机目录第一次挂载时属于 root；官方脚本只修正 $PGDATA 本身，上级目录也要交给 postgres
DATA_ROOT=/var/lib/postgresql
if [ "$(stat -c %U "$DATA_ROOT")" != postgres ]; then
    chown postgres:postgres "$DATA_ROOT"
fi

POSTGRES_PASSWORD=$(cat "$FILE")
export POSTGRES_PASSWORD
exec docker-entrypoint.sh "$@"
