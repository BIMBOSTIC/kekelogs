import json
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from parser.nlp import parse_message
from services import users as user_svc
from services import vehicles as vehicle_svc
from utils.formatting import format_currency
from handlers.commands import (
    cmd_undo, cmd_redo, cmd_day, cmd_today, cmd_week, cmd_month,
    cmd_clients, cmd_owed, cmd_setremit, cmd_car, cmd_rest, cmd_morning,
    cmd_fuel, cmd_help, cmd_privacy, cmd_report,
)

# Single-word commands — route only when text is exactly this word
_EXACT_CMDS = {
    "undo": cmd_undo,
    "redo": cmd_redo,
    "today": cmd_today,
    "week": cmd_week,
    "month": cmd_month,
    "clients": cmd_clients,
    "owed": cmd_owed,
    "car": cmd_car,
    "rest": cmd_rest,
    "help": cmd_help,
    "privacy": cmd_privacy,
    "fuel": cmd_fuel,
}

# First-word commands — route when text starts with this word, pass remainder as args
_PREFIX_CMDS = {
    "day": cmd_day,
    "setremit": cmd_setremit,
    "morning": cmd_morning,
    "report": cmd_report,
}


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    text = update.message.text.strip()

    db_user = await user_svc.get_user(uid)
    if not db_user or not db_user["onboarded"]:
        await update.message.reply_text("Please set up your account first with /start")
        return

    # Command keyword routing — no slash needed
    lower = text.lower()
    if lower in _EXACT_CMDS:
        await _EXACT_CMDS[lower](update, ctx)
        return

    words = lower.split()
    if words[0] in _PREFIX_CMDS:
        ctx.args = text.split()[1:]
        await _PREFIX_CMDS[words[0]](update, ctx)
        return

    parsed = await parse_message(text)

    if not parsed:
        await update.message.reply_text(
            "I didn't understand that. Try:\n"
            "`450` — log a trip\n"
            "`fuel 2000` — log fuel\n"
            "`day 3600` — log full day\n"
            "`help` — all commands",
            parse_mode="Markdown",
        )
        return

    currency = db_user["currency"]
    entry_type = parsed["type"]

    if entry_type == "trip":
        await _confirm_trip(update, ctx, parsed, currency, db_user["id"])
    elif entry_type == "expense":
        await _confirm_expense(update, ctx, parsed, currency, db_user["id"])
    elif entry_type == "remittance":
        await _confirm_remittance(update, ctx, parsed, currency, db_user["id"])
    else:
        await update.message.reply_text(
            "I didn't understand that. Try:\n"
            "`450` — log a trip\n"
            "`450 kyrenia mrs adama unpaid` — unpaid trip\n"
            "`fuel 2000` — log fuel\n"
            "`/help` — all commands",
            parse_mode="Markdown",
        )


async def _confirm_trip(update, ctx, parsed, currency, user_db_id):
    amount = parsed["amount"]
    destination = parsed.get("destination")
    passenger = parsed.get("passenger")
    paid = parsed.get("paid", True)
    method = parsed.get("payment_method", "CASH")

    lines = [f"*{format_currency(currency, amount)}*"]
    if destination:
        lines.append(f"To: {destination}")
    if passenger:
        lines.append(f"Client: {passenger}")
    paid_str = ("Cash" if method == "CASH" else method.title()) + " ✅" if paid else "Unpaid ❌ (→ /owed)"
    lines.append(f"Paid: {paid_str}")

    ctx.user_data["pending"] = json.dumps({
        "type": "trip", "amount": amount,
        "destination": destination, "passenger": passenger,
        "paid": paid, "payment_method": method,
        "user_db_id": user_db_id,
    })

    await update.message.reply_text(
        "💰 *New trip*\n" + "\n".join(lines),
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✓ Save", callback_data="ct:trip"),
            InlineKeyboardButton("✗ Cancel", callback_data="cn:trip"),
        ]]),
        parse_mode="Markdown",
    )


async def _confirm_expense(update, ctx, parsed, currency, user_db_id):
    etype = parsed["expense_type"]
    amount = parsed["amount"]
    note = parsed.get("note")
    litres = parsed.get("litres")

    icons = {"FUEL": "⛽", "REPAIR": "🔧", "WASHING": "🚿", "FINE": "📋",
             "INSURANCE": "🛡️", "TYRE": "🔄", "ACCESSORY": "🔩", "OTHER": "📎"}
    icon = icons.get(etype, "💸")

    lines = [f"*{format_currency(currency, amount)}*", f"Type: {etype.title()}"]
    if note:
        lines.append(f"Note: {note}")
    if litres:
        lines.append(f"Litres: {litres}L — cost/L: {format_currency(currency, amount / litres)}")

    ctx.user_data["pending"] = json.dumps({
        "type": "expense", "expense_type": etype,
        "amount": amount, "note": note,
        "litres": litres, "odometer": parsed.get("odometer"),
        "user_db_id": user_db_id,
    })

    await update.message.reply_text(
        f"{icon} *{etype.title()} expense*\n" + "\n".join(lines),
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✓ Save", callback_data="ct:expense"),
            InlineKeyboardButton("✗ Cancel", callback_data="cn:expense"),
        ]]),
        parse_mode="Markdown",
    )


async def _confirm_remittance(update, ctx, parsed, currency, user_db_id):
    amount = parsed["amount"]

    ctx.user_data["pending"] = json.dumps({
        "type": "remittance", "amount": amount, "user_db_id": user_db_id,
    })

    await update.message.reply_text(
        f"🏦 *Remittance paid*\n*{format_currency(currency, amount)}*",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✓ Save", callback_data="ct:remittance"),
            InlineKeyboardButton("✗ Cancel", callback_data="cn:remittance"),
        ]]),
        parse_mode="Markdown",
    )
