import io
import json
from datetime import date
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from services import users as user_svc
from services import vehicles as vehicle_svc
from services.trips import (
    save_trip, clear_redo_stack,
    update_trip, update_expense, update_remittance_entry,
)
from services.report import build_report, _PERIOD_LABELS
from db.database import get_db
from utils.formatting import format_currency, snapshot_to_hint, format_log_label


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
        saved = await _save_remittance(db_user["id"], vehicle["id"], data)
        if not saved:
            await q.edit_message_text(
                "⚠️ Remittance already logged today.\nUse `undo` first if you need to change it.",
                parse_mode="Markdown",
            )
            return
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
            "litres": data.get("litres"),
            "odometer": data.get("odometer"),
        })
        await db.execute(
            """INSERT INTO action_log (user_id, action_type, table_name, record_id, snapshot)
               VALUES ($1, 'expense', 'expenses', $2, $3)""",
            user_id, row["id"], snapshot,
        )
    await clear_redo_stack(user_id)


async def _save_remittance(user_id: int, vehicle_id: int, data: dict) -> bool:
    today = date.today()
    async with get_db() as db:
        row = await db.fetchrow(
            """INSERT INTO remittance_log (vehicle_id, amount, paid_on)
               VALUES ($1, $2, $3)
               ON CONFLICT (vehicle_id, paid_on) DO NOTHING
               RETURNING id""",
            vehicle_id, data["amount"], today,
        )
        if not row:
            return False
        snapshot = json.dumps({"amount": data["amount"], "paid_on": str(today)})
        await db.execute(
            """INSERT INTO action_log (user_id, action_type, table_name, record_id, snapshot)
               VALUES ($1, 'remittance', 'remittance_log', $2, $3)""",
            user_id, row["id"], snapshot,
        )
    await clear_redo_stack(user_id)
    return True


