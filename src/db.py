"""Database engine + small helpers."""
from sqlalchemy import create_engine, text
from config import DATABASE_URL

engine = create_engine(DATABASE_URL, pool_pre_ping=True)


def get_engine():
    return engine


def truncate(schema: str, table: str) -> None:
    """Empty a table before a fresh full load (simple, deterministic for a
    learning project — swap for incremental upserts later if desired)."""
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE TABLE {schema}.{table} RESTART IDENTITY CASCADE"))


def ping() -> bool:
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    return True
