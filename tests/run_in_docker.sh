#!/bin/sh
# 在装有 Docker 的测试服务器上、仓库根目录执行：
#   sh tests/run_in_docker.sh            # 完整测试：先 SQLite，再临时 PostgreSQL
#   sh tests/run_in_docker.sh -k quota   # 额外参数原样传给 pytest
#
# 用与生产镜像相同的 python:3.12-slim 运行；仓库只读挂载，复制到容器内再测，
# 不会在服务器的源码目录里留下 __pycache__ 或测试数据。PostgreSQL 容器用 tmpfs，结束即删除。
set -eu
cd "$(dirname "$0")/.."

PY_IMAGE=${PY_IMAGE:-python:3.12-slim}
PG_IMAGE=${PG_IMAGE:-postgres:18.7-trixie}
NET=ft-test-net
PG=ft-test-pg

cleanup() {
    docker rm -f "$PG" >/dev/null 2>&1 || true
    docker network rm "$NET" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM
cleanup

docker network create "$NET" >/dev/null
# 密码故意带 @ 和 %，顺带验证连接串的 URL 编码
docker run -d --name "$PG" --network "$NET" --tmpfs /var/lib/postgresql \
    -e POSTGRES_USER=ft -e 'POSTGRES_PASSWORD=p@ss%w0rd' -e POSTGRES_DB=filetransfer_test \
    "$PG_IMAGE" >/dev/null
# 初始化阶段的临时实例只监听 unix socket，TCP 可连才说明正式实例已就绪
until docker exec "$PG" pg_isready -q -h 127.0.0.1 -U ft -d filetransfer_test; do sleep 1; done

docker run --rm --network "$NET" -v "$PWD":/src:ro -v ft-test-pip-cache:/root/.cache/pip \
    "$PY_IMAGE" sh -c '
        cp -r /src /w && cd /w
        pip install -q --root-user-action=ignore --disable-pip-version-check \
            -r requirements.txt pytest==9.1.1 pytest-asyncio==1.4.0 || exit 1
        # pytest 设置都在这里，仓库里不放 pytest.ini
        set -- -q -p no:cacheprovider -W ignore::DeprecationWarning \
            -o asyncio_mode=auto -o asyncio_default_fixture_loop_scope=session \
            -o asyncio_default_test_loop_scope=session tests "$@"
        status=0
        echo "===== SQLite"
        python -m pytest "$@" || status=1
        echo "===== PostgreSQL"
        FT_TEST_DATABASE_URL="postgresql://ft:p%40ss%25w0rd@ft-test-pg:5432/filetransfer_test" \
            python -m pytest "$@" || status=1
        exit $status
    ' sh "$@"
