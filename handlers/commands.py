from datetime import date, datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from services import users as user_svc
from services import vehicles as vehicle_svc
from services.trips import undo_last_action, save_trip
from db.database import get_db
from utils.formatting import format_currency


async def cmd_undo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    db_user = await user_svc.get_user(uid)
    if not db_user or not db_user["onboarded"]:
        await update.message.reply_text("Please run /start first.")
        return

    result = await undo_last_action(db_user["id"])
    if not result:
        await update.message.reply_text("Nothing to undo.")
        return

    s = result["snapshot"]
    currency = db_user["currency"]
    atype = result["action_type"]

    if atype == "trip":
        detail = format_currency(currency, s["amount"])
        if s.get("destination"):
            detail += f" → {s['destination']}"
        if s.get("passenger_name"):
            detail += f" ({s['passenger_name']})"
        await update.message.reply_text(f"↩️ Removed trip: {detail}")
    elif atype == "expense":
        await update.message.reply_text(
            f"↩️ Removed {s.get('expense_type', 'expense').title()}: {format_currency(currency, s['amount'])}"
        )
    elif atype == "remittance":
        await update.message.reply_text(
            f"↩️ Removed remittance: {format_currency(currency, s['amount'])}"
        )
    else:
        await update.message.reply_text("↩️ Last entry removed.")


