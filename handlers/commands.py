import io
import json
import logging
from datetime import date, datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from services import users as user_svc
from services import vehicles as vehicle_svc
from services.trips import undo_last_action, redo_last_action, save_trip
from services.remittance import mark_rest_day
from services.report import build_report, _PERIOD_LABELS
from db.database import get_db
from utils.formatting import format_currency, format_log_label

_logger = logging.getLogger(__name__)


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
        if s.get("status") == "REST":
            await update.message.reply_text("↩️ Rest day removed.")
        else:
            await update.message.reply_text(
                f"↩️ Removed remittance: {format_currency(currency, s['amount'])}"
            )
    elif atype == "mark_paid":
        name = s.get("name", "Client")
        cnt = s.get("trip_count", 0)
        paid_total = s.get("paid_total", 0.0)
        await update.message.reply_text(
            f"↩️ Payment reversed: *{name}* — {cnt} trip(s) marked unpaid\n"
            f"({format_currency(currency, paid_total)} unconfirmed)",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text("↩️ Last entry removed.")


async def cmd_redo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    db_user = await user_svc.get_user(uid)
    if not db_user or not db_user["onboarded"]:
        await update.message.reply_text("Please run /start first.")
        return

    vehicle = await vehicle_svc.get_active_vehicle(db_user["id"])
    if not vehicle:
        await update.message.reply_text("No vehicle found. Run /start to set up.")
        return

    result = await redo_last_action(db_user["id"], vehicle["id"])
    if not result:
        await update.message.reply_text("Nothing to redo.")
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
        await update.message.reply_text(f"↪️ Redone trip: {detail}")
    elif atype == "expense":
        await update.message.reply_text(
            f"↪️ Redone {s.get('expense_type', 'expense').title()}: {format_currency(currency, s['amount'])}"
        )
    elif atype == "remittance":
        if s.get("status") == "REST":
            await update.message.reply_text("↪️ Rest day restored.")
        else:
            await update.message.reply_text(
                f"↪️ Redone remittance: {format_currency(currency, s['amount'])}"
            )
    elif atype == "mark_paid":
        name = s.get("name", "Client")
        cnt = s.get("trip_count", 0)
        paid_total = s.get("paid_total", 0.0)
        await update.message.reply_text(
            f"↪️ Payment restored: *{name}* — {cnt} trip(s) marked paid\n"
            f"({format_currency(currency, paid_total)} settled)",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text("↪️ Last entry restored.")


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
    cleared = db_user.get("log_cleared_at")

    try:
        async with get_db() as db:
            remit_rate = 0.0
            remit_info = {"status": "na", "amount": 0.0}
            owing_balance = 0.0

            if vehicle:
                rate_row = await db.fetchrow(
                    """SELECT amount FROM remittance_rules
                       WHERE vehicle_id = $1 AND effective_to IS NULL
                       ORDER BY effective_from DESC LIMIT 1""",
                    vehicle["id"],
                )
                remit_rate = rate_row["amount"] if rate_row else 0.0

                if remit_rate > 0:
                    remit_row = await db.fetchrow(
                        """SELECT status, amount FROM remittance_log
                           WHERE vehicle_id = $1 AND paid_on = $2
                             AND ($3::timestamptz IS NULL OR created_at >= $3)""",
                        vehicle["id"], today, cleared,
                    )
                    if remit_row:
                        remit_info = {"status": remit_row["status"].lower(), "amount": remit_row["amount"]}
                    else:
                        remit_info = {"status": "owing", "amount": remit_rate}

                    v_start = vehicle["created_at"].date()
                    clear_date = cleared.date() if cleared else None
                    ow_start = max(v_start, clear_date) if clear_date else v_start
                    if ow_start < today:
                        ow_count = await db.fetchval(
                            """SELECT COUNT(*)
                               FROM generate_series($1::date, ($2::date - INTERVAL '1 day')::date, '1 day') AS d(day)
                               LEFT JOIN remittance_log rl
                                 ON rl.vehicle_id = $3 AND rl.paid_on = d.day
                                 AND rl.status IN ('PAID', 'REST')
                                 AND ($4::timestamptz IS NULL OR rl.created_at >= $4)
                               WHERE rl.id IS NULL""",
                            ow_start, today, vehicle["id"], cleared,
                        )
                        owing_balance = float(ow_count or 0) * remit_rate

            tr = await db.fetchrow(
                """SELECT COALESCE(SUM(amount), 0) AS gross, COUNT(*) AS cnt
                   FROM trips
                   WHERE user_id = $1 AND DATE(occurred_at) = $2 AND paid = 1
                     AND occurred_at >= COALESCE($3, '2000-01-01'::timestamptz)""",
                db_user["id"], today, cleared,
            )
            un = await db.fetchrow(
                """SELECT COALESCE(SUM(amount), 0) AS gross, COUNT(*) AS cnt
                   FROM trips
                   WHERE user_id = $1 AND DATE(occurred_at) = $2 AND paid = 0
                     AND occurred_at >= COALESCE($3, '2000-01-01'::timestamptz)""",
                db_user["id"], today, cleared,
            )
            ex = await db.fetchrow(
                """SELECT COALESCE(SUM(amount), 0) AS total
                   FROM expenses
                   WHERE user_id = $1 AND DATE(occurred_at) = $2
                     AND occurred_at >= COALESCE($3, '2000-01-01'::timestamptz)""",
                db_user["id"], today, cleared,
            )
    except Exception:
        _logger.error("cmd_today DB error for user %s", uid, exc_info=True)
        await update.message.reply_text("Couldn't load today's data — please try again in a moment.")
        return

    gross = tr["gross"]
    paid_count = tr["cnt"]
    owed_today = un["gross"]
    owed_count = un["cnt"]
    costs = ex["total"]
    remit_due = remit_info["amount"] if remit_info["status"] not in ("na", "rest") else 0.0
    profit = gross - costs - remit_due

    day_str = datetime.now().strftime("%A %d %b").replace(" 0", " ")
    lines = [f"📊 *Today — {day_str}*\n"]

    trip_note = f"{paid_count} paid"
    if owed_count:
        trip_note += f", {owed_count} owed"
    lines.append(f"Trips: *{format_currency(currency, gross)}* ({trip_note})")

    if owed_today > 0:
        lines.append(f"❌ Owed today: *{format_currency(currency, owed_today)}*")
    if costs > 0:
        lines.append(f"Costs: *−{format_currency(currency, costs)}*")

    if remit_info["status"] == "paid":
        lines.append(f"Remittance: ✅ *{format_currency(currency, remit_info['amount'])}* paid")
    elif remit_info["status"] == "rest":
        lines.append("Remittance: 🏖️ rest day")
    elif remit_due > 0:
        lines.append(f"Remittance: ⚠️ *{format_currency(currency, remit_due)}* not paid yet")

    lines.append("──────────────────")
    sign = "+" if profit >= 0 else ""
    lines.append(f"Net profit: *{sign}{format_currency(currency, profit)}*")

    breakeven = costs + remit_due
    if breakeven > 0:
        if gross < breakeven:
            still_need = breakeven - gross
            lines.append(f"\nBreak-even: {format_currency(currency, breakeven)}")
            lines.append(f"You need *{format_currency(currency, still_need)}* more ↑")
        else:
            lines.append(f"\n✅ Break-even beaten by *{format_currency(currency, profit)}*")

    if owing_balance > 0:
        lines.append(f"\n📛 Previous days owing: *{format_currency(currency, owing_balance)}*")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_week(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    db_user = await user_svc.get_user(uid)
    if not db_user or not db_user["onboarded"]:
        await update.message.reply_text("Please run /start first.")
        return

    currency = db_user["currency"]
    vehicle = await vehicle_svc.get_active_vehicle(db_user["id"])
    remit_rate = await vehicle_svc.get_remittance_rate(vehicle["id"]) if vehicle else 0.0

    today = date.today()
    week_start = today - timedelta(days=6)
    cleared = db_user.get("log_cleared_at")
    effective_start = max(week_start, cleared.date()) if cleared and cleared.date() > week_start else week_start

    async with get_db() as db:
        trip_rows = await db.fetch(
            """SELECT DATE(occurred_at) AS day, COALESCE(SUM(amount), 0) AS gross, COUNT(*) AS cnt
               FROM trips WHERE user_id = $1 AND DATE(occurred_at) >= $2 AND paid = 1
                 AND ($3::timestamptz IS NULL OR occurred_at >= $3)
               GROUP BY DATE(occurred_at)""",
            db_user["id"], effective_start, cleared,
        )
        expense_rows = await db.fetch(
            """SELECT DATE(occurred_at) AS day, COALESCE(SUM(amount), 0) AS costs
               FROM expenses WHERE user_id = $1 AND DATE(occurred_at) >= $2
                 AND ($3::timestamptz IS NULL OR occurred_at >= $3)
               GROUP BY DATE(occurred_at)""",
            db_user["id"], effective_start, cleared,
        )
        remit_rows = await db.fetch(
            "SELECT paid_on AS day, status FROM remittance_log WHERE vehicle_id = $1 AND paid_on >= $2",
            vehicle["id"] if vehicle else 0, effective_start,
        ) if vehicle else []

    trips_by_day = {r["day"]: (r["gross"], r["cnt"]) for r in trip_rows}
    costs_by_day = {r["day"]: r["costs"] for r in expense_rows}
    remit_by_day = {r["day"]: r["status"] for r in remit_rows}

    days = []
    d = effective_start
    while d <= today:
        gross, cnt = trips_by_day.get(d, (0.0, 0))
        costs = costs_by_day.get(d, 0.0)
        remit = 0.0 if remit_by_day.get(d) == "REST" else remit_rate
        profit = gross - costs - remit
        days.append({"date": d, "gross": gross, "profit": profit, "cnt": cnt})
        d += timedelta(days=1)

    if not days:
        await update.message.reply_text("No data yet since your last clear.")
        return

    profits = [d["profit"] for d in days]
    best = max(days, key=lambda d: d["profit"])
    worst = min(days, key=lambda d: d["profit"])
    bar_max = max((abs(p) for p in profits), default=1) or 1

    week_gross = sum(d["gross"] for d in days)
    week_profit = sum(d["profit"] for d in days)

    header = "📅 *Since clear*\n" if cleared and cleared.date() > week_start else "📅 *Last 7 days*\n"
    lines = [header]
    for d in days:
        profit = d["profit"]
        bar_len = min(8, round(abs(profit) / bar_max * 8))
        bar = ("█" if profit >= 0 else "▒") * bar_len + "░" * (8 - bar_len)
        star = " ⭐" if d["date"] == best["date"] and profit > 0 else ""
        sign = "+" if profit >= 0 else ""
        day_label = d["date"].strftime("%a") + " " + str(d["date"].day)
        lines.append(f"`{day_label:<7}` {bar}  {sign}{format_currency(currency, profit)}{star}")

    lines.append("\n──────────────────")
    lines.append(f"Earnings: *{format_currency(currency, week_gross)}*")
    sign = "+" if week_profit >= 0 else ""
    lines.append(f"Profit:   *{sign}{format_currency(currency, week_profit)}*")
    if best["profit"] != worst["profit"]:
        lines.append(f"\n⭐ Best: {best['date'].strftime('%a')} {format_currency(currency, best['profit'])}")
        lines.append(f"📉 Worst: {worst['date'].strftime('%a')} {format_currency(currency, worst['profit'])}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_month(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    db_user = await user_svc.get_user(uid)
    if not db_user or not db_user["onboarded"]:
        await update.message.reply_text("Please run /start first.")
        return

    currency = db_user["currency"]
    vehicle = await vehicle_svc.get_active_vehicle(db_user["id"])
    now = datetime.now()
    month_start = date(now.year, now.month, 1)
    month_name = now.strftime("%B %Y")
    cleared = db_user.get("log_cleared_at")
    effective_start = max(month_start, cleared.date()) if cleared and cleared.date() > month_start else month_start

    async with get_db() as db:
        tr = await db.fetchrow(
            """SELECT COALESCE(SUM(amount), 0) AS gross, COUNT(*) AS trips,
                      COUNT(DISTINCT DATE(occurred_at)) AS days
               FROM trips WHERE user_id = $1 AND DATE(occurred_at) >= $2 AND paid = 1
                 AND ($3::timestamptz IS NULL OR occurred_at >= $3)""",
            db_user["id"], effective_start, cleared,
        )
        ex_rows = await db.fetch(
            """SELECT type, COALESCE(SUM(amount), 0) AS total
               FROM expenses WHERE user_id = $1 AND DATE(occurred_at) >= $2
                 AND ($3::timestamptz IS NULL OR occurred_at >= $3)
               GROUP BY type ORDER BY total DESC""",
            db_user["id"], effective_start, cleared,
        )
        remit = await db.fetchrow(
            """SELECT COALESCE(SUM(amount), 0) AS total, COUNT(*) AS days_paid
               FROM remittance_log WHERE vehicle_id = $1 AND paid_on >= $2 AND status = 'PAID'""",
            vehicle["id"] if vehicle else 0, effective_start,
        ) if vehicle else None

    gross = tr["gross"]
    trip_count = tr["trips"]
    days_worked = tr["days"]
    total_expenses = sum(r["total"] for r in ex_rows)
    remit_total = remit["total"] if remit else 0.0
    remit_days = remit["days_paid"] if remit else 0
    net = gross - total_expenses - remit_total

    type_labels = {
        "FUEL": "Fuel", "REPAIR": "Repairs", "WASHING": "Washing",
        "ACCESSORY": "Accessories", "FINE": "Fines",
        "INSURANCE": "Insurance", "TYRE": "Tyres", "OTHER": "Other",
    }

    lines = [f"📆 *{month_name}*\n"]
    lines.append(f"Earnings: *{format_currency(currency, gross)}*  ({trip_count} trips, {days_worked} days)")
    lines.append("──────────────────")

    for r in ex_rows:
        label = type_labels.get(r["type"], r["type"].title())
        lines.append(f"{label}: *−{format_currency(currency, r['total'])}*")

    if remit_total > 0:
        lines.append(f"Remittance: *−{format_currency(currency, remit_total)}*  ({remit_days} days paid)")

    lines.append("──────────────────")
    sign = "+" if net >= 0 else ""
    lines.append(f"Net profit: *{sign}{format_currency(currency, net)}*")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_clients(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    db_user = await user_svc.get_user(uid)
    if not db_user or not db_user["onboarded"]:
        await update.message.reply_text("Please run /start first.")
        return

    currency = db_user["currency"]
    cleared = db_user.get("log_cleared_at")

    async with get_db() as db:
        rows = await db.fetch(
            """SELECT p.id, p.display_name,
                      COALESCE(SUM(t.amount), 0) AS lifetime_revenue,
                      COUNT(t.id) AS trip_count
               FROM passengers p
               JOIN trips t ON t.passenger_id = p.id AND t.paid = 1
                 AND ($2::timestamptz IS NULL OR t.occurred_at >= $2)
               WHERE p.user_id = $1
               GROUP BY p.id, p.display_name
               HAVING COUNT(t.id) > 0
               ORDER BY lifetime_revenue DESC LIMIT 10""",
            db_user["id"], cleared,
        )
        unpaid_rows = await db.fetch(
            """SELECT p.display_name, COUNT(*) AS cnt, SUM(t.amount) AS total
               FROM trips t JOIN passengers p ON p.id = t.passenger_id
               WHERE t.user_id = $1 AND t.paid = 0
                 AND ($2::timestamptz IS NULL OR t.occurred_at >= $2)
               GROUP BY p.display_name""",
            db_user["id"], cleared,
        )

    if not rows:
        await update.message.reply_text(
            "No named clients yet.\nAdd a name to a trip: `450 kyrenia mrs adama`",
            parse_mode="Markdown",
        )
        return

    unpaid_map = {r["display_name"].lower(): r for r in unpaid_rows}
    total_revenue = sum(r["lifetime_revenue"] for r in rows) or 1

    lines = ["👥 *Top clients*\n"]
    for i, r in enumerate(rows, 1):
        name = r["display_name"]
        revenue = r["lifetime_revenue"]
        trips = r["trip_count"]
        pct = round(revenue / total_revenue * 100)
        avg = revenue / trips if trips else 0

        lines.append(
            f"*{i}. {name}* — {format_currency(currency, revenue)}  ({trips} trips · {pct}% · avg {format_currency(currency, avg)})"
        )
        unpaid = unpaid_map.get(name.lower())
        if unpaid:
            lines.append(f"   ⚠️ owes {format_currency(currency, unpaid['total'])} ({unpaid['cnt']} unpaid)")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_owed(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    db_user = await user_svc.get_user(uid)
    if not db_user or not db_user["onboarded"]:
        await update.message.reply_text("Please run /start first.")
        return

    currency = db_user["currency"]
    cleared = db_user.get("log_cleared_at")
    async with get_db() as db:
        named = await db.fetch(
            """SELECT p.id, p.display_name, COUNT(*) AS cnt, SUM(t.amount) AS total
               FROM trips t JOIN passengers p ON p.id = t.passenger_id
               WHERE t.user_id = $1 AND t.paid = 0
                 AND ($2::timestamptz IS NULL OR t.occurred_at >= $2)
               GROUP BY p.id, p.display_name
               ORDER BY total DESC""",
            db_user["id"], cleared,
        )
        unnamed = await db.fetchrow(
            """SELECT COUNT(*) AS cnt, COALESCE(SUM(amount), 0) AS total
               FROM trips WHERE user_id = $1 AND paid = 0 AND passenger_id IS NULL
                 AND ($2::timestamptz IS NULL OR occurred_at >= $2)""",
            db_user["id"], cleared,
        )

    total_owed = sum(r["total"] for r in named) + unnamed["total"]
    if total_owed == 0:
        await update.message.reply_text("No unpaid trips. All clear! ✅")
        return

    lines = [f"💸 *Unpaid — {format_currency(currency, total_owed)} total*\n"]
    keyboard = []
    for r in named:
        lines.append(f"*{r['display_name']}* · {r['cnt']} trip(s) · {format_currency(currency, r['total'])}")
        keyboard.append([InlineKeyboardButton(
            f"💳 {r['display_name']} — {format_currency(currency, r['total'])}",
            callback_data=f"paid:{r['id']}",
        )])

    if unnamed["cnt"] > 0:
        lines.append(f"Unnamed · {unnamed['cnt']} trip(s) · {format_currency(currency, unnamed['total'])}")

    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None,
        parse_mode="Markdown",
    )


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


async def cmd_setremit(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    db_user = await user_svc.get_user(uid)
    if not db_user or not db_user["onboarded"]:
        await update.message.reply_text("Please run /start first.")
        return

    currency = db_user["currency"]
    vehicle = await vehicle_svc.get_active_vehicle(db_user["id"])
    if not vehicle:
        await update.message.reply_text("No vehicle found. Run /start to set up.")
        return

    args = ctx.args
    if not args:
        current = await vehicle_svc.get_remittance_rate(vehicle["id"])
        await update.message.reply_text(
            f"Current remittance: *{format_currency(currency, current)}/day*\n\n"
            "To update: `/setremit 1200`",
            parse_mode="Markdown",
        )
        return

    try:
        amount = float(args[0].replace(",", ""))
        if amount < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Enter a valid amount, e.g. `/setremit 1200`", parse_mode="Markdown")
        return

    await vehicle_svc.update_remittance_rate(vehicle["id"], amount)
    await update.message.reply_text(
        f"✅ Remittance updated to *{format_currency(currency, amount)}/day* from today.\n"
        "Past days are unchanged — the old rate still applies to historical profit.",
        parse_mode="Markdown",
    )


async def cmd_morning(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    db_user = await user_svc.get_user(uid)
    if not db_user or not db_user["onboarded"]:
        await update.message.reply_text("Please run /start first.")
        return

    args = ctx.args
    if not args:
        status = "on ✅" if db_user.get("morning_push") else "off"
        push_time = db_user.get("morning_push_time", "07:00")
        await update.message.reply_text(
            f"Morning push is *{status}*\n"
            f"Time: *{push_time}* ({db_user['timezone']})\n\n"
            f"Turn on: `/morning on` or `/morning on 06:30`\n"
            f"Turn off: `/morning off`",
            parse_mode="Markdown",
        )
        return

    action = args[0].lower()

    if action == "off":
        await user_svc.update_user(uid, morning_push=0)
        for job in ctx.job_queue.get_jobs_by_name(f"morning_{uid}"):
            job.schedule_removal()
        await update.message.reply_text("Morning push turned off.")
        return

    if action == "on":
        time_str = args[1] if len(args) > 1 else "07:00"
        try:
            parts = time_str.split(":")
            h, m = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
            if not (0 <= h <= 23 and 0 <= m <= 59):
                raise ValueError
            time_str = f"{h:02d}:{m:02d}"
        except (ValueError, IndexError):
            await update.message.reply_text(
                "Invalid time. Use format: `/morning on 07:00`", parse_mode="Markdown"
            )
            return

        await user_svc.update_user(uid, morning_push=1, morning_push_time=time_str)

        vehicle = await vehicle_svc.get_active_vehicle(db_user["id"])
        if vehicle:
            from services.morning import schedule_push
            schedule_push(
                ctx.application,
                telegram_id=uid,
                user_db_id=db_user["id"],
                vehicle_id=vehicle["id"],
                currency=db_user["currency"],
                timezone=db_user["timezone"],
                push_time_str=time_str,
            )

        await update.message.reply_text(
            f"✅ Morning push set for *{time_str}* ({db_user['timezone']})\n"
            "I'll send your break-even every morning before your shift.",
            parse_mode="Markdown",
        )
        return

    await update.message.reply_text(
        "Usage: `/morning on [HH:MM]` or `/morning off`", parse_mode="Markdown"
    )


async def cmd_rest(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    db_user = await user_svc.get_user(uid)
    if not db_user or not db_user["onboarded"]:
        await update.message.reply_text("Please run /start first.")
        return

    vehicle = await vehicle_svc.get_active_vehicle(db_user["id"])
    if not vehicle:
        await update.message.reply_text("No vehicle found. Run /start to set up.")
        return

    success = await mark_rest_day(vehicle["id"], db_user["id"])
    if success:
        await update.message.reply_text(
            "🏖️ *Rest day logged.*\nNo remittance due today.\nUse /undo to remove if logged by mistake.",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            "Today already has a remittance entry. Use /undo first if you need to change it."
        )


async def cmd_fuel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    db_user = await user_svc.get_user(uid)
    if not db_user or not db_user["onboarded"]:
        await update.message.reply_text("Please run /start first.")
        return

    currency = db_user["currency"]
    cleared = db_user.get("log_cleared_at")

    async with get_db() as db:
        rows = await db.fetch(
            """SELECT DATE_TRUNC('month', occurred_at) AS month,
                      SUM(amount) AS total, SUM(litres) AS total_litres, COUNT(*) AS fills
               FROM expenses
               WHERE user_id = $1 AND type = 'FUEL'
                 AND ($2::timestamptz IS NULL OR occurred_at >= $2)
               GROUP BY DATE_TRUNC('month', occurred_at)
               ORDER BY month DESC LIMIT 6""",
            db_user["id"], cleared,
        )

    if not rows:
        await update.message.reply_text(
            "No fuel entries yet.\nLog fuel: `fuel 2000` or `fuel 2000 32l` to track cost per litre.",
            parse_mode="Markdown",
        )
        return

    lines = ["⛽ *Fuel — last 6 months*\n"]
    for r in rows:
        month_str = r["month"].strftime("%b %Y")
        total = r["total"]
        fills = r["fills"]
        litres = r["total_litres"]

        line = f"*{month_str}* — {format_currency(currency, total)}  ({fills} fills)"
        if litres:
            cpl = total / litres
            line += f"\n  {litres:.0f} L · {format_currency(currency, cpl)}/L"
        lines.append(line)

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    db_user = await user_svc.get_user(uid)
    if not db_user or not db_user["onboarded"]:
        await update.message.reply_text("Please run /start first.")
        return

    args = ctx.args or []
    period = args[0].lower() if args else None

    if not period:
        await update.message.reply_text(
            "📊 *Export report*\n\nChoose a period:",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("Today", callback_data="report:today"),
                    InlineKeyboardButton("This week", callback_data="report:week"),
                    InlineKeyboardButton("This month", callback_data="report:month"),
                ],
            ]),
            parse_mode="Markdown",
        )
        return

    await _send_report(update.message, db_user, period)


async def _send_report(message, db_user: dict, period: str) -> None:
    if period not in _PERIOD_LABELS:
        await message.reply_text("Invalid period. Choose: today, week, or month.")
        return

    vehicle = await vehicle_svc.get_active_vehicle(db_user["id"])
    if not vehicle:
        await message.reply_text("No vehicle found. Run /start to set up.")
        return

    label = _PERIOD_LABELS[period]
    wait_msg = await message.reply_text(f"⏳ Building {label} report…")

    excel_bytes, filename = await build_report(
        db_user["id"], vehicle["id"], period, db_user["currency"],
        cleared_at=db_user.get("log_cleared_at"),
    )

    doc = io.BytesIO(excel_bytes)
    doc.name = filename
    await message.reply_document(
        doc,
        caption=f"📊 *{label} report*",
        parse_mode="Markdown",
        filename=filename,
    )
    await wait_msg.delete()


async def cmd_edit(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    db_user = await user_svc.get_user(uid)
    if not db_user or not db_user["onboarded"]:
        await update.message.reply_text("Please run /start first.")
        return

    async with get_db() as db:
        logs = await db.fetch(
            "SELECT id, action_type, snapshot FROM action_log WHERE user_id = $1 ORDER BY created_at DESC LIMIT 3",
            db_user["id"],
        )

    if not logs:
        await update.message.reply_text("No recent entries to edit.")
        return

    currency = db_user["currency"]
    buttons = []
    for log in logs:
        snap = json.loads(log["snapshot"])
        label = format_log_label(log["action_type"], snap, currency)
        buttons.append([InlineKeyboardButton(f"✏️ {label}", callback_data=f"edit_sel:{log['id']}")])

    await update.message.reply_text(
        "✏️ *Edit an entry*\n\nSelect which one to edit:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )


async def cmd_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    db_user = await user_svc.get_user(uid)
    if not db_user or not db_user["onboarded"]:
        await update.message.reply_text("Please run /start first.")
        return

    await update.message.reply_text(
        "⚠️ *Clear your logs?*\n\n"
        "All views (today, week, month, fuel, clients) will start fresh from now.\n"
        "Your data is never deleted — export your full history anytime with `report`.\n\n"
        "_Undo and redo history will also be cleared._",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✓ Clear now", callback_data="logclear:confirm"),
            InlineKeyboardButton("✗ Cancel", callback_data="logclear:cancel"),
        ]]),
        parse_mode="Markdown",
    )


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
        f"`450 kyrenia on aug 13` — log trip for a past day\n"
        f"`fuel 2000` — fuel expense\n"
        f"`fuel 2000 32l` — fuel with litres\n"
        f"`repair 600 bumper` — repair\n"
        f"`washing 120` — car wash\n"
        f"`remit 1050` — remittance paid\n\n"
        f"*Commands (type with or without `/`):*\n"
        f"`day 3600` — log full day earnings\n"
        f"`undo` — remove last entry\n"
        f"`redo` — restore last undone entry\n"
        f"`edit` — edit one of the last 3 entries\n"
        f"`clear` — start fresh (data kept, views reset)\n"
        f"`today` — today's profit & break-even\n"
        f"`summary yesterday` — full detail for any day\n"
        f"`rest` — mark today as a rest day (no remittance)\n"
        f"`morning on 07:00` — daily break-even push before your shift\n"
        f"`week` — weekly summary\n"
        f"`month` — monthly summary\n"
        f"`fuel` — fuel cost history\n"
        f"`owed` — unpaid trips by client\n"
        f"`clients` — top clients\n"
        f"`setremit 1200` — update daily remittance rate\n"
        f"`car` — vehicle & remittance settings\n"
        f"`report` — export Excel report (week / month / today)\n"
        f"`privacy` — privacy info\n"
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
        "⚠️ *What would you like to delete?*\n\n"
        "Passenger names are personal data of third parties — you can remove them without losing your earnings history.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🗑 Passenger names only", callback_data="delete:passengers")],
            [InlineKeyboardButton("💥 Delete everything", callback_data="delete:confirm")],
            [InlineKeyboardButton("Cancel", callback_data="delete:cancel")],
        ]),
        parse_mode="Markdown",
    )


