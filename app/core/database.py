"""数据库引擎与会话。

SQLite 的并发策略：
- ``WAL`` 模式让读不阻塞写；
- ``busy_timeout`` 让写锁冲突时等待而不是立刻抛 ``database is locked``；
- 元数据查询都很轻，真正的重活（文件字节流）不经过数据库。
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .config import settings

settings.ensure_dirs()

engine = create_async_engine(
    settings.db_url,
    echo=False,
    future=True,
    pool_pre_ping=True,
    connect_args={
        "check_same_thread": False,
        "timeout": 30,
    },
)


@event.listens_for(engine.sync_engine, "connect")
def _set_sqlite_pragma(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA busy_timeout=10000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA temp_store=MEMORY")
    cursor.execute("PRAGMA cache_size=-20000")  # 约 20MB 页缓存
    cursor.close()


SessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 依赖：每个请求一个会话。"""
    async with SessionLocal() as session:
        yield session