async def handle_mark_paid(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Show unpaid trip breakdown for a client and offer Pay All or partial payment."""
    q = update.callback_query
    await q.answer()
    passenger_id = int(q.data.split(":", 1)[1])
    uid = update.effective_user.id
    db_user = await user_svc.get_user(uid)
    currency = db_user["currency"]

    async with get_db() as db:
        trips = await db.fetch(
            """SELECT id, amount, destination, occurred_at
               FROM trips WHERE passenger_id = $1 AND paid = 0 AND user_id = $2
               ORDER BY occurred_at ASC""",
            passenger_id, db_user["id"],
        )
        p = await db.fetchrow(
            "SELECT display_name FROM passengers WHERE id = $1 AND user_id = $2",
            passenger_id, db_user["id"],
        )

    if not trips or not p:
        await q.edit_message_text("No unpaid trips found for this client.")
        return

    name = p["display_name"]
    total = sum(float(t["amount"]) for t in trips)
    lines = [f"💳 *{name}* owes {format_currency(currency, total)}\n"]
    for t in trips:
        dest = f" · {t['destination']}" if t["destination"] else ""
        lines.append(f"• {format_currency(currency, t['amount'])}{dest} — {t['occurred_at'].strftime('%d %b %H:%M')}")
    lines.append("\n_Type an amount to log a partial payment, or tap Pay All._")

    ctx.user_data["paying_passenger"] = {
        "passenger_id": passenger_id,
        "name": name,
        "total": total,
        "trips": [{"id": t["id"], "amount": float(t["amount"])} for t in trips],
    }

    await q.edit_message_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(
                f"✓ Pay All ({format_currency(currency, total)})",
                callback_data=f"payall:{passenger_id}",
            ),
            InlineKeyboardButton("✗ Cancel", callback_data="cn:owed"),
        ]]),
        parse_mode="Markdown",
    )


async def handle_pay_all(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Mark all unpaid trips for a passenger as paid."""
    q = update.callback_query
    await q.answer()
    passenger_id = int(q.data.split(":", 1)[1])
    uid = update.effective_user.id
    db_user = await user_svc.get_user(uid)
    currency = db_user["currency"]
    ctx.user_data.pop("paying_passenger", None)

    async with get_db() as db:
        paid_rows = await db.fetch(
            "UPDATE trips SET paid = 1 WHERE passenger_id = $1 AND paid = 0 AND user_id = $2 RETURNING amount",
            passenger_id, db_user["id"],
        )
        if not paid_rows:
            await q.edit_message_text("No unpaid trips found.")
            return
        total = sum(float(r["amount"]) for r in paid_rows)
        cnt = len(paid_rows)
        await db.execute(
            """UPDATE passengers
               SET lifetime_revenue = lifetime_revenue + $1, trip_count = trip_count + $2
               WHERE id = $3 AND user_id = $4""",
            total, cnt, passenger_id, db_user["id"],
        )
        p = await db.fetchrow(
            "SELECT display_name FROM passengers WHERE id = $1 AND user_id = $2",
            passenger_id, db_user["id"],
        )

    name = p["display_name"] if p else "Client"
    await q.edit_message_text(
        f"✅ *{name}* fully paid — {format_currency(currency, total)} settled ({cnt} trip(s)).",
        parse_mode="Markdown",
    )


async def handle_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    ctx.user_data.pop("pending", None)
    ctx.user_data.pop("paying_passenger", None)
    await q.edit_message_text("Cancelled.")


async def handle_report_select(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    period = q.data.split(":", 1)[1]
    uid = update.effective_user.id
    db_user = await user_svc.get_user(uid)
    if not db_user:
        await q.edit_message_text("Please run /start first.")
        return

    vehicle = await vehicle_svc.get_active_vehicle(db_user["id"])
    if not vehicle:
        await q.edit_message_text("No vehicle found. Run /start to set up.")
        return

    if period not in _PERIOD_LABELS:
        await q.edit_message_text("Invalid period selection.")
        return

    label = _PERIOD_LABELS.get(period)
    await q.edit_message_text(f"⏳ Building {label} report…")

    excel_bytes, filename = await build_report(
        db_user["id"], vehicle["id"], period, db_user["currency"],
        cleared_at=db_user.get("log_cleared_at"),
    )
    doc = io.BytesIO(excel_bytes)
    doc.name = filename
    await q.message.reply_document(
        doc,
        caption=f"📊 *{label} report*",
        parse_mode="Markdown",
        filename=filename,
    )


async def handle_edit_select(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    log_id = int(q.data.split(":", 1)[1])
    uid = update.effective_user.id
    db_user = await user_svc.get_user(uid)

    async with get_db() as db:
        log = await db.fetchrow(
            "SELECT * FROM action_log WHERE id = $1 AND user_id = $2",
            log_id, db_user["id"],
        )

    if not log:
        await q.edit_message_text("Entry not found — it may have been undone already.")
        return

    log = dict(log)
    snapshot = json.loads(log["snapshot"])

    if log["action_type"] == "remittance" and snapshot.get("status") == "REST":
        await q.edit_message_text(
            "Rest days can't be edited.\nUse `undo` then log `rest` again if needed.",
            parse_mode="Markdown",
        )
        return

    hint = snapshot_to_hint(log["action_type"], snapshot)
    ctx.user_data["editing"] = {
        "log_id": log_id,
        "record_id": log["record_id"],
        "action_type": log["action_type"],
        "table_name": log["table_name"],
        "old_snapshot": snapshot,
    }

    await q.edit_message_text(
        f"✏️ *Editing entry*\n\nCurrent: `{hint}`\n\n"
        "Send your corrected entry, or type `cancel` to exit.",
        parse_mode="Markdown",
    )


async def handle_edit_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    uid = update.effective_user.id

    pending = ctx.user_data.pop("pending_edit", None)
    ctx.user_data.pop("editing", None)

    if not pending:
        await q.edit_message_text("Edit expired — please try `edit` again.", parse_mode="Markdown")
        return

    db_user = await user_svc.get_user(uid)
    vehicle = await vehicle_svc.get_active_vehicle(db_user["id"])
    if not vehicle:
        await q.edit_message_text("No vehicle found. Run /start to set up.")
        return
    currency = db_user["currency"]
    atype = pending["action_type"]
    parsed = pending["new_parsed"]

    if atype == "trip":
        await update_trip(
            pending["record_id"], db_user["id"], vehicle["id"],
            parsed, pending["old_snapshot"],
        )
        await q.edit_message_text(
            f"✅ Trip updated — *{format_currency(currency, parsed['amount'])}*",
            parse_mode="Markdown",
        )
    elif atype == "expense":
        await update_expense(pending["record_id"], db_user["id"], parsed)
        await q.edit_message_text(
            f"✅ {parsed['expense_type'].title()} updated — *{format_currency(currency, parsed['amount'])}*",
            parse_mode="Markdown",
        )
    elif atype == "remittance":
        await update_remittance_entry(
            pending["record_id"], vehicle["id"], db_user["id"], parsed["amount"]
        )
        await q.edit_message_text(
            f"✅ Remittance updated — *{format_currency(currency, parsed['amount'])}*",
            parse_mode="Markdown",
        )


async def handle_edit_cancel_inline(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    ctx.user_data.pop("pending_edit", None)
    ctx.user_data.pop("editing", None)
    await q.edit_message_text("Edit cancelled.")


async def handle_clear_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    action = q.data.split(":", 1)[1]

    if action == "cancel":
        await q.edit_message_text("Cancelled — your logs are unchanged.")
        return

    uid = update.effective_user.id
    db_user = await user_svc.get_user(uid)
    if not db_user:
        await q.edit_message_text("Account not found.")
        return

    async with get_db() as db:
        await db.execute(
            "UPDATE users SET log_cleared_at = NOW() WHERE telegram_id = $1", uid
        )
        await db.execute("DELETE FROM action_log WHERE user_id = $1", db_user["id"])
        await db.execute("DELETE FROM redo_log   WHERE user_id = $1", db_user["id"])

    await q.edit_message_text(
        "✅ *Logs cleared.*\n\n"
        "All views start fresh from now.\n"
        "Your data is safe — use `report` to export your full history.",
        parse_mode="Markdown",
    )


async def handle_delete_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    action = q.data.split(":", 1)[1]

    if action == "cancel":
        await q.edit_message_text("Cancelled. Your account is safe.")
        return

    if action == "passengers":
        uid = update.effective_user.id
        db_user = await user_svc.get_user(uid)
        if db_user:
            async with get_db() as db:
                await db.execute("UPDATE trips SET passenger_id = NULL WHERE user_id = $1", db_user["id"])
                await db.execute("DELETE FROM passengers WHERE user_id = $1", db_user["id"])
        await q.edit_message_text(
            "✅ All passenger names deleted.\nYour trips, earnings, and settings are unchanged."
        )
        return

    uid = update.effective_user.id
    db_user = await user_svc.get_user(uid)
    if not db_user:
        await q.edit_message_text("Account not found.")
        return

    user_db_id = db_user["id"]
    async with get_db() as db:
        await db.execute("DELETE FROM action_log WHERE user_id = $1", user_db_id)
        await db.execute("DELETE FROM redo_log   WHERE user_id = $1", user_db_id)
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