async def cmd_day(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    db_user = await user_svc.get_user(uid)
    if not db_user or not db_user["onboarded"]:
        await update.message.reply_text("Please run /start first.")
        return

    args = ctx.args
    if not args:
        await update.message.reply_text(
            "Usage: `/day 3600` — logs your full day's earnings as one entry.",
            parse_mode="Markdown",
        )
        return

    try:
        amount = float(args[0].replace(",", ""))
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Enter a valid amount, e.g. `/day 3600`", parse_mode="Markdown")
        return

    vehicle = await vehicle_svc.get_active_vehicle(db_user["id"])
    if not vehicle:
        await update.message.reply_text("No vehicle found. Run /start to set up.")
        return

    await save_trip(user_id=db_user["id"], vehicle_id=vehicle["id"], amount=amount)
    currency = db_user["currency"]
    await update.message.reply_text(
        f"✅ Day total logged — *{format_currency(currency, amount)}*\n"
        "Use /today to see your profit breakdown.",
        parse_mode="Markdown",
    )


async def cmd_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    db_user = await user_svc.get_user(uid)
    if not db_user or not db_user["onboarded"]:
        await update.message.reply_text("Please run /start first.")
        return

    today = date.today()
    currency = db_user["currency"]
    vehicle = await vehicle_svc.get_active_vehicle(db_user["id"])
    remit_rate = await vehicle_svc.get_remittance_rate(vehicle["id"]) if vehicle else 0.0

    async with get_db() as db:
        tr = await db.fetchrow(
            "SELECT COALESCE(SUM(amount), 0) AS gross, COUNT(*) AS cnt FROM trips WHERE user_id = $1 AND DATE(occurred_at) = $2 AND paid = 1",
            db_user["id"], today,
        )
        ex = await db.fetchrow(
            "SELECT COALESCE(SUM(amount), 0) AS total FROM expenses WHERE user_id = $1 AND DATE(occurred_at) = $2",
            db_user["id"], today,
        )

    gross = tr["gross"]
    trip_count = tr["cnt"]
    costs = ex["total"]
    profit = gross - costs - remit_rate

    day_str = datetime.now().strftime("%A %d %b").replace(" 0", " ")
    lines = [f"📊 *Today — {day_str}*\n"]
    lines.append(f"Trips: *{format_currency(currency, gross)}* ({trip_count})")
    if costs > 0:
        lines.append(f"Costs: *−{format_currency(currency, costs)}*")
    if remit_rate > 0:
        lines.append(f"Remittance: *−{format_currency(currency, remit_rate)}*")
    lines.append("──────────────────")
    sign = "+" if profit >= 0 else ""
    lines.append(f"Net profit: *{sign}{format_currency(currency, profit)}*")

    breakeven = costs + remit_rate
    if breakeven > 0:
        if gross < breakeven:
            still_need = breakeven - gross
            lines.append(f"\nBreak-even: {format_currency(currency, breakeven)}")
            lines.append(f"You need *{format_currency(currency, still_need)}* more ↑")
        else:
            lines.append(f"\n✅ Break-even beaten by *{format_currency(currency, profit)}*")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_week(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("📅 Weekly view coming in a few days. Use /today for now.")


async def cmd_month(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("📆 Monthly view coming soon. Use /today for now.")


async def cmd_clients(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("👥 Client stats coming soon.")


async def cmd_owed(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    db_user = await user_svc.get_user(uid)
    if not db_user or not db_user["onboarded"]:
        await update.message.reply_text("Please run /start first.")
        return

    currency = db_user["currency"]
    async with get_db() as db:
        rows = await db.fetch(
            """SELECT t.amount, t.destination, t.occurred_at, p.display_name
               FROM trips t LEFT JOIN passengers p ON p.id = t.passenger_id
               WHERE t.user_id = $1 AND t.paid = 0
               ORDER BY t.occurred_at DESC LIMIT 20""",
            db_user["id"],
        )

    if not rows:
        await update.message.reply_text("No unpaid trips. All clear!")
        return

    total = sum(r["amount"] for r in rows)
    lines = [f"💸 *Unpaid trips — {format_currency(currency, total)} total*\n"]
    for r in rows:
        name = r["display_name"] or "—"
        dest = r["destination"] or "—"
        dt = r["occurred_at"].strftime("%Y-%m-%d") if r["occurred_at"] else "—"
        lines.append(f"• {format_currency(currency, r['amount'])} · {dest} · {name} · {dt}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_car(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    db_user = await user_svc.get_user(uid)
    if not db_user or not db_user["onboarded"]:
        await update.message.reply_text("Please run /start first.")
        return

    vehicle = await vehicle_svc.get_active_vehicle(db_user["id"])
    if not vehicle:
        await update.message.reply_text("No vehicle found. Run /start to add your car.")
        return

    currency = db_user["currency"]
    remit_rate = await vehicle_svc.get_remittance_rate(vehicle["id"])

    labels = {"OWNED": "Owned", "RENTED": "Renting", "HIRE_PURCHASE": "Hire Purchase"}
    ownership = labels.get(vehicle["ownership"], vehicle["ownership"])

    lines = [f"🚗 *{vehicle['plate_or_nickname']}*\n", f"Ownership: {ownership}"]
    if remit_rate > 0:
        lines.append(f"Daily remittance: *{format_currency(currency, remit_rate)}*")
    lines.append(f"\nCurrency: {currency} · Timezone: {db_user['timezone']}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_fuel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("⛽ Fuel trends coming soon (Day 7).")


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    db_user = await user_svc.get_user(uid)
    currency = (db_user or {}).get("currency", "₺")

    await update.message.reply_text(
        f"*Driver Ledger — Commands*\n\n"
        f"*Logging (just type):*\n"
        f"`450` — trip for {currency}450\n"
        f"`450 kyrenia` — trip with destination\n"
        f"`450 kyrenia mrs adama` — trip + client\n"
        f"`450 kyrenia mrs adama unpaid` — unpaid trip\n"
        f"`fuel 2000` — fuel expense\n"
        f"`fuel 2000 32l` — fuel with litres\n"
        f"`repair 600 bumper` — repair\n"
        f"`washing 120` — car wash\n"
        f"`remit 1050` — remittance paid\n\n"
        f"*Commands:*\n"
        f"`/day 3600` — log full day earnings\n"
        f"`/undo` — remove last entry\n"
        f"`/today` — today's profit & break-even\n"
        f"`/week` — weekly summary\n"
        f"`/owed` — unpaid trips by client\n"
        f"`/car` — vehicle & remittance settings\n"
        f"`/privacy` — privacy info\n"
        f"`/deleteme` — delete your account\n",
        parse_mode="Markdown",
    )


async def cmd_privacy(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "*Privacy*\n\n"
        "Driver Ledger stores the data you type to track your earnings.\n\n"
        "If you log passenger names: we store only the name you type — no phone numbers "
        "or addresses. Please don't enter more than a first name or nickname. Passenger "
        "names are personal data of third parties who haven't consented; handle carefully.\n\n"
        "Your data is never shared with other accounts.\n\n"
        "To delete everything: /deleteme",
        parse_mode="Markdown",
    )


async def cmd_deleteme(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "⚠️ *Delete your account?*\n\n"
        "This permanently deletes all trips, expenses, passengers, and settings. Cannot be undone.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("Yes, delete everything", callback_data="delete:confirm"),
            InlineKeyboardButton("Cancel", callback_data="delete:cancel"),
        ]]),
        parse_mode="Markdown",
    )
