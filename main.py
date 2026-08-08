import logging
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters,
)
from config import TELEGRAM_TOKEN, LOG_LEVEL
from db.database import init_db
from handlers.start import build_start_handler
from handlers.message import handle_text
from handlers.callbacks import handle_confirm, handle_cancel, handle_delete_confirm
from handlers.commands import (
    cmd_undo, cmd_day, cmd_today, cmd_week, cmd_month,
    cmd_clients, cmd_owed, cmd_car, cmd_rest, cmd_fuel, cmd_help,
    cmd_privacy, cmd_deleteme,
)

logging.basicConfig(
    format="%(asctime)s — %(name)s — %(levelname)s — %(message)s",
    level=getattr(logging, LOG_LEVEL, logging.INFO),
)
logger = logging.getLogger(__name__)


async def _post_init(app: Application) -> None:
    await init_db()
    logger.info("Database ready")


def main() -> None:
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(_post_init).build()

    app.add_handler(build_start_handler())

    app.add_handler(CommandHandler("undo", cmd_undo))
    app.add_handler(CommandHandler("day", cmd_day))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("week", cmd_week))
    app.add_handler(CommandHandler("month", cmd_month))
    app.add_handler(CommandHandler("clients", cmd_clients))
    app.add_handler(CommandHandler("owed", cmd_owed))
    app.add_handler(CommandHandler("car", cmd_car))
    app.add_handler(CommandHandler("rest", cmd_rest))
    app.add_handler(CommandHandler("fuel", cmd_fuel))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("privacy", cmd_privacy))
    app.add_handler(CommandHandler("deleteme", cmd_deleteme))

    app.add_handler(CallbackQueryHandler(handle_confirm, pattern="^ct:"))
    app.add_handler(CallbackQueryHandler(handle_cancel, pattern="^cn:"))
    app.add_handler(CallbackQueryHandler(handle_delete_confirm, pattern="^delete:"))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Starting Driver Ledger bot (polling)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
