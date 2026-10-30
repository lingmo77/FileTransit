# Isolated systemd host for exercising the bundled runtime on a machine without Docker.
FROM debian:trixie-slim
ENV container=docker
RUN apt-get update && apt-get install -y --no-install-recommends \
    systemd-sysv dbus iptables mount procps iproute2 ca-certificates \
    && rm -rf /var/lib/apt/lists/*
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
