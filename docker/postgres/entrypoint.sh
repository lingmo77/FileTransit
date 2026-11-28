#!/bin/sh
# 内置 PostgreSQL 的启动脚本：准备好数据库密码，再交给官方镜像的 docker-entrypoint.sh。
#
# 密码保存在 compose 目录的 .env（挂载到 $FT_ENV_FILE）的 DB_PASSWORD 一行。
# 第一次启动时该行为空，随机生成后写回 .env，网盘容器从同一个文件读取。
# 密码只在第一次初始化数据库时生效。之后若 .env 里的密码丢失，重新生成的密码和库里的对不上，
# 所以库已存在而 .env 里没有密码时直接报错，提示恢复，而不是悄悄生成一个新的。
set -eu

ENV_FILE="$FT_ENV_FILE"
KEY=DB_PASSWORD

if [ ! -f "$ENV_FILE" ]; then
    echo "[ft-postgres] 没有找到 .env（挂载在 $ENV_FILE）。" >&2
    echo "[ft-postgres] 请从安装包恢复 docker-compose.yml 旁边的 .env；如果那里是一个名为 .env 的目录，先删除它。" >&2
    exit 1
fi

# 取最后一个 DB_PASSWORD=，去掉 Windows 换行、行尾注释（空白加 #）和两侧引号
read_password() {
    sed -n "s/^[[:space:]]*$KEY=//p" "$ENV_FILE" | tail -n 1 | tr -d '\r' \
        | sed -e 's/[[:space:]]\{1,\}#.*$//' -e 's/[[:space:]]*$//' -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/"
}

PASSWORD=$(read_password)
if [ -z "$PASSWORD" ]; then
    if [ -s "$PGDATA/PG_VERSION" ]; then
        echo "[ft-postgres] 数据库已存在，但 .env 里没有 $KEY。" >&2
        echo "[ft-postgres] 请把原来的密码写回 .env 的 $KEY= 一行（从备份的 .env 找回）后再启动。" >&2
        echo "[ft-postgres] 从 1.5.0 升级的话，密码在 data/secrets/postgres-password。" >&2
        exit 1
    fi
    # 只用字母和数字，写进连接串和 .env 都不需要转义
    PASSWORD=$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 40)
    TMP=$(mktemp)
    if grep -q "^[[:space:]]*$KEY=" "$ENV_FILE"; then
        sed "s/^[[:space:]]*$KEY=.*/$KEY=$PASSWORD/" "$ENV_FILE" >"$TMP"
    else
        cat "$ENV_FILE" >"$TMP"
        # 原文件最后一行没有换行时先补一个
        [ -z "$(tail -c 1 "$ENV_FILE")" ] || echo >>"$TMP"
        echo "$KEY=$PASSWORD" >>"$TMP"
    fi
    # .env 是单文件挂载，只能原地改写（不能 mv 替换），否则宿主机上的文件不会变
    cat "$TMP" >"$ENV_FILE"
    rm -f "$TMP"
    echo "[ft-postgres] 已生成数据库密码，写入 .env 的 $KEY"
fi
# .env 里有数据库密码，只让属主（解压安装包的用户）读写
chmod 0600 "$ENV_FILE"

# 宿主机目录第一次挂载时属于 root；官方脚本只修正 $PGDATA 本身，上级目录也要交给 postgres
DATA_ROOT=$(dirname "$(dirname "$PGDATA")")  # /var/lib/postgresql/18/docker → /var/lib/postgresql
if [ "$(stat -c %U "$DATA_ROOT")" != postgres ]; then
    chown postgres:postgres "$DATA_ROOT"
fi

POSTGRES_PASSWORD=$PASSWORD
export POSTGRES_PASSWORD
exec docker-entrypoint.sh "$@"
