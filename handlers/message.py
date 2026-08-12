import json
import time
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from parser.nlp import parse_message
from services import users as user_svc
from services import vehicles as vehicle_svc
from utils.formatting import format_currency
from handlers.commands import (
    cmd_undo, cmd_redo, cmd_day, cmd_today, cmd_week, cmd_month,
    cmd_clients, cmd_owed, cmd_setremit, cmd_car, cmd_rest, cmd_morning,
    cmd_fuel, cmd_help, cmd_privacy, cmd_report, cmd_edit, cmd_clear,
)
from utils.formatting import snapshot_to_hint

_PARSE_RATE_LIMIT = 10  # max parse_message calls per user per 60 seconds

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
    "edit": cmd_edit,
    "clear": cmd_clear,
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

    # Rate-limit parse_message calls (applies to both edit replies and free text)
    now = time.monotonic()
    bucket = [t for t in ctx.user_data.get("_rl_calls", []) if now - t < 60]
    if len(bucket) >= _PARSE_RATE_LIMIT:
        await update.message.reply_text("Too many requests — please wait a moment.")
        return
    ctx.user_data["_rl_calls"] = bucket + [now]

    # Editing state: user tapped an entry and must send corrected text
    if ctx.user_data.get("editing"):
        await _handle_edit_reply(update, ctx, db_user)
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


async def _handle_edit_reply(update, ctx, db_user: dict) -> None:
    text = update.message.text.strip()
    editing = ctx.user_data["editing"]

    if text.lower() in ("cancel", "/cancel"):
        ctx.user_data.pop("editing", None)
        ctx.user_data.pop("pending_edit", None)
        await update.message.reply_text("Edit cancelled.")
        return

    parsed = await parse_message(text)
    if not parsed:
        await update.message.reply_text(
            "Couldn't parse that. Try again or type `cancel` to exit.",
            parse_mode="Markdown",
        )
        return

    atype = editing["action_type"]
    if parsed["type"] != atype:
        hints = {
            "trip": "a trip amount e.g. `500 kyrenia`",
            "expense": "an expense e.g. `fuel 2000`",
            "remittance": "a remittance e.g. `remit 1050`",
        }
        await update.message.reply_text(
            f"This entry is a *{atype}* — send {hints.get(atype, 'the correct type')}.\n"
            "Type `cancel` to exit.",
            parse_mode="Markdown",
        )
        return

    currency = db_user["currency"]
    ctx.user_data["pending_edit"] = {**editing, "new_parsed": parsed}

    if atype == "trip":
        lines = [f"*{format_currency(currency, parsed['amount'])}*"]
        if parsed.get("destination"):
            lines.append(f"To: {parsed['destination']}")
        if parsed.get("passenger"):
            lines.append(f"Client: {parsed['passenger']}")
        lines.append("Paid: Yes" if parsed.get("paid", True) else "Paid: No (unpaid)")
        old = snapshot_to_hint(atype, editing["old_snapshot"])
        content = f"Old: `{old}`\nNew:\n" + "\n".join(lines)
    elif atype == "expense":
        old = snapshot_to_hint(atype, editing["old_snapshot"])
        content = (
            f"Old: `{old}`\n"
            f"New: *{parsed['expense_type'].title()}* — {format_currency(currency, parsed['amount'])}"
        )
        if parsed.get("litres"):
            content += f"  ({parsed['litres']}L)"
    else:
        old = snapshot_to_hint(atype, editing["old_snapshot"])
        content = f"Old: `{old}`\nNew: Remit *{format_currency(currency, parsed['amount'])}*"

    await update.message.reply_text(
        f"✏️ *Save edit?*\n\n{content}",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✓ Save", callback_data="edit_ok"),
            InlineKeyboardButton("✗ Cancel", callback_data="edit_no"),
        ]]),
        parse_mode="Markdown",
    )
