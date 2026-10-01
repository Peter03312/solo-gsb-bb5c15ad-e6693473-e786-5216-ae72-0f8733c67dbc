"""数据库连接与 Session 管理。

默认连接 docker-compose 中的 PostgreSQL；可通过环境变量 DATABASE_URL 覆盖。
为了便于本地测试，sqlite 内存库使用 StaticPool（同一连接共享内存数据库）。
"""
import os
import threading

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import StaticPool

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://coldchain:coldchain@db:5432/coldchain",
)

if DATABASE_URL.startswith("sqlite"):
    # 内存库需要 StaticPool 让所有 Session 共享同一连接；文件库用默认队列池
    is_memory = ":memory:" in DATABASE_URL
    engine = create_engine(
        DATABASE_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool if is_memory else None,
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _register_sqlite_compat(dbapi_conn, _record):
        # DDL/CHECK 约束统一书写 char_length()，SQLite 仅有 length()，补一个同名函数
        dbapi_conn.create_function("char_length", 1, lambda s: len(s) if s is not None else None)
        # 文件型库：并发写入时靠 busy timeout 等待行锁（PostgreSQL 无需此设置）
        if not is_memory:
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=10000")
            cur.close()

    # SQLite 没有真正的行级锁，隔离语义也弱于 PostgreSQL（并发事务可能读到
    # 其他连接已写入但未提交的“幻影”行）。用一把进程内可重入锁把所有写事务
    # 串行化，作为 SQLite（内存/文件库、测试）适配；PostgreSQL 不使用此锁，
    # 生产环境仍由 SELECT ... FOR UPDATE 的车次 / 货物行锁串行化。
    sqlite_write_lock = threading.RLock()
else:
    engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)

    import contextlib

    # PostgreSQL：生产路径由数据库行锁串行化，此对象仅保持导入一致（空上下文）
    sqlite_write_lock = contextlib.nullcontext()

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    """FastAPI 依赖：每个请求一个 Session，结束时关闭（未提交自动回滚）。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
