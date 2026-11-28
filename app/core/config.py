"""应用配置。

所有配置项都可以通过环境变量覆盖，前缀为 ``FT_``，例如 ``FT_SECRET_KEY``；
数据库配置例外，统一用 ``DB_`` 开头（``DB_BUILTIN``、``DB_HOST`` 等）。
也支持项目根目录下的 ``.env`` 文件（参考 ``.env.example``）。
"""

from __future__ import annotations

import os
import secrets
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL

BASE_DIR = Path(__file__).resolve().parent.parent.parent

#: 内置 PostgreSQL 的账号名与库名（见 docker-compose.yml 的 db 服务）
BUILTIN_DB_NAME = "filetransfer"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_prefix="FT_",
        extra="ignore",
        # 数据库配置用 DB_ 开头的别名读取；代码和测试里仍可用字段名传参
        populate_by_name=True,
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
    #: true：完整版内置的 PostgreSQL 容器（完整版默认），只用 DB_PASSWORD，
    #:   密码由数据库容器首次启动时生成并写进 .env；
    #: false：填了 DB_HOST 就连外置 PostgreSQL，没填就用数据目录下的 SQLite（源码版默认）。
    builtin_db: bool = Field(False, validation_alias="DB_BUILTIN")
    db_host: str = Field("", validation_alias="DB_HOST")
    db_port: str = Field("5432", validation_alias="DB_PORT")
    db_name: str = Field("", validation_alias="DB_NAME")
    db_user: str = Field("", validation_alias="DB_USER")
    db_password: str = Field("", validation_alias="DB_PASSWORD")
    #: 内置 PostgreSQL 的主机名，与 docker-compose.yml 的 db 服务一致
    builtin_db_host: str = "db"
    #: 旧版的数据库配置（1.5.0 的 FT_DB_MODE、FT_DATABASE_URL）；仍然设置时启动报错，提示改配置
    db_mode: str = ""
    database_url: str = ""

    @property
    def db_kind(self) -> str:
        """``sqlite``、``builtin`` 或 ``external``。"""
        if self.db_mode.strip() or self.database_url.strip():
            raise ValueError("FT_DB_MODE、FT_DATABASE_URL 已不再使用，请改用 DB_BUILTIN 和 DB_HOST、DB_PORT、"
                             "DB_NAME、DB_USER、DB_PASSWORD（见 .env.example），改好后删除这两行")
        if self.builtin_db:
            if self.db_host.strip():
                raise ValueError("DB_BUILTIN=true 时不使用 DB_HOST；要连接外置 PostgreSQL，请把 DB_BUILTIN 改为 false")
            return "builtin"
        return "external" if self.db_host.strip() else "sqlite"

    def _database_url(self) -> URL:
        kind = self.db_kind
        if kind == "sqlite":
            return URL.create("sqlite", database=(self.data_dir / "app.db").as_posix())
        # 驱动由应用决定：同步（迁移、启动初始化）用 psycopg 3，异步（运行时）用 asyncpg。
        # URL.create 会自动转义密码里的特殊字符，.env 里照原样填写即可。
        if kind == "builtin":
            password = self.db_password.strip()
            if not password:
                raise ValueError(
                    "没有读到内置数据库密码 DB_PASSWORD：它由 docker-compose.yml 里的 db 服务"
                    "首次启动时写进 .env，请确认 db 服务已启动；不使用内置数据库时把 DB_BUILTIN 改为 false")
            return URL.create("postgresql+psycopg", username=BUILTIN_DB_NAME, password=password,
                              host=self.builtin_db_host, port=5432, database=BUILTIN_DB_NAME)
        missing = [name for name, value in (("DB_NAME", self.db_name), ("DB_USER", self.db_user))
                   if not value.strip()]
        if missing:
            raise ValueError(f"使用外置 PostgreSQL 时必须填写 {'、'.join(missing)}")
        port = self.db_port.strip() or "5432"
        if not port.isdigit() or not 0 < int(port) < 65536:
            raise ValueError(f"DB_PORT 必须是 1-65535 的端口号，当前为 {self.db_port!r}")
        return URL.create("postgresql+psycopg", username=self.db_user.strip(),
                          password=self.db_password or None, host=self.db_host.strip(),
                          port=int(port), database=self.db_name.strip())

    @property
    def db_dialect(self) -> str:
        """``sqlite`` 或 ``postgresql``。"""
        return "sqlite" if self.db_kind == "sqlite" else "postgresql"

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
