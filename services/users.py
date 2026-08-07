from db.database import get_db


async def get_user(telegram_id: int) -> dict | None:
    async with get_db() as db:
        row = await db.fetchrow(
            "SELECT * FROM users WHERE telegram_id = $1", telegram_id
        )
        return dict(row) if row else None


async def create_user(telegram_id: int, username: str | None) -> None:
    async with get_db() as db:
        await db.execute(
            "INSERT INTO users (telegram_id, username) VALUES ($1, $2) ON CONFLICT (telegram_id) DO NOTHING",
            telegram_id, username,
        )


async def update_user(telegram_id: int, **kwargs) -> None:
    if not kwargs:
        return
    keys = list(kwargs.keys())
    values = list(kwargs.values())
    fields = ", ".join(f"{k} = ${i + 1}" for i, k in enumerate(keys))
    values.append(telegram_id)
    async with get_db() as db:
        await db.execute(
            f"UPDATE users SET {fields} WHERE telegram_id = ${len(values)}",
            *values,
        )
