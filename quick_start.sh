#!/usr/bin/env bash
# Optional online bootstrap. The downloaded installer still works independently.
set -euo pipefail
[ "$EUID" = 0 ] || { echo '请用 sudo bash 运行' >&2; exit 1; }
VERSION=1.4.0
EDITION="${1:-bundled}"
case "$EDITION" in
    bundled) PACKAGE="file-transfer-with-aria2-$VERSION.tar.gz";;
    native) PACKAGE="file-transfer-native-$VERSION.tar.gz";;
    *) echo '用法：quick_start.sh [bundled|native]' >&2; exit 1;;
esac
if [ "$EDITION" = bundled ] && [ "$(uname -m)" != x86_64 ]; then
    echo '完整版安装包只支持 Linux amd64' >&2; exit 1
fi
if [ -d /opt/file-transfer ] && [ -n "$(ls -A /opt/file-transfer)" ]; then
    [ -f /opt/file-transfer/.edition ] || { echo '发现已有安装，请按 EDITIONS.md 保留旧配置升级' >&2; exit 1; }
    [ "$(cat /opt/file-transfer/.edition)" = "$EDITION" ] || { echo '已有安装类型不同，请不要覆盖安装' >&2; exit 1; }
fi
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
BASE="https://github.com/lingmo77/FileTransit/releases/download/v$VERSION"
curl --fail --location --retry 3 "$BASE/$PACKAGE" -o "$TMP/$PACKAGE"
curl --fail --location --retry 3 "$BASE/SHA256SUMS" -o "$TMP/SHA256SUMS"
expected="$(awk -v file="$PACKAGE" '$2 == file {print $1}' "$TMP/SHA256SUMS")"
[[ "$expected" =~ ^[a-f0-9]{64}$ ]] || { echo '安装包校验值无效' >&2; exit 1; }
echo "$expected  $TMP/$PACKAGE" | sha256sum -c -
tar xzf "$TMP/$PACKAGE" -C /opt --no-same-owner
bash /opt/file-transfer/install.sh
