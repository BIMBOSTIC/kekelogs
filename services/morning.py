import logging
import statistics
import pytz
from datetime import datetime, time
from telegram.ext import Application
from db.database import get_db
from utils.formatting import format_currency

_logger = logging.getLogger(__name__)


async def compute_breakeven(user_id: int, vehicle_id: int) -> dict:
    async with get_db() as db:
        rate_row = await db.fetchrow(
            """SELECT amount FROM remittance_rules
               WHERE vehicle_id = $1 AND effective_to IS NULL
               ORDER BY effective_from DESC LIMIT 1""",
            vehicle_id,
        )
        fuel_rows = await db.fetch(
            """SELECT DATE(occurred_at) AS day, SUM(amount) AS daily_fuel
               FROM expenses
               WHERE user_id = $1 AND type = 'FUEL'
               AND occurred_at >= NOW() - INTERVAL '14 days'
               GROUP BY DATE(occurred_at)""",
            user_id,
        )

    remit = rate_row["amount"] if rate_row else 0.0
    fuel_totals = sorted(r["daily_fuel"] for r in fuel_rows)
    median_fuel = statistics.median(fuel_totals) if fuel_totals else 0.0

    return {"remit": remit, "median_fuel": median_fuel, "breakeven": remit + median_fuel}


async def _send_morning_push(context) -> None:
    job = context.job
    d = job.data
    data = await compute_breakeven(d["user_db_id"], d["vehicle_id"])
    currency = d["currency"]

    lines = ["🌅 *Good morning!*\n\nToday's target:"]
    if data["remit"] > 0:
        lines.append(f"• Remittance: *{format_currency(currency, data['remit'])}*")
    if data["median_fuel"] > 0:
        lines.append(f"• Fuel (14-day avg): *~{format_currency(currency, data['median_fuel'])}*")
        lines.append(f"• Break-even: *{format_currency(currency, data['breakeven'])}*")
    elif data["remit"] > 0:
        lines.append(f"• Break-even: *{format_currency(currency, data['remit'])}*")
        lines.append("_Log `fuel 2000 32l` a few times to get a fuel estimate._")

    lines.append("\nGood luck out there 🚗")

    await context.bot.send_message(
        chat_id=d["telegram_id"],
        text="\n".join(lines),
        parse_mode="Markdown",
    )


def _parse_push_time(time_str: str, tz) -> time:
    parts = time_str.split(":")
    h = int(parts[0])
    m = int(parts[1]) if len(parts) > 1 else 0
    # Localize correctly via pytz.localize(), then convert to UTC.
    # Passing a pytz tz directly to time() gives wrong DST offsets.
    today = datetime.now(pytz.utc).date()
    local_dt = tz.localize(datetime(today.year, today.month, today.day, h, m))
    utc_dt = local_dt.astimezone(pytz.utc)
    return utc_dt.time().replace(tzinfo=pytz.utc)


def schedule_push(
    app: Application,
    telegram_id: int,
    user_db_id: int,
    vehicle_id: int,
    currency: str,
    timezone: str,
    push_time_str: str,
) -> None:
    job_name = f"morning_{telegram_id}"
    for job in app.job_queue.get_jobs_by_name(job_name):
        job.schedule_removal()

    try:
        tz = pytz.timezone(timezone)
    except pytz.UnknownTimeZoneError:
        tz = pytz.utc

    push_time = _parse_push_time(push_time_str, tz)
    app.job_queue.run_daily(
        _send_morning_push,
        time=push_time,
        data={"user_db_id": user_db_id, "telegram_id": telegram_id,
              "vehicle_id": vehicle_id, "currency": currency},
        name=job_name,
        chat_id=telegram_id,
    )


async def schedule_all_morning_pushes(app: Application) -> None:
    async with get_db() as db:
        rows = await db.fetch(
            """SELECT u.id, u.telegram_id, u.currency, u.timezone, u.morning_push_time,
                      v.id AS vehicle_id
               FROM users u
               LEFT JOIN vehicles v ON v.user_id = u.id AND v.is_active = 1
               WHERE u.morning_push = 1 AND u.onboarded = 1""",
        )

    for r in rows:
        if not r["vehicle_id"]:
            continue
        try:
            schedule_push(
                app,
                telegram_id=r["telegram_id"],
                user_db_id=r["id"],
                vehicle_id=r["vehicle_id"],
                currency=r["currency"],
                timezone=r["timezone"],
                push_time_str=r["morning_push_time"],
            )
        except Exception:
            _logger.exception("Failed to schedule morning push for user %s", r["telegram_id"])
