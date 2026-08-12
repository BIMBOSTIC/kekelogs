from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes, ConversationHandler, CommandHandler,
    MessageHandler, CallbackQueryHandler, filters,
)
from services import users as user_svc
from services import vehicles as vehicle_svc

ASK_OWNERSHIP, ASK_PLATE, ASK_REMITTANCE, ASK_CURRENCY, ASK_TIMEZONE = range(5)

_OWNERSHIP_KB = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("🔑 Own it", callback_data="own:OWNED"),
        InlineKeyboardButton("📋 Renting", callback_data="own:RENTED"),
    ],
    [InlineKeyboardButton("💳 Hire Purchase", callback_data="own:HIRE_PURCHASE")],
])

_CURRENCY_KB = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("₺ TRY", callback_data="cur:₺"),
        InlineKeyboardButton("$ USD", callback_data="cur:$"),
    ],
    [
        InlineKeyboardButton("£ GBP", callback_data="cur:£"),
        InlineKeyboardButton("₦ NGN", callback_data="cur:₦"),
    ],
    [InlineKeyboardButton("Other — type it", callback_data="cur:other")],
])

_TZ_KB = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("Istanbul / KKTC", callback_data="tz:Europe/Istanbul"),
        InlineKeyboardButton("Lagos", callback_data="tz:Africa/Lagos"),
    ],
    [
        InlineKeyboardButton("London", callback_data="tz:Europe/London"),
        InlineKeyboardButton("New York", callback_data="tz:America/New_York"),
    ],
    [InlineKeyboardButton("Other — type it", callback_data="tz:other")],
])


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    db_user = await user_svc.get_user(user.id)

    if db_user and db_user["onboarded"]:
        await update.message.reply_text(
            "You're already set up! Use /car to see your vehicle, /help for all commands."
        )
        return ConversationHandler.END

    await user_svc.create_user(user.id, user.username)
    name = f", {user.first_name}" if user.first_name else ""

    await update.message.reply_text(
        f"👋 Welcome to Driver Ledger{name}!\n\n"
        "I track your trips, costs, and daily profit — and tell you your break-even "
        "before you start each shift.\n\n"
        "*Do you own the car or rent it?*",
        reply_markup=_OWNERSHIP_KB,
        parse_mode="Markdown",
    )
    return ASK_OWNERSHIP


_VALID_OWNERSHIPS = {"OWNED", "RENTED", "HIRE_PURCHASE"}


async def _ownership_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    ownership = q.data.split(":", 1)[1]
    if ownership not in _VALID_OWNERSHIPS:
        await q.edit_message_text("Invalid selection. Please try again.", reply_markup=_OWNERSHIP_KB)
        return ASK_OWNERSHIP
    ctx.user_data["ownership"] = ownership

    labels = {"OWNED": "own it", "RENTED": "rent it", "HIRE_PURCHASE": "have hire purchase"}
    await q.edit_message_text(
        f"Got it — you {labels[ownership]}.\n\n"
        "What's your *car plate or nickname?* (e.g. `34 ABC 123` or `Toyota Corolla`)",
        parse_mode="Markdown",
    )
    return ASK_PLATE


