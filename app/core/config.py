"""应用配置。

所有配置项都可以通过环境变量覆盖，前缀为 ``FT_``，例如 ``FT_SECRET_KEY``。
也支持项目根目录下的 ``.env`` 文件（参考 ``.env.example``）。
"""

from __future__ import annotations

import os
import secrets
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError

BASE_DIR = Path(__file__).resolve().parent.parent.parent

DB_MODES = ("sqlite", "builtin", "external")
#: 内置 PostgreSQL 的账号名与库名（见 docker/postgres/Dockerfile）
BUILTIN_DB_NAME = "filetransfer"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_prefix="FT_",
        extra="ignore",
    )

    # ---- 基础 ----
    app_name: str = "文件中转站"
    debug: bool = False

    #: 运行时数据目录（默认 SQLite 数据库、文件本体、签名密钥），容器中应挂载为卷
    data_dir: Path = BASE_DIR / "data"

    host: str = "0.0.0.0"
    port: int = 8000
    workers: int = 4

    # ---- 安全 ----
    #: 用于签名 Cookie 与加密敏感配置；留空时自动生成并保存在数据目录（见 _resolve_secret_key）
    secret_key: str = ""
    session_cookie_name: str = "ft_session"
    session_ttl_hours: int = 24 * 14
    #: HTTPS 环境下应设为 true
    cookie_secure: bool = False
    #: 部署在 Nginx 等反向代理之后时设为 true，才会信任 X-Forwarded-For
    trust_proxy: bool = False

    # ---- 限流 ----
    #: 关闭后不再限制请求频率（自动化测试会关闭）
    rate_limit_enabled: bool = True
    #: 每个 IP 每窗口允许的登录尝试次数（窗口单位：秒）
    rate_limit_login: int = 10
    rate_limit_login_window: int = 300
    #: 每个 IP 每窗口允许的注册次数
    rate_limit_register: int = 5
    rate_limit_register_window: int = 3600
    #: 每个 IP 每窗口允许的「重发验证邮件 / 找回密码」次数
    rate_limit_resend: int = 3
    rate_limit_resend_window: int = 600
    #: 每个用户每窗口允许的上传次数
    rate_limit_upload: int = 120
    rate_limit_upload_window: int = 3600

    # ---- 上传 / 下载 ----
    #: 单文件大小上限的**播种默认值**：只在首次建库时写进 settings 表，
    #: 之后一律以后台设置 quota.max_upload_mb 为准（改这里不影响已建好的库）。
    max_upload_mb: int = 20480
    #: 服务端读写文件的块大小
    io_chunk_kb: int = 1024
    upload_concurrency: int = 8
    upload_user_concurrency: int = 3
    upload_temp_max_mb: int = 40960
    upload_idle_timeout_seconds: int = 120
    #: 过期文件清理间隔（分钟）
    cleanup_interval_minutes: int = 10

    # ---- 离线下载（下载引擎运行在独立容器） ----
    offline_dir: Path | None = None
    aria2_rpc_url: str = "http://aria2:6800/jsonrpc"
    aria2_rpc_secret: str = ""
    aria2_rpc_secret_file: Path | None = None
    aria2_download_dir: str = "/downloads"
    offline_poll_seconds: float = 2.0

    @property
    def offline_root(self) -> Path:
        return self.offline_dir or self.data_dir / "offline"

    @property
    def offline_secret_path(self) -> Path:
        return self.aria2_rpc_secret_file or self.offline_root / "control" / "rpc-secret"

    # ---- 默认管理员（首次启动时创建）----
    admin_username: str = "admin"
    admin_password: str = "admin"
    # Installer seeds these only when the corresponding database setting is absent.
    initial_site_url: str = ""
    initial_offline_enabled: bool = False

    # ---- 数据库 ----
    #: ``sqlite``：数据目录下的 app.db（源码版默认）；
    #: ``builtin``：完整版内置的 PostgreSQL 容器（完整版默认），密码由数据库容器自动生成；
    #: ``external``：外置 PostgreSQL，连接串写在 ``database_url``。
    db_mode: str = "sqlite"
    #: 只用于 external：``postgresql://用户:密码@主机:5432/库名``，密码里的特殊字符需要 URL 编码
    database_url: str = ""
    #: 内置 PostgreSQL 的连接参数，与 docker-compose.yml 的 db 服务、docker/postgres/ 镜像保持一致
    builtin_db_host: str = "db"
    builtin_db_password_file: Path = Path("/run/ft-secrets/postgres-password")

    @property
    def db_mode_normalized(self) -> str:
        mode = self.db_mode.strip().lower()
        if mode not in DB_MODES:
            raise ValueError(f"FT_DB_MODE 只能是 {'、'.join(DB_MODES)}，当前为 {self.db_mode!r}")
        return mode

    def _database_url(self) -> URL:
        mode = self.db_mode_normalized
        raw = self.database_url.strip()
        if mode == "sqlite":
            if raw:
                raise ValueError("FT_DB_MODE=sqlite 时不使用 FT_DATABASE_URL；"
                                 "要连接外置 PostgreSQL，请把 FT_DB_MODE 改为 external")
            return URL.create("sqlite", database=(self.data_dir / "app.db").as_posix())
        # 驱动由应用决定：同步（迁移、启动初始化）用 psycopg 3，异步（运行时）用 asyncpg
        if mode == "builtin":
            if raw:
                raise ValueError("FT_DB_MODE=builtin 时不使用 FT_DATABASE_URL；"
                                 "要连接外置 PostgreSQL，请把 FT_DB_MODE 改为 external")
            return URL.create("postgresql+psycopg", username=BUILTIN_DB_NAME,
                              password=self._builtin_password(), host=self.builtin_db_host,
                              port=5432, database=BUILTIN_DB_NAME)
        if not raw:
            raise ValueError("FT_DB_MODE=external 时必须设置 FT_DATABASE_URL")
        try:
            url = make_url(raw)
        except ArgumentError as exc:
            raise ValueError("FT_DATABASE_URL 格式不正确") from exc
        if url.get_backend_name() not in ("postgresql", "postgres"):
            raise ValueError("FT_DATABASE_URL 只支持 postgresql://")
        return url.set(drivername="postgresql+psycopg")

    def _builtin_password(self) -> str:
        path = self.builtin_db_password_file
        try:
            password = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ValueError(
                f"读不到内置数据库的密码文件 {path}：请确认 docker-compose.yml 里的 db 服务已启动；"
                "不使用内置数据库时把 FT_DB_MODE 改为 sqlite 或 external") from exc
        if not password:
            raise ValueError(f"内置数据库的密码文件 {path} 为空")
        return password

    @property
    def db_dialect(self) -> str:
        """``sqlite`` 或 ``postgresql``。"""
        return "sqlite" if self.db_mode_normalized == "sqlite" else "postgresql"

    @property
    def is_sqlite(self) -> bool:
        return self.db_dialect == "sqlite"

    @property
    def sqlite_path(self) -> Path:
        """SQLite 模式下的数据库文件；也是迁移工具默认读取的源库。"""
        return self.data_dir / "app.db"

    @property
    def db_path(self) -> Path | None:
        """SQLite 数据库文件；PostgreSQL 时为 None。"""
        return self.sqlite_path if self.is_sqlite else None

    @property
    def db_url(self) -> str:
        """异步驱动（应用运行时使用）。"""
        return self._render("sqlite+aiosqlite", "postgresql+asyncpg")

    @property
    def sync_db_url(self) -> str:
        """同步驱动（Alembic 迁移、启动初始化使用）。"""
        return self._render("sqlite", "postgresql+psycopg")

    def _render(self, sqlite_driver: str, postgresql_driver: str) -> str:
        url = self._database_url()
        if url.get_backend_name() == "sqlite":
            # 文件路径原样拼接：render_as_string 会把 Windows 盘符的冒号编码成 %3A
            return f"{sqlite_driver}:///{url.database}"
        # asyncpg 不受 Windows 默认事件循环（Proactor）限制，psycopg 的异步模式受限
        return url.set(drivername=postgresql_driver).render_as_string(hide_password=False)

    @property
    def storage_dir(self) -> Path:
        """已完成的文件本体。"""
        return self.data_dir / "storage"

    @property
    def tmp_dir(self) -> Path:
        """上传中的临时分片。"""
        return self.data_dir / "tmp"

    @property
    def io_chunk_size(self) -> int:
        return self.io_chunk_kb * 1024

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.storage_dir, self.tmp_dir):
            path.mkdir(parents=True, exist_ok=True)


