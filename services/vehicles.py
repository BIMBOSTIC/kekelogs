from datetime import date
from db.database import get_db


async def create_vehicle(user_id: int, plate_or_nickname: str, ownership: str) -> int:
    async with get_db() as db:
        row = await db.fetchrow(
            "INSERT INTO vehicles (user_id, plate_or_nickname, ownership) VALUES ($1, $2, $3) RETURNING id",
            user_id, plate_or_nickname, ownership,
        )
        return row["id"]


async def get_active_vehicle(user_id: int) -> dict | None:
    async with get_db() as db:
        row = await db.fetchrow(
            "SELECT * FROM vehicles WHERE user_id = $1 AND is_active = 1 ORDER BY created_at DESC LIMIT 1",
            user_id,
        )
        return dict(row) if row else None


async def get_remittance_rate(vehicle_id: int) -> float:
    async with get_db() as db:
        row = await db.fetchrow(
            """SELECT amount FROM remittance_rules
               WHERE vehicle_id = $1 AND effective_to IS NULL
               ORDER BY effective_from DESC LIMIT 1""",
            vehicle_id,
        )
        return row["amount"] if row else 0.0


async def create_remittance_rule(vehicle_id: int, amount: float, frequency: str = "DAILY") -> None:
    async with get_db() as db:
        await db.execute(
            "INSERT INTO remittance_rules (vehicle_id, amount, frequency, effective_from) VALUES ($1, $2, $3, $4)",
            vehicle_id, amount, frequency, date.today(),
        )


async def update_remittance_rate(vehicle_id: int, new_amount: float) -> None:
    async with get_db() as db:
        await db.execute(
            "UPDATE remittance_rules SET effective_to = $1 WHERE vehicle_id = $2 AND effective_to IS NULL",
            date.today(), vehicle_id,
        )
        await db.execute(
            "INSERT INTO remittance_rules (vehicle_id, amount, frequency, effective_from) VALUES ($1, $2, 'DAILY', $3)",
            vehicle_id, new_amount, date.today(),
        )