_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2,
    "mar": 3, "march": 3, "apr": 4, "april": 4,
    "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}


def _parse_summary_date(args: list) -> date | None:
    today = date.today()
    if not args:
        return today
    text = " ".join(args).lower().strip()
    if text in ("today", ""):
        return today
    if text == "yesterday":
        return today - timedelta(days=1)
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    parts = text.split()
    if len(parts) == 2:
        a, b = parts
        if a in _MONTHS and b.isdigit():
            month, day = _MONTHS[a], int(b)
        elif b in _MONTHS and a.isdigit():
            month, day = _MONTHS[b], int(a)
        else:
            return None
        year = today.year
        try:
            d = date(year, month, day)
            return date(year - 1, month, day) if d > today else d
        except ValueError:
            return None
    if len(parts) == 1 and parts[0].isdigit():
        try:
            return date(today.year, today.month, int(parts[0]))
        except ValueError:
            return None
    return None


async def cmd_summary(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    db_user = await user_svc.get_user(uid)
    if not db_user or not db_user["onboarded"]:
        await update.message.reply_text("Please run /start first.")
        return

    args = ctx.args or []
    target = _parse_summary_date(args)
    if target is None:
        await update.message.reply_text(
            "Couldn't understand the date. Try:\n"
            "`summary yesterday`\n"
            "`summary aug 8`\n"
            "`summary 2026-08-08`",
            parse_mode="Markdown",
        )
        return

    currency = db_user["currency"]
    vehicle = await vehicle_svc.get_active_vehicle(db_user["id"])
    cleared = db_user.get("log_cleared_at")

    # For an explicit historical date query, don't apply the clear filter if the
    # target day predates the clear — the user wants to see that day's actual data.
    if cleared and target < cleared.date():
        effective_cleared = None
    else:
        effective_cleared = cleared

    try:
        async with get_db() as db:
            paid = await db.fetchrow(
                """SELECT COALESCE(SUM(amount), 0) AS gross, COUNT(*) AS cnt
                   FROM trips WHERE user_id = $1 AND DATE(occurred_at) = $2 AND paid = 1
                     AND occurred_at >= COALESCE($3, '2000-01-01'::timestamptz)""",
                db_user["id"], target, effective_cleared,
            )
            unpaid = await db.fetchrow(
                """SELECT COALESCE(SUM(amount), 0) AS gross, COUNT(*) AS cnt
                   FROM trips WHERE user_id = $1 AND DATE(occurred_at) = $2 AND paid = 0
                     AND occurred_at >= COALESCE($3, '2000-01-01'::timestamptz)""",
                db_user["id"], target, effective_cleared,
            )
            trip_rows = await db.fetch(
                """SELECT t.amount, t.destination, t.paid, t.occurred_at,
                          p.display_name AS passenger
                   FROM trips t LEFT JOIN passengers p ON p.id = t.passenger_id
                   WHERE t.user_id = $1 AND DATE(t.occurred_at) = $2
                     AND t.occurred_at >= COALESCE($3, '2000-01-01'::timestamptz)
                   ORDER BY t.occurred_at""",
                db_user["id"], target, effective_cleared,
            )
            fuel = await db.fetchrow(
                """SELECT COALESCE(SUM(amount), 0) AS total, COALESCE(SUM(litres), 0) AS litres
                   FROM expenses WHERE user_id = $1 AND type = 'FUEL' AND DATE(occurred_at) = $2
                     AND occurred_at >= COALESCE($3, '2000-01-01'::timestamptz)""",
                db_user["id"], target, effective_cleared,
            )
            other_exp = await db.fetchrow(
                """SELECT COALESCE(SUM(amount), 0) AS total
                   FROM expenses WHERE user_id = $1 AND type != 'FUEL' AND DATE(occurred_at) = $2
                     AND occurred_at >= COALESCE($3, '2000-01-01'::timestamptz)""",
                db_user["id"], target, effective_cleared,
            )
            remit_row = await db.fetchrow(
                """SELECT amount, status FROM remittance_log
                   WHERE vehicle_id = $1 AND paid_on = $2""",
                vehicle["id"] if vehicle else 0, target,
            ) if vehicle else None
    except Exception:
        _logger.error("cmd_summary DB error for user %s target %s", uid, target, exc_info=True)
        await update.message.reply_text("Couldn't load summary — please try again in a moment.")
        return

    day_str = target.strftime("%A, %d %b %Y")
    lines = [f"📋 *{day_str}*\n"]

    total_trips = paid["cnt"] + unpaid["cnt"]
    if total_trips == 0:
        lines.append("No trips recorded this day.")
    else:
        lines.append(f"🚗 *Trips* ({total_trips})")
        if paid["cnt"] > 0:
            lines.append(f"  ✅ Paid: {paid['cnt']} — {format_currency(currency, paid['gross'])}")
        if unpaid["cnt"] > 0:
            lines.append(f"  ❌ Owed: {unpaid['cnt']} — {format_currency(currency, unpaid['gross'])}")
        for t in trip_rows[:20]:
            icon = "✅" if t["paid"] else "❌"
            parts = [f"  {icon} {format_currency(currency, t['amount'])}"]
            if t["destination"]:
                parts.append(t["destination"])
            if t["passenger"]:
                parts.append(f"({t['passenger']})")
            parts.append(t["occurred_at"].strftime("%H:%M"))
            lines.append(" · ".join(parts))
        if len(trip_rows) > 20:
            lines.append(f"  _...and {len(trip_rows) - 20} more_")

    if fuel["total"] > 0:
        fuel_line = f"\n⛽ Fuel: {format_currency(currency, fuel['total'])}"
        if fuel["litres"] > 0:
            fuel_line += f" ({fuel['litres']:.0f}L)"
        lines.append(fuel_line)
    if other_exp["total"] > 0:
        lines.append(f"🔧 Other costs: {format_currency(currency, other_exp['total'])}")

    if remit_row:
        status = remit_row["status"].lower()
        if status == "paid":
            lines.append(f"\n🏦 Remittance: ✅ {format_currency(currency, remit_row['amount'])} paid")
        elif status == "rest":
            lines.append("\n🏦 Remittance: 🏖️ rest day")
    elif vehicle:
        rate = await vehicle_svc.get_remittance_rate(vehicle["id"])
        if rate > 0:
            lines.append(f"\n🏦 Remittance: ⚠️ {format_currency(currency, rate)} not logged")

    total_costs = fuel["total"] + other_exp["total"]
    remit_paid = remit_row["amount"] if remit_row and remit_row["status"] == "PAID" else 0.0
    net = paid["gross"] - total_costs - remit_paid

    lines.append("\n──────────────────")
    sign = "+" if net >= 0 else ""
    lines.append(f"Net profit (paid): *{sign}{format_currency(currency, net)}*")
    if unpaid["gross"] > 0:
        incl = net + unpaid["gross"]
        sign2 = "+" if incl >= 0 else ""
        lines.append(f"Incl. owed: *{sign2}{format_currency(currency, incl)}*")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
