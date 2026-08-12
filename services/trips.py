import json
from datetime import date, datetime
from db.database import get_db


async def _get_or_create_passenger(db, user_id: int, display_name: str) -> int:
    name_lower = display_name.strip().lower()
    row = await db.fetchrow(
        "SELECT id FROM passengers WHERE user_id = $1 AND LOWER(display_name) = $2",
        user_id, name_lower,
    )

    if row:
        pid = row["id"]
        await db.execute("UPDATE passengers SET last_seen = NOW() WHERE id = $1", pid)
        return pid

    row = await db.fetchrow(
        "INSERT INTO passengers (user_id, display_name) VALUES ($1, $2) RETURNING id",
        user_id, display_name.strip(),
    )
    return row["id"]


async def clear_redo_stack(user_id: int) -> None:
    async with get_db() as db:
        await db.execute("DELETE FROM redo_log WHERE user_id = $1", user_id)


async def save_trip(
    user_id: int,
    vehicle_id: int,
    amount: float,
    destination: str | None = None,
    passenger_name: str | None = None,
    paid: bool = True,
    payment_method: str = "CASH",
    occurred_at: datetime | None = None,
) -> int:
    ts = occurred_at or datetime.now()

    async with get_db() as db:
        passenger_id = None
        if passenger_name:
            passenger_id = await _get_or_create_passenger(db, user_id, passenger_name)

        row = await db.fetchrow(
            """INSERT INTO trips
               (user_id, vehicle_id, amount, destination, passenger_id, paid, payment_method, occurred_at)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING id""",
            user_id, vehicle_id, amount, destination, passenger_id, int(paid), payment_method, ts,
        )
        trip_id = row["id"]

        snapshot = json.dumps({
            "amount": amount,
            "destination": destination,
            "passenger_name": passenger_name,
            "paid": paid,
            "payment_method": payment_method,
            "occurred_at": ts.isoformat(),
        })
        await db.execute(
            """INSERT INTO action_log (user_id, action_type, table_name, record_id, snapshot)
               VALUES ($1, 'trip', 'trips', $2, $3)""",
            user_id, trip_id, snapshot,
        )

        if passenger_id and paid:
            await db.execute(
                """UPDATE passengers
                   SET lifetime_revenue = lifetime_revenue + $1, trip_count = trip_count + 1
                   WHERE id = $2""",
                amount, passenger_id,
            )

    await clear_redo_stack(user_id)
    return trip_id


async def undo_last_action(user_id: int) -> dict | None:
    async with get_db() as db:
        log = await db.fetchrow(
            "SELECT * FROM action_log WHERE user_id = $1 ORDER BY created_at DESC LIMIT 1",
            user_id,
        )

        if not log:
            return None

        log = dict(log)
        snapshot = json.loads(log["snapshot"])

        # Save to redo_log before deleting so it can be replayed
        await db.execute(
            """INSERT INTO redo_log (user_id, action_type, table_name, snapshot)
               VALUES ($1, $2, $3, $4)""",
            user_id, log["action_type"], log["table_name"], log["snapshot"],
        )

        await db.execute(
            f"DELETE FROM {log['table_name']} WHERE id = $1", log["record_id"]
        )

        if log["action_type"] == "trip":
            pname = snapshot.get("passenger_name")
            if pname and snapshot.get("paid"):
                p = await db.fetchrow(
                    "SELECT id FROM passengers WHERE user_id = $1 AND LOWER(display_name) = $2",
                    user_id, pname.lower(),
                )
                if p:
                    await db.execute(
                        """UPDATE passengers
                           SET lifetime_revenue = GREATEST(0, lifetime_revenue - $1),
                               trip_count = GREATEST(0, trip_count - 1)
                           WHERE id = $2""",
                        snapshot["amount"], p["id"],
                    )

        await db.execute("DELETE FROM action_log WHERE id = $1", log["id"])
        return {"action_type": log["action_type"], "snapshot": snapshot}


async def redo_last_action(user_id: int, vehicle_id: int) -> dict | None:
    async with get_db() as db:
        log = await db.fetchrow(
            "SELECT * FROM redo_log WHERE user_id = $1 ORDER BY created_at DESC LIMIT 1",
            user_id,
        )
        if not log:
            return None

        log = dict(log)
        snapshot = json.loads(log["snapshot"])
        action_type = log["action_type"]
        new_id = None

        if action_type == "trip":
            occurred_at = datetime.fromisoformat(
                snapshot.get("occurred_at", datetime.now().isoformat())
            )
            passenger_id = None
            if snapshot.get("passenger_name"):
                passenger_id = await _get_or_create_passenger(
                    db, user_id, snapshot["passenger_name"]
                )
            paid = snapshot.get("paid", True)
            row = await db.fetchrow(
                """INSERT INTO trips
                   (user_id, vehicle_id, amount, destination, passenger_id, paid, payment_method, occurred_at)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING id""",
                user_id, vehicle_id, snapshot["amount"], snapshot.get("destination"),
                passenger_id, int(paid), snapshot.get("payment_method", "CASH"), occurred_at,
            )
            new_id = row["id"]
            if passenger_id and paid:
                await db.execute(
                    """UPDATE passengers
                       SET lifetime_revenue = lifetime_revenue + $1, trip_count = trip_count + 1
                       WHERE id = $2""",
                    snapshot["amount"], passenger_id,
                )

        elif action_type == "expense":
            row = await db.fetchrow(
                """INSERT INTO expenses
                   (user_id, vehicle_id, type, amount, note, litres, odometer)
                   VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id""",
                user_id, vehicle_id, snapshot["expense_type"], snapshot["amount"],
                snapshot.get("note"), snapshot.get("litres"), snapshot.get("odometer"),
            )
            new_id = row["id"]

        elif action_type == "remittance":
            status = snapshot.get("status", "PAID")
            paid_on_raw = snapshot.get("paid_on", str(date.today()))
            paid_on = date.fromisoformat(paid_on_raw) if isinstance(paid_on_raw, str) else paid_on_raw
            row = await db.fetchrow(
                """INSERT INTO remittance_log (vehicle_id, amount, paid_on, status)
                   VALUES ($1, $2, $3, $4) RETURNING id""",
                vehicle_id, snapshot.get("amount", 0), paid_on, status,
            )
            new_id = row["id"]

        if new_id:
            await db.execute(
                """INSERT INTO action_log (user_id, action_type, table_name, record_id, snapshot)
                   VALUES ($1, $2, $3, $4, $5)""",
                user_id, action_type, log["table_name"], new_id, log["snapshot"],
            )

        await db.execute("DELETE FROM redo_log WHERE id = $1", log["id"])
        return {"action_type": action_type, "snapshot": snapshot}
