import asyncpg
from contextlib import asynccontextmanager
from pathlib import Path
from config import DATABASE_URL, DATABASE_SSL

_SCHEMA = Path(__file__).parent / "schema.sql"
_pool: asyncpg.Pool | None = None


async def init_db() -> None:
    global _pool
    ssl = "require" if DATABASE_SSL else False
    _pool = await asyncpg.create_pool(DATABASE_URL, ssl=ssl)
    async with _pool.acquire() as conn:
        await conn.execute(_SCHEMA.read_text(encoding="utf-8"))
        await conn.execute(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS morning_push INTEGER NOT NULL DEFAULT 0"
        )
        await conn.execute(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS morning_push_time TEXT NOT NULL DEFAULT '07:00'"
        )


@asynccontextmanager
async def get_db():
    async with _pool.acquire() as conn:
        async with conn.transaction():
            yield conn
