import json
from datetime import date
from telegram import Update
from telegram.ext import ContextTypes
from services import users as user_svc
from services import vehicles as vehicle_svc
from services.trips import save_trip
from db.database import get_db
from utils.formatting import format_currency


async def handle_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    uid = update.effective_user.id
    entry_type = q.data.split(":", 1)[1]

    pending_json = ctx.user_data.get("pending")
    if not pending_json:
        await q.edit_message_text("This confirmation expired. Please try again.")
        return

    data = json.loads(pending_json)
    ctx.user_data.pop("pending", None)

    db_user = await user_svc.get_user(uid)
    currency = db_user["currency"]
    vehicle = await vehicle_svc.get_active_vehicle(db_user["id"])

    if not vehicle:
        await q.edit_message_text("No vehicle found. Run /start to set up your vehicle.")
        return

    if entry_type == "trip":
        await save_trip(
            user_id=db_user["id"],
            vehicle_id=vehicle["id"],
            amount=data["amount"],
            destination=data.get("destination"),
            passenger_name=data.get("passenger"),
            paid=data.get("paid", True),
            payment_method=data.get("payment_method", "CASH"),
        )
        amt = format_currency(currency, data["amount"])
        parts = [p for p in [data.get("destination"), data.get("passenger")] if p]
        detail = " · ".join(parts)
        await q.edit_message_text(
            f"✅ Trip saved — *{amt}*" + (f"\n{detail}" if detail else ""),
            parse_mode="Markdown",
        )

    elif entry_type == "expense":
        await _save_expense(db_user["id"], vehicle["id"], data)
        amt = format_currency(currency, data["amount"])
        await q.edit_message_text(
            f"✅ {data['expense_type'].title()} saved — *{amt}*", parse_mode="Markdown"
        )

    elif entry_type == "remittance":
        await _save_remittance(db_user["id"], vehicle["id"], data)
        amt = format_currency(currency, data["amount"])
        await q.edit_message_text(f"✅ Remittance logged — *{amt}*", parse_mode="Markdown")


async def _save_expense(user_id: int, vehicle_id: int, data: dict) -> None:
    async with get_db() as db:
        row = await db.fetchrow(
            """INSERT INTO expenses (user_id, vehicle_id, type, amount, note, litres, odometer)
               VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id""",
            user_id, vehicle_id, data["expense_type"], data["amount"],
            data.get("note"), data.get("litres"), data.get("odometer"),
        )
        snapshot = json.dumps({
            "expense_type": data["expense_type"],
            "amount": data["amount"],
            "note": data.get("note"),
        })
        await db.execute(
            """INSERT INTO action_log (user_id, action_type, table_name, record_id, snapshot)
               VALUES ($1, 'expense', 'expenses', $2, $3)""",
            user_id, row["id"], snapshot,
        )


async def _save_remittance(user_id: int, vehicle_id: int, data: dict) -> None:
    async with get_db() as db:
        row = await db.fetchrow(
            "INSERT INTO remittance_log (vehicle_id, amount, paid_on) VALUES ($1, $2, $3) RETURNING id",
            vehicle_id, data["amount"], date.today(),
        )
        snapshot = json.dumps({"amount": data["amount"]})
        await db.execute(
            """INSERT INTO action_log (user_id, action_type, table_name, record_id, snapshot)
               VALUES ($1, 'remittance', 'remittance_log', $2, $3)""",
            user_id, row["id"], snapshot,
        )


async def handle_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    ctx.user_data.pop("pending", None)
    await q.edit_message_text("Cancelled.")


async def handle_delete_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    action = q.data.split(":", 1)[1]

    if action == "cancel":
        await q.edit_message_text("Cancelled. Your account is safe.")
        return

    uid = update.effective_user.id
    db_user = await user_svc.get_user(uid)
    if not db_user:
        await q.edit_message_text("Account not found.")
        return

    user_db_id = db_user["id"]
    async with get_db() as db:
        await db.execute("DELETE FROM action_log WHERE user_id = $1", user_db_id)
        await db.execute("DELETE FROM trips WHERE user_id = $1", user_db_id)
        await db.execute("DELETE FROM expenses WHERE user_id = $1", user_db_id)
        await db.execute("DELETE FROM passengers WHERE user_id = $1", user_db_id)

        vids = await db.fetch("SELECT id FROM vehicles WHERE user_id = $1", user_db_id)
        for v in vids:
            await db.execute("DELETE FROM remittance_rules WHERE vehicle_id = $1", v["id"])
            await db.execute("DELETE FROM remittance_log WHERE vehicle_id = $1", v["id"])

        await db.execute("DELETE FROM vehicles WHERE user_id = $1", user_db_id)
        await db.execute("DELETE FROM users WHERE id = $1", user_db_id)

    await q.edit_message_text("All your data has been deleted. Goodbye.\n\nStart fresh anytime with /start")
