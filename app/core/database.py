"""数据库引擎与会话。

支持 SQLite 与 PostgreSQL，由 ``DB_BUILTIN`` 和 ``DB_HOST`` 等 ``DB_`` 配置决定（见 config.py）。

业务代码按「同一时刻只有一个写事务」设计：配额扣减、离线任务认领等都是
「先写一行取得写锁，再读最新状态，再决定」。

- SQLite 天然如此：``WAL`` 让读不阻塞写，``busy_timeout`` 让写锁冲突时等待
  而不是立刻抛 ``database is locked``。
- PostgreSQL 是行级锁，多个写事务会并行，按不同顺序锁行还可能死锁。因此每个事务
  第一次写入前先取一把事务级咨询锁（``pg_advisory_xact_lock``），提交或回滚时自动
  释放，写事务之间的串行语义与 SQLite 一致；只读查询不受影响。

元数据查询都很轻，真正的重活（文件字节流）不经过数据库，写事务都很短。
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .config import settings

settings.ensure_dirs()

#: 写事务串行化用的咨询锁编号（"FTWR"），与同库其他应用撞号的概率可忽略
WRITE_LOCK_KEY = 0x46545752
#: PostgreSQL 等锁上限（毫秒），与 SQLite 的 busy_timeout 作用相同
PG_LOCK_TIMEOUT_MS = 30000
_WRITE_PREFIXES = ("INSERT", "UPDATE", "DELETE")
_LOCKED = "ft_write_locked"


def _set_sqlite_pragma(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA busy_timeout=10000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA temp_store=MEMORY")
    cursor.execute("PRAGMA cache_size=-20000")  # 约 20MB 页缓存
    cursor.close()


def install_postgresql_write_lock(sync_engine) -> None:
    """让每个 PostgreSQL 事务在第一条写语句前取得全局写锁。"""

    def reset(conn, *_args) -> None:
        conn.info.pop(_LOCKED, None)

    for name in ("begin", "commit", "rollback"):
        event.listen(sync_engine, name, reset)

    @event.listens_for(sync_engine, "before_cursor_execute")
    def _serialize_writes(conn, cursor, statement, _parameters, _context, _executemany) -> None:
        if conn.info.get(_LOCKED) or not statement.lstrip()[:6].upper().startswith(_WRITE_PREFIXES):
            return
        cursor.execute(f"SELECT pg_advisory_xact_lock({WRITE_LOCK_KEY})")
        conn.info[_LOCKED] = True


def _create_engine():
    if settings.is_sqlite:
        created = create_async_engine(
            settings.db_url,
            future=True,
            pool_pre_ping=True,
            connect_args={"check_same_thread": False, "timeout": 30},
        )
        event.listen(created.sync_engine, "connect", _set_sqlite_pragma)
        return created
    created = create_async_engine(
        settings.db_url,
        future=True,
        pool_pre_ping=True,
        # 每个 worker 最多 10 个连接；默认 4 个 worker 约 40 个，低于 PostgreSQL 默认上限 100
        pool_size=5,
        max_overflow=5,
        connect_args={"server_settings": {"lock_timeout": str(PG_LOCK_TIMEOUT_MS), "timezone": "UTC"}},
    )
    install_postgresql_write_lock(created.sync_engine)
    return created


engine = _create_engine()

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
