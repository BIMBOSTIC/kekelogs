import json
import logging
from datetime import date, datetime, timezone
from db.database import get_db

_logger = logging.getLogger(__name__)

_DELETE_SQL = {
    "trips":          "DELETE FROM trips WHERE id = $1",
    "expenses":       "DELETE FROM expenses WHERE id = $1",
    "remittance_log": "DELETE FROM remittance_log WHERE id = $1",
}


async def _get_or_create_passenger(db, user_id: int, display_name: str) -> int:
    row = await db.fetchrow(
        """INSERT INTO passengers (user_id, display_name)
           VALUES ($1, $2)
           ON CONFLICT (user_id, LOWER(display_name)) DO UPDATE SET last_seen = NOW()
           RETURNING id""",
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
    ts = occurred_at or datetime.now(timezone.utc)

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

        await db.execute("DELETE FROM redo_log WHERE user_id = $1", user_id)

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

        if log["action_type"] == "mark_paid":
            trip_ids = snapshot.get("trip_ids", [])
            paid_total = snapshot.get("paid_total", 0.0)
            trip_count_val = snapshot.get("trip_count", 0)
            passenger_id = snapshot.get("passenger_id")
            if trip_ids:
                await db.execute("UPDATE trips SET paid = 0 WHERE id = ANY($1::int[])", trip_ids)
            if passenger_id and paid_total:
                await db.execute(
                    """UPDATE passengers
                       SET lifetime_revenue = GREATEST(0, lifetime_revenue - $1),
                           trip_count = GREATEST(0, trip_count - $2)
                       WHERE id = $3""",
                    paid_total, trip_count_val, passenger_id,
                )
        else:
            sql = _DELETE_SQL.get(log["table_name"])
            if sql is None:
                _logger.error("Illegal table_name %r in action_log id=%s", log["table_name"], log["id"])
                raise ValueError(f"Illegal table_name: {log['table_name']!r}")
            await db.execute(sql, log["record_id"])

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
                snapshot.get("occurred_at", datetime.now(timezone.utc).isoformat())
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
                   VALUES ($1, $2, $3, $4)
                   ON CONFLICT (vehicle_id, paid_on) DO NOTHING
                   RETURNING id""",
                vehicle_id, snapshot.get("amount", 0), paid_on, status,
            )
            if row:
                new_id = row["id"]

        elif action_type == "mark_paid":
            trip_ids = snapshot.get("trip_ids", [])
            paid_total = snapshot.get("paid_total", 0.0)
            trip_count_val = snapshot.get("trip_count", 0)
            passenger_id = snapshot.get("passenger_id")
            if trip_ids:
                await db.execute("UPDATE trips SET paid = 1 WHERE id = ANY($1::int[])", trip_ids)
            if passenger_id and paid_total:
                await db.execute(
                    """UPDATE passengers
                       SET lifetime_revenue = lifetime_revenue + $1,
                           trip_count = trip_count + $2
                       WHERE id = $3""",
                    paid_total, trip_count_val, passenger_id,
                )
            await db.execute(
                """INSERT INTO action_log (user_id, action_type, table_name, record_id, snapshot)
                   VALUES ($1, 'mark_paid', 'trips', 0, $2)""",
                user_id, log["snapshot"],
            )

        if new_id:
            await db.execute(
                """INSERT INTO action_log (user_id, action_type, table_name, record_id, snapshot)
                   VALUES ($1, $2, $3, $4, $5)""",
                user_id, action_type, log["table_name"], new_id, log["snapshot"],
            )

        await db.execute("DELETE FROM redo_log WHERE id = $1", log["id"])
        return {"action_type": action_type, "snapshot": snapshot}


async def update_trip(
    record_id: int,
    user_id: int,
    vehicle_id: int,
    parsed: dict,
    old_snapshot: dict,
) -> None:
    async with get_db() as db:
        # Reverse old passenger stats if the original trip was a paid named trip
        old_pname = old_snapshot.get("passenger_name")
        old_paid = old_snapshot.get("paid", True)
        if old_pname and old_paid:
            p = await db.fetchrow(
                "SELECT id FROM passengers WHERE user_id = $1 AND LOWER(display_name) = $2",
                user_id, old_pname.lower(),
            )
            if p:
                await db.execute(
                    """UPDATE passengers
                       SET lifetime_revenue = GREATEST(0, lifetime_revenue - $1),
                           trip_count = GREATEST(0, trip_count - 1)
                       WHERE id = $2""",
                    old_snapshot["amount"], p["id"],
                )

        new_pname = parsed.get("passenger")
        new_paid = parsed.get("paid", True)
        new_amount = parsed["amount"]
        passenger_id = None
        if new_pname:
            passenger_id = await _get_or_create_passenger(db, user_id, new_pname)

        await db.execute(
            """UPDATE trips
               SET amount=$1, destination=$2, passenger_id=$3, paid=$4, payment_method=$5
               WHERE id=$6 AND user_id=$7""",
            new_amount, parsed.get("destination"), passenger_id,
            int(new_paid), parsed.get("payment_method", "CASH"), record_id, user_id,
        )

        if passenger_id and new_paid:
            await db.execute(
                """UPDATE passengers
                   SET lifetime_revenue = lifetime_revenue + $1, trip_count = trip_count + 1
                   WHERE id = $2""",
                new_amount, passenger_id,
            )

        new_snapshot = json.dumps({
            "amount": new_amount,
            "destination": parsed.get("destination"),
            "passenger_name": new_pname,
            "paid": new_paid,
            "payment_method": parsed.get("payment_method", "CASH"),
            "occurred_at": old_snapshot.get("occurred_at"),
        })
        await db.execute(
            "UPDATE action_log SET snapshot=$1 WHERE user_id=$2 AND table_name='trips' AND record_id=$3",
            new_snapshot, user_id, record_id,
        )


async def update_expense(record_id: int, user_id: int, parsed: dict) -> None:
    async with get_db() as db:
        await db.execute(
            """UPDATE expenses
               SET type=$1, amount=$2, note=$3, litres=$4, odometer=$5
               WHERE id=$6 AND user_id=$7""",
            parsed["expense_type"], parsed["amount"],
            parsed.get("note"), parsed.get("litres"), parsed.get("odometer"),
            record_id, user_id,
        )
        new_snapshot = json.dumps({
            "expense_type": parsed["expense_type"],
            "amount": parsed["amount"],
            "note": parsed.get("note"),
            "litres": parsed.get("litres"),
            "odometer": parsed.get("odometer"),
        })
        await db.execute(
            "UPDATE action_log SET snapshot=$1 WHERE user_id=$2 AND table_name='expenses' AND record_id=$3",
            new_snapshot, user_id, record_id,
        )


async def update_remittance_entry(record_id: int, vehicle_id: int, user_id: int, amount: float) -> None:
    async with get_db() as db:
        await db.execute(
            "UPDATE remittance_log SET amount=$1 WHERE id=$2 AND vehicle_id=$3",
            amount, record_id, vehicle_id,
        )
        new_snapshot = json.dumps({"amount": amount})
        await db.execute(
            "UPDATE action_log SET snapshot=$1 WHERE user_id=$2 AND table_name='remittance_log' AND record_id=$3",
            new_snapshot, user_id, record_id,
        )
