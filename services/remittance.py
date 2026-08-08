import json
from datetime import date, timedelta
from db.database import get_db


async def get_today_status(vehicle_id: int) -> dict:
    """Returns today's remittance status: owing / paid / rest / na (owned car)."""
    today = date.today()
    async with get_db() as db:
        rate_row = await db.fetchrow(
            """SELECT amount FROM remittance_rules
               WHERE vehicle_id = $1 AND effective_to IS NULL
               ORDER BY effective_from DESC LIMIT 1""",
            vehicle_id,
        )
        rate = rate_row["amount"] if rate_row else 0.0
        if rate == 0:
            return {"status": "na", "amount": 0.0}

        row = await db.fetchrow(
            "SELECT status, amount FROM remittance_log WHERE vehicle_id = $1 AND paid_on = $2",
            vehicle_id, today,
        )

    if not row:
        return {"status": "owing", "amount": rate}
    return {"status": row["status"].lower(), "amount": row["amount"]}


async def get_owing_balance(vehicle_id: int) -> float:
    """Sum of unpaid remittance days from vehicle creation up to (not including) today."""
    today = date.today()
    async with get_db() as db:
        vehicle = await db.fetchrow(
            "SELECT created_at FROM vehicles WHERE id = $1", vehicle_id
        )
        if not vehicle:
            return 0.0

        rate_row = await db.fetchrow(
            """SELECT amount FROM remittance_rules
               WHERE vehicle_id = $1 AND effective_to IS NULL
               ORDER BY effective_from DESC LIMIT 1""",
            vehicle_id,
        )
        if not rate_row or rate_row["amount"] == 0:
            return 0.0
        rate = rate_row["amount"]

        logged = await db.fetch(
            "SELECT paid_on, status FROM remittance_log WHERE vehicle_id = $1",
            vehicle_id,
        )

    logged_days = {r["paid_on"]: r["status"] for r in logged}
    start = vehicle["created_at"].date()
    owing = 0.0
    current = start
    while current < today:
        if logged_days.get(current) not in ("PAID", "REST"):
            owing += rate
        current += timedelta(days=1)

    return owing


async def mark_rest_day(vehicle_id: int, user_id: int) -> bool:
    """Mark today as a rest day. Returns False if today already has an entry."""
    today = date.today()
    async with get_db() as db:
        existing = await db.fetchrow(
            "SELECT id FROM remittance_log WHERE vehicle_id = $1 AND paid_on = $2",
            vehicle_id, today,
        )
        if existing:
            return False

        row = await db.fetchrow(
            """INSERT INTO remittance_log (vehicle_id, amount, paid_on, status)
               VALUES ($1, 0, $2, 'REST') RETURNING id""",
            vehicle_id, today,
        )
        await db.execute(
            """INSERT INTO action_log (user_id, action_type, table_name, record_id, snapshot)
               VALUES ($1, 'remittance', 'remittance_log', $2, $3)""",
            user_id, row["id"], json.dumps({"amount": 0, "status": "REST"}),
        )
        return True
