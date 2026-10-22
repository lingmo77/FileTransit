#!/usr/bin/env bash
#
# 打包发布产物到 dist/。在项目根目录执行。
#
#   ./package.sh                          只打源码包
#   ./package.sh --image <网盘镜像tar> --aria2-image <下载镜像tar>
#
# 离线包里的镜像需要在**有 Docker 的机器**上导出后拿过来：
#
#   docker compose build
#   docker save file-transfer:1.0 -o file-transfer-1.0.tar
#   docker save file-transfer-aria2:1.37.0 -o file-transfer-aria2-1.37.0.tar
#
# 两份 docker save 产物原样放入 images/，发布包统一使用 gzip 压缩。
#
set -euo pipefail

VERSION="${VERSION:-$(date +%F)}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIST="$ROOT/dist"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

IMAGE_TAR=""
ARIA2_IMAGE_TAR=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --image) IMAGE_TAR="${2:?--image 需要镜像tar路径}"; shift 2 ;;
        --aria2-image) ARIA2_IMAGE_TAR="${2:?--aria2-image 需要镜像tar路径}"; shift 2 ;;
        *) echo "未知参数: $1" >&2; exit 1 ;;
    esac
done
[[ "$VERSION" =~ ^[[:alnum:]][[:alnum:]._-]*$ ]] || { echo "版本名称无效" >&2; exit 1; }
if [ -n "$IMAGE_TAR" ]; then
    [ -f "$IMAGE_TAR" ] || { echo "找不到网盘镜像: $IMAGE_TAR" >&2; exit 1; }
fi
if [ -n "$ARIA2_IMAGE_TAR" ]; then
    [ -n "$IMAGE_TAR" ] && [ -f "$ARIA2_IMAGE_TAR" ] || { echo "--aria2-image 需要有效文件及 --image" >&2; exit 1; }
fi

# ---------------------------------------------------------------- 暂存
mkdir -p "$STAGE/file-transfer"
cd "$ROOT"

# 用 tar 管道复制，顺手把缓存排除掉；.env 和本地 data/ 绝不能进包
tar cf - \
    --exclude='__pycache__' --exclude='*.pyc' --exclude='*.pyo' \
    app migrations tests docker \
    Dockerfile docker-compose.yml docker-compose.aria2.yml .dockerignore .gitignore .gitattributes .env.example \
    alembic.ini pytest.ini requirements.txt requirements-dev.txt package.sh package.py \
    README.md PROJECT.md DEPLOY.md FILES.md OFFLINE_DOWNLOAD_PLAN.md RELEASE_NOTES.md EDITIONS.md \
    LICENSE AGENTS.md AUDIT_FIXES.md \
    | (cd "$STAGE/file-transfer" && tar xf -)

mkdir -p "$DIST"

# ---------------------------------------------------------------- 源码包
echo "==> 源码包"
tar czf "$DIST/file-transfer-src-$VERSION.tar.gz" -C "$STAGE" file-transfer
ls -lh "$DIST/file-transfer-src-$VERSION.tar.gz"

# ---------------------------------------------------------------- 离线包
if [ -n "$IMAGE_TAR" ]; then
    echo "==> 标准版（仅网盘镜像）"
    mkdir -p "$STAGE/file-transfer/images"
    cp "$IMAGE_TAR" "$STAGE/file-transfer/images/file-transfer-1.0.tar"
    ( cd "$STAGE/file-transfer/images" && sha256sum file-transfer-1.0.tar > SHA256SUMS )
    tar czf "$DIST/file-transfer-standard-$VERSION.tar.gz" -C "$STAGE" file-transfer
    ls -lh "$DIST/file-transfer-standard-$VERSION.tar.gz"
    if [ -n "$ARIA2_IMAGE_TAR" ]; then
        echo "==> 可选 Aria2 离线下载版（网盘 + Aria2 镜像）"
        cp "$STAGE/file-transfer/docker-compose.aria2.yml" "$STAGE/file-transfer/docker-compose.yml"
        cp "$ARIA2_IMAGE_TAR" "$STAGE/file-transfer/images/file-transfer-aria2-1.37.0.tar"
        ( cd "$STAGE/file-transfer/images" && sha256sum file-transfer-aria2-1.37.0.tar >> SHA256SUMS )
        tar czf "$DIST/file-transfer-with-aria2-$VERSION.tar.gz" -C "$STAGE" file-transfer
        ls -lh "$DIST/file-transfer-with-aria2-$VERSION.tar.gz"
    fi
else
    echo "==> 跳过镜像安装包（未提供 --image）"
fi

(cd "$DIST" && {
    sha256sum "file-transfer-src-$VERSION.tar.gz"
    if [ -n "$IMAGE_TAR" ]; then sha256sum "file-transfer-standard-$VERSION.tar.gz"; fi
    if [ -n "$ARIA2_IMAGE_TAR" ]; then sha256sum "file-transfer-with-aria2-$VERSION.tar.gz"; fi
} > SHA256SUMS)

echo
echo "产物目录: $DIST"