async def _plate_received(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data["plate"] = update.message.text.strip()
    ownership = ctx.user_data.get("ownership")

    if ownership == "OWNED":
        ctx.user_data["remittance"] = 0.0
        await update.message.reply_text(
            f"*{ctx.user_data['plate']}* — nice!\n\nWhat *currency* do you work in?",
            reply_markup=_CURRENCY_KB,
            parse_mode="Markdown",
        )
        return ASK_CURRENCY

    await update.message.reply_text(
        f"*{ctx.user_data['plate']}* — noted.\n\n"
        "How much do you pay the car owner *per day?* (just the number, e.g. `1050`)",
        parse_mode="Markdown",
    )
    return ASK_REMITTANCE


async def _remittance_received(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip().replace(",", "")
    try:
        amount = float(raw)
        if amount < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Please enter a valid number, e.g. `1050`", parse_mode="Markdown")
        return ASK_REMITTANCE

    ctx.user_data["remittance"] = amount
    await update.message.reply_text(
        f"Daily remittance: *{amount:,.0f}*\n\nWhat *currency* do you work in?",
        reply_markup=_CURRENCY_KB,
        parse_mode="Markdown",
    )
    return ASK_CURRENCY


async def _currency_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    val = q.data.split(":", 1)[1]

    if val == "other":
        ctx.user_data["awaiting_currency"] = True
        await q.edit_message_text("Type your currency symbol (e.g. `€`, `₹`, `GHS`):", parse_mode="Markdown")
        return ASK_CURRENCY

    ctx.user_data["currency"] = val
    await q.edit_message_text(
        f"Currency: *{val}*\n\nWhat *timezone* are you in?",
        reply_markup=_TZ_KB,
        parse_mode="Markdown",
    )
    return ASK_TIMEZONE


async def _currency_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not ctx.user_data.get("awaiting_currency"):
        return ASK_CURRENCY
    raw = update.message.text.strip()[:8]
    if not raw:
        await update.message.reply_text("Please enter a currency symbol (e.g. `€`, `₹`, `GHS`).", parse_mode="Markdown")
        return ASK_CURRENCY
    ctx.user_data["currency"] = raw
    ctx.user_data.pop("awaiting_currency", None)
    await update.message.reply_text(
        f"Currency: *{ctx.user_data['currency']}*\n\nWhat *timezone* are you in?",
        reply_markup=_TZ_KB,
        parse_mode="Markdown",
    )
    return ASK_TIMEZONE


async def _timezone_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    val = q.data.split(":", 1)[1]

    if val == "other":
        ctx.user_data["awaiting_tz"] = True
        await q.edit_message_text(
            "Type your timezone (e.g. `Asia/Dubai`, `America/Toronto`).\n"
            "See: en.wikipedia.org/wiki/List_of_tz_database_time_zones",
            parse_mode="Markdown",
        )
        return ASK_TIMEZONE

    ctx.user_data["timezone"] = val
    return await _finish(update, ctx, edit_fn=q.edit_message_text)


async def _timezone_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not ctx.user_data.get("awaiting_tz"):
        return ASK_TIMEZONE
    ctx.user_data["timezone"] = update.message.text.strip()
    ctx.user_data.pop("awaiting_tz", None)
    return await _finish(update, ctx, reply_fn=update.message.reply_text)


async def _finish(update: Update, ctx: ContextTypes.DEFAULT_TYPE, edit_fn=None, reply_fn=None) -> int:
    uid = update.effective_user.id
    data = ctx.user_data

    db_user = await user_svc.get_user(uid)
    await user_svc.update_user(
        uid,
        currency=data["currency"],
        timezone=data.get("timezone", "Europe/Istanbul"),
        onboarded=1,
    )

    existing_vehicle = await vehicle_svc.get_active_vehicle(db_user["id"])
    if not existing_vehicle:
        vehicle_id = await vehicle_svc.create_vehicle(
            user_id=db_user["id"],
            plate_or_nickname=data["plate"],
            ownership=data["ownership"],
        )
        remit = data.get("remittance", 0.0)
        if remit > 0:
            await vehicle_svc.create_remittance_rule(vehicle_id, remit)
    else:
        vehicle_id = existing_vehicle["id"]

    currency = data["currency"]
    remit_line = f"Daily remittance: *{currency}{remit:,.0f}*\n" if remit > 0 else ""

    msg = (
        f"*All set!* 🚗\n\n"
        f"Vehicle: *{data['plate']}*\n"
        f"{remit_line}"
        f"Currency: *{currency}*\n\n"
        f"*Quick guide:*\n"
        f"`450` — trip for {currency}450\n"
        f"`450 kyrenia` — trip with destination\n"
        f"`450 kyrenia mrs adama` — trip + client name\n"
        f"`fuel 2000` — fuel expense\n"
        f"`/day 3600` — log full day earnings\n"
        f"`/undo` — remove last entry\n"
        f"`/help` — all commands\n\n"
        f"Start typing a trip amount to log your first trip!"
    )

    send = edit_fn or reply_fn
    await send(msg, parse_mode="Markdown")
    ctx.user_data.clear()
    return ConversationHandler.END


async def _cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Setup cancelled. Use /start to begin again.")
    ctx.user_data.clear()
    return ConversationHandler.END


def build_start_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            ASK_OWNERSHIP: [CallbackQueryHandler(_ownership_cb, pattern="^own:")],
            ASK_PLATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, _plate_received)],
            ASK_REMITTANCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, _remittance_received)],
            ASK_CURRENCY: [
                CallbackQueryHandler(_currency_cb, pattern="^cur:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _currency_text),
            ],
            ASK_TIMEZONE: [
                CallbackQueryHandler(_timezone_cb, pattern="^tz:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, _timezone_text),
            ],
        },
        fallbacks=[CommandHandler("cancel", _cancel)],
        allow_reentry=True,
    )
