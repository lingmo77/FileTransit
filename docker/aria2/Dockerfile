# Built on the Docker host; no local Docker installation is required.
FROM debian:trixie-slim

ARG ARIA2_VERSION=1.37.0+debian-3
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       aria2=${ARIA2_VERSION} ca-certificates python3 nftables \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 offline \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin aria2 \
    && useradd --uid 10002 --gid 10001 --no-create-home --shell /usr/sbin/nologin watchdog

COPY docker/aria2/ /opt/aria2/
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    FT_ARIA2_BT_PORT=16881 FT_ARIA2_BLOCKED_NETWORKS=""
VOLUME ["/downloads", "/state"]
EXPOSE 6800 16881/tcp 16881/udp
HEALTHCHECK --interval=20s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python3", "/opt/aria2/healthcheck.py"]
ENTRYPOINT ["python3", "/opt/aria2/supervisor.py"]