#: 1.5.0 之前未配置密钥时使用的默认值；只用于兼容这类已有数据库，不再用于新站点
LEGACY_SECRET_KEY = "dev-insecure-secret-key-please-change-me"


def _resolve_secret_key(cfg: Settings) -> None:
    """未通过环境变量提供密钥时，从数据目录读取；首次启动随机生成并持久保存。

    多 worker 会同时走到这里：先写临时文件再 ``os.link`` 发布，已存在则读取对方写好的值，
    保证所有进程使用同一把密钥。已有数据库却没有密钥文件，说明是旧版用默认值建的库，
    继续沿用旧值，否则会话签名、SMTP 密码和离线任务来源都无法解密。
    """
    if cfg.secret_key:
        return
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    path = cfg.data_dir / ".secret_key"
    if not path.exists():
        # 看数据目录里有没有旧 SQLite 库，与当前数据库模式无关：从旧版迁到 PostgreSQL 的站点
        # 同样要沿用旧密钥，否则迁过去的会话、SMTP 密码等都解不开
        legacy = cfg.sqlite_path.exists()
        value = LEGACY_SECRET_KEY if legacy else secrets.token_urlsafe(48)
        tmp = path.with_name(f".secret_key.{os.getpid()}.{secrets.token_hex(4)}.tmp")
        try:
            tmp.write_text(value, encoding="utf-8")
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            try:
                os.link(tmp, path)
            except FileExistsError:
                pass
        finally:
            tmp.unlink(missing_ok=True)
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError(f"密钥文件 {path} 为空，请恢复原文件或通过 FT_SECRET_KEY 提供原密钥")
    cfg.secret_key = value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    cfg = Settings()
    cfg.sync_db_url  # 连接串写错时启动即报错，而不是等到第一次查询
    _resolve_secret_key(cfg)
    return cfg


settings = get_settings()
