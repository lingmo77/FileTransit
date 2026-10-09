#!/usr/bin/env bash
#
# 打包发布产物到 dist/。在项目根目录执行。
#
#   ./package.sh                          只打源码包
#   ./package.sh --image <镜像tar路径>     额外打离线包（含已构建好的镜像）
#
# 离线包里的镜像需要在**有 Docker 的机器**上导出后拿过来：
#
#   docker compose build
#   docker save file-transfer:1.0 -o file-transfer-1.0.tar
#
# 注意 docker save 的产物本身已经是压缩过的（实测再套 gzip 只省 0.7%），
# 所以这里直接放原始 tar，不改名也不二次压缩。
#
set -euo pipefail

VERSION="${VERSION:-$(date +%F)}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIST="$ROOT/dist"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

IMAGE_TAR=""
if [ "${1:-}" = "--image" ]; then
    IMAGE_TAR="${2:?用法: ./package.sh --image <镜像tar路径>}"
    [ -f "$IMAGE_TAR" ] || { echo "找不到镜像文件: $IMAGE_TAR" >&2; exit 1; }
fi

# ---------------------------------------------------------------- 暂存
mkdir -p "$STAGE/file-transfer"
cd "$ROOT"

# 用 tar 管道复制，顺手把缓存排除掉；.env 和本地 data/ 绝不能进包
tar cf - \
    --exclude='__pycache__' --exclude='*.pyc' --exclude='*.pyo' \
    app migrations tests docker \
    Dockerfile docker-compose.yml .dockerignore .gitignore .env.example \
    alembic.ini pytest.ini requirements.txt requirements-dev.txt package.sh \
    README.md PROJECT.md DEPLOY.md FILES.md \
    | (cd "$STAGE/file-transfer" && tar xf -)

mkdir -p "$DIST"

# ---------------------------------------------------------------- 源码包
echo "==> 源码包"
tar czf "$DIST/file-transfer-src-$VERSION.tar.gz" -C "$STAGE" file-transfer
ls -lh "$DIST/file-transfer-src-$VERSION.tar.gz"

# ---------------------------------------------------------------- 离线包
if [ -n "$IMAGE_TAR" ]; then
    echo "==> 离线包"
    mkdir -p "$STAGE/file-transfer/images"
    cp "$IMAGE_TAR" "$STAGE/file-transfer/images/file-transfer-1.0.tar"
    ( cd "$STAGE/file-transfer/images" && sha256sum file-transfer-1.0.tar > SHA256SUMS )
    tar czf "$DIST/file-transfer-offline-$VERSION.tar.gz" -C "$STAGE" file-transfer
    ls -lh "$DIST/file-transfer-offline-$VERSION.tar.gz"
else
    echo "==> 跳过离线包（未提供 --image）"
fi

echo
echo "产物目录: $DIST"
