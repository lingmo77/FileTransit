# 构建：docker build -t file-transfer:1.0 .
# 运行：docker run -d -p 8000:8000 -v ft-data:/app/data file-transfer:1.0
#
# 采用两段式构建：编译器和头文件只留在 builder 阶段，最终镜像里没有它们。
#
# 这里刻意不写 `# syntax=docker/dockerfile:1`：本文件只用标准指令，
# 不写就不必额外拉取 BuildKit 前端镜像，在受限网络里少一次失败点。

# ---------------------------------------------------------------- 构建阶段
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 少数依赖（如没有对应 wheel 的平台）需要现场编译
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# 先只拷贝依赖清单，让这一层能被缓存
COPY requirements.txt ./
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install -r requirements.txt

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
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh \
    && mkdir -p /app/data \
    && chown -R app:app /app

# 数据卷：SQLite 数据库与用户上传的文件都落在这里
VOLUME ["/app/data"]
EXPOSE 8000

USER app

# slim 镜像里没有 curl，用标准库探活
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request,sys; \
port=os.environ.get('FT_PORT','8000'); \
sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz', timeout=3).status==200 else 1)"

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
