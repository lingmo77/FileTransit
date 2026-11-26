# 在仓库根目录构建（上下文是根目录）：
#   docker build -f docker/app/Dockerfile -t file-transfer:<版本> .
# 通常直接用根目录的 docker compose，不必单独构建。
#
# 采用两段式构建：builder 安装锁定的二进制 wheel，最终只复制虚拟环境。
#
# 这里刻意不写 `# syntax=docker/dockerfile:1`：本文件只用标准指令，
# 不写就不必额外拉取 BuildKit 前端镜像，在受限网络里少一次失败点。

# ---------------------------------------------------------------- 构建阶段
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# 先只拷贝依赖清单，让这一层能被缓存
COPY requirements.txt ./
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install --only-binary=:all: -r requirements.txt

# ---------------------------------------------------------------- 运行阶段
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    FT_DATA_DIR=/app/data \
    FT_HOST=0.0.0.0 \
    FT_PORT=8000 \
    FT_WORKERS=4

# tini 接管 PID 1，让 SIGTERM 能正确传到 uvicorn；passwd 提供 useradd/groupadd
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini passwd \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app

# 只有这些是运行时必需：应用本体、模板/静态资源、迁移脚本
COPY app/ ./app/
COPY migrations/ ./migrations/
COPY alembic.ini ./
COPY docker/app/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh \
    && mkdir -p /app/data \
    && chown -R app:app /app

# 数据目录：SQLite 数据库（如使用）、用户上传的文件和签名密钥都落在这里
VOLUME ["/app/data"]
EXPOSE 8000

# 不设 USER：entrypoint 以 root 修正挂载目录的属主后，立即降为 app 用户运行

# slim 镜像里没有 curl，用标准库探活
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request,sys; \
port=os.environ.get('FT_PORT','8000'); \
sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz', timeout=3).status==200 else 1)"

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
