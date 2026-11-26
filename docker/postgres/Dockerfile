# 完整版内置的 PostgreSQL。在仓库根目录构建（通常由 docker compose build 完成）。
#
# 在官方镜像外面包一层启动脚本：首次启动时随机生成数据库密码，保存到 /run/ft-secrets，
# 网盘容器以只读方式挂载同一目录读取。账号名、库名与 app/core/config.py 的 BUILTIN_DB_NAME 一致。
FROM postgres:18.6-trixie

ENV POSTGRES_USER=filetransfer \
    POSTGRES_DB=filetransfer \
    FT_DB_PASSWORD_FILE=/run/ft-secrets/postgres-password

COPY docker/postgres/entrypoint.sh /usr/local/bin/ft-postgres-entrypoint.sh
RUN chmod 0755 /usr/local/bin/ft-postgres-entrypoint.sh

# 走 TCP 检查：初始化阶段的临时实例只监听 unix socket，TCP 可连才说明正式实例已就绪
HEALTHCHECK --interval=10s --timeout=5s --start-period=60s --retries=5 \
    CMD pg_isready -q -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"

ENTRYPOINT ["ft-postgres-entrypoint.sh"]
CMD ["postgres"]
