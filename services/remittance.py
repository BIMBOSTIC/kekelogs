import json
from datetime import date
from db.database import get_db
from services.trips import clear_redo_stack


async def get_today_status(vehicle_id: int, cleared_at=None) -> dict:
    """Returns today's remittance status: owing / paid / rest / na (owned car).

    If cleared_at is provided, only remittance entries logged AFTER that timestamp
    are considered (pre-clear entries are invisible).
    """
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
            """SELECT status, amount FROM remittance_log
               WHERE vehicle_id = $1 AND paid_on = $2
                 AND ($3::timestamptz IS NULL OR created_at >= $3)""",
            vehicle_id, today, cleared_at,
        )

    if not row:
        return {"status": "owing", "amount": rate}
    return {"status": row["status"].lower(), "amount": row["amount"]}


async def get_owing_balance(vehicle_id: int, cleared_at=None) -> float:
    """Sum of unpaid remittance days up to (not including) today.

    If cleared_at is set, counts only from that date forward so pre-clear
    history is excluded.
    """
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

        vehicle_start = vehicle["created_at"].date()
        clear_date = cleared_at.date() if cleared_at else None
        start = max(vehicle_start, clear_date) if clear_date else vehicle_start

        if start >= today:
            return 0.0

        owing_count = await db.fetchval(
            """SELECT COUNT(*)
               FROM generate_series($1::date, ($2::date - INTERVAL '1 day')::date, INTERVAL '1 day') AS d(day)
               WHERE d.day NOT IN (
                   SELECT paid_on FROM remittance_log
                   WHERE vehicle_id = $3
                     AND status IN ('PAID', 'REST')
                     AND ($4::timestamptz IS NULL OR created_at >= $4)
               )""",
            start, today, vehicle_id, cleared_at,
        )

    return float(owing_count) * rate


async def mark_rest_day(vehicle_id: int, user_id: int) -> bool:
    """Mark today as a rest day. Returns False if today already has an entry."""
    today = date.today()
    async with get_db() as db:
        row = await db.fetchrow(
            """INSERT INTO remittance_log (vehicle_id, amount, paid_on, status)
               VALUES ($1, 0, $2, 'REST')
               ON CONFLICT (vehicle_id, paid_on) DO NOTHING
               RETURNING id""",
            vehicle_id, today,
        )
        if not row:
            return False
        await db.execute(
            """INSERT INTO action_log (user_id, action_type, table_name, record_id, snapshot)
               VALUES ($1, 'remittance', 'remittance_log', $2, $3)""",
            user_id, row["id"],
            json.dumps({"amount": 0, "status": "REST", "paid_on": str(today)}),
        )
    await clear_redo_stack(user_id)
    return True
