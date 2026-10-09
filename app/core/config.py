"""应用配置。

所有配置项都可以通过环境变量覆盖，前缀为 ``FT_``，例如 ``FT_SECRET_KEY``。
也支持项目根目录下的 ``.env`` 文件（参考 ``.env.example``）。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_prefix="FT_",
        extra="ignore",
    )

    # ---- 基础 ----
    app_name: str = "文件中转站"
    debug: bool = False

    #: 运行时数据目录（SQLite + 文件本体），容器中应挂载为卷
    data_dir: Path = BASE_DIR / "data"

    host: str = "0.0.0.0"
    port: int = 8000
    workers: int = 4

    # ---- 安全 ----
    #: 用于签名 Cookie 与加密敏感配置，生产环境必须通过环境变量覆盖
    secret_key: str = "dev-insecure-secret-key-please-change-me"
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
    #: 过期文件清理间隔（分钟）
    cleanup_interval_minutes: int = 10

    # ---- 默认管理员（首次启动时创建）----
    admin_username: str = "admin"
    admin_password: str = "admin"

    # ---- 缓存目录 ----
    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def db_url(self) -> str:
        """异步驱动（应用运行时使用）。"""
        return f"sqlite+aiosqlite:///{self.db_path.as_posix()}"

    @property
    def sync_db_url(self) -> str:
        """同步驱动（Alembic 迁移使用）。"""
        return f"sqlite:///{self.db_path.as_posix()}"

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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
