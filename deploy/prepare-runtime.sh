#!/usr/bin/env bash
# Run on the release build host. Pin upstream versions and retain their notices.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$ROOT/dist/runtime"
mkdir -p "$OUT"
curl --fail --location --retry 3 https://download.docker.com/linux/static/stable/x86_64/docker-29.9.2.tgz -o "$OUT/docker.tgz"
curl --fail --location --retry 3 https://github.com/docker/compose/releases/download/v5.6.0/docker-compose-linux-x86_64 -o "$OUT/docker-compose"
curl --fail --location --retry 3 https://github.com/docker/compose/releases/download/v5.6.0/checksums.txt -o "$OUT/upstream-checksums.txt"
expected="$(awk '$2 == "*docker-compose-linux-x86_64" || $2 == "docker-compose-linux-x86_64" {print $1}' "$OUT/upstream-checksums.txt")"
[ -n "$expected" ] || { echo 'Missing upstream Compose checksum' >&2; exit 1; }
echo "$expected  $OUT/docker-compose" | sha256sum -c -
chmod +x "$OUT/docker-compose"
mkdir -p "$OUT/licenses"
curl --fail --location --retry 3 https://raw.githubusercontent.com/moby/moby/docker-v29.9.2/LICENSE -o "$OUT/licenses/docker-LICENSE"
curl --fail --location --retry 3 https://raw.githubusercontent.com/docker/cli/v29.9.2/LICENSE -o "$OUT/licenses/docker-cli-LICENSE"
curl --fail --location --retry 3 https://raw.githubusercontent.com/containerd/containerd/v2.2.1/LICENSE -o "$OUT/licenses/containerd-LICENSE"
curl --fail --location --retry 3 https://raw.githubusercontent.com/opencontainers/runc/v1.3.3/LICENSE -o "$OUT/licenses/runc-LICENSE"
curl --fail --location --retry 3 https://raw.githubusercontent.com/krallin/tini/v0.19.0/LICENSE -o "$OUT/licenses/tini-LICENSE"
curl --fail --location --retry 3 https://raw.githubusercontent.com/docker/compose/v5.6.0/LICENSE -o "$OUT/licenses/compose-LICENSE"
cat > "$OUT/NOTICE.md" <<'EOF'
Bundled Docker Engine 29.9.2 (Linux x86_64 static binaries), Apache-2.0.
Upstream: https://download.docker.com/linux/static/stable/x86_64/docker-29.9.2.tgz
Source and component notices: https://github.com/moby/moby/tree/docker-v29.9.2
Docker's static distribution also contains containerd, runc, tini and related
runtime tools. The archive is redistributed without modification.
Bundled Docker Compose 5.6.0, Apache-2.0.
Upstream: https://github.com/docker/compose/releases/tag/v5.6.0
Compose is verified against the upstream release checksums.txt before packaging.
The Docker tarball is fetched over HTTPS and hashed locally; no upstream signature
verification is claimed. Static runtime installations do not auto-update.
EOF
(cd "$OUT" && sha256sum docker.tgz docker-compose licenses/* NOTICE.md > SHA256SUMS)
