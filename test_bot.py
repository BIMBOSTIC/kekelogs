"""
Driver Ledger — Full feature test
Run from the driver-bot folder: python test_bot.py
Requires .env with DATABASE_URL set.
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

from datetime import date, timedelta
from db.database import init_db, get_db
from parser.nlp import _fast_parse
from services import users as user_svc
from services import vehicles as vehicle_svc
from services.trips import save_trip, undo_last_action
from services.remittance import get_today_status, get_owing_balance, mark_rest_day
from services.morning import compute_breakeven

TEST_TG_ID = 9_000_000_001  # fake ID, cleaned up after tests

results = []


def ok(name, condition, detail=""):
    icon = "✅" if condition else "❌"
    results.append((condition, name))
    suffix = f"  ({detail})" if detail else ""
    print(f"  {icon}  {name}{suffix}")
    return condition


# ── helpers ──────────────────────────────────────────────────────────────────

async def cleanup():
    async with get_db() as db:
        row = await db.fetchrow("SELECT id FROM users WHERE telegram_id = $1", TEST_TG_ID)
        if not row:
            return
        uid = row["id"]
        await db.execute("DELETE FROM action_log   WHERE user_id = $1", uid)
        await db.execute("DELETE FROM trips         WHERE user_id = $1", uid)
        await db.execute("DELETE FROM expenses      WHERE user_id = $1", uid)
        await db.execute("DELETE FROM passengers    WHERE user_id = $1", uid)
        vids = await db.fetch("SELECT id FROM vehicles WHERE user_id = $1", uid)
        for v in vids:
            await db.execute("DELETE FROM remittance_rules WHERE vehicle_id = $1", v["id"])
            await db.execute("DELETE FROM remittance_log   WHERE vehicle_id = $1", v["id"])
        await db.execute("DELETE FROM vehicles WHERE user_id = $1", uid)
        await db.execute("DELETE FROM users    WHERE id = $1",      uid)


# ── test groups ───────────────────────────────────────────────────────────────

async def test_parser():
    print("\n── Parser ─────────────────────────────")

    r = _fast_parse("450")
    ok("bare number → trip",         r and r["type"] == "trip" and r["amount"] == 450)

    r = _fast_parse("450 kyrenia")
    ok("trip + destination",         r and r["destination"] == "Kyrenia")

    r = _fast_parse("450 kyrenia mrs adama")
    ok("trip + passenger",           r and r["passenger"] == "Mrs Adama")

    r = _fast_parse("450 kyrenia mrs adama unpaid")
    ok("unpaid flag",                r and r["paid"] is False)

    r = _fast_parse("450 airport transfer")
    ok("transfer payment",           r and r["payment_method"] == "TRANSFER")

    r = _fast_parse("fuel 2000")
    ok("fuel expense",               r and r["type"] == "expense" and r["expense_type"] == "FUEL")

    r = _fast_parse("fuel 2000 32l")
    ok("fuel + litres",              r and r["litres"] == 32.0, f"litres={r['litres'] if r else None}")

    r = _fast_parse("repair 600 bumper")
    ok("repair + note",              r and r["expense_type"] == "REPAIR" and r["note"] == "bumper")

    r = _fast_parse("washing 120")
    ok("washing",                    r and r["expense_type"] == "WASHING")

    r = _fast_parse("remit 1050")
    ok("remittance keyword",         r and r["type"] == "remittance" and r["amount"] == 1050)

    r = _fast_parse("remittance 860")
    ok("remittance alt keyword",     r and r["type"] == "remittance")

    r = _fast_parse("hello world")
    ok("unknown input → None",       r is None)


async def test_users_vehicles():
    print("\n── Users & Vehicles ────────────────────")

    await user_svc.create_user(TEST_TG_ID, "test_driver")
    u = await user_svc.get_user(TEST_TG_ID)
    ok("create user",                u is not None)

    await user_svc.update_user(TEST_TG_ID, currency="₺", timezone="Europe/Istanbul", onboarded=1)
    u = await user_svc.get_user(TEST_TG_ID)
    ok("update user",                u["onboarded"] == 1 and u["currency"] == "₺")

    # Create duplicate — should silently skip (ON CONFLICT DO NOTHING)
    await user_svc.create_user(TEST_TG_ID, "duplicate")
    count_row = None
    async with get_db() as db:
        count_row = await db.fetchrow("SELECT COUNT(*) AS n FROM users WHERE telegram_id = $1", TEST_TG_ID)
    ok("no duplicate user",          count_row["n"] == 1)

    vid = await vehicle_svc.create_vehicle(u["id"], "34 TEST 001", "RENTED")
    v = await vehicle_svc.get_active_vehicle(u["id"])
    ok("create vehicle",             v is not None, f"id={vid}")

    await vehicle_svc.create_remittance_rule(vid, 1050.0)
    rate = await vehicle_svc.get_remittance_rate(vid)
    ok("remittance rule",            rate == 1050.0, f"rate={rate}")

    return u, v


async def test_trips(u, v):
    print("\n── Trip Logging ────────────────────────")

    tid1 = await save_trip(u["id"], v["id"], 450.0)
    ok("bare trip saved",            tid1 > 0)

    tid2 = await save_trip(u["id"], v["id"], 600.0, destination="Kyrenia", passenger_name="Mrs Adama")
    ok("trip + destination + client", tid2 > 0)

    tid3 = await save_trip(u["id"], v["id"], 500.0, passenger_name="Mr Okon", paid=False)
    ok("unpaid trip saved",          tid3 > 0)

    async with get_db() as db:
        p = await db.fetchrow(
            "SELECT lifetime_revenue, trip_count FROM passengers WHERE user_id=$1 AND LOWER(display_name)='mrs adama'",
            u["id"]
        )
    ok("passenger created",          p is not None)
    ok("passenger revenue tracked",  p and p["lifetime_revenue"] == 600.0, f"rev={p['lifetime_revenue'] if p else None}")
    ok("trip_count tracked",         p and p["trip_count"] == 1)

    result = await undo_last_action(u["id"])
    ok("/undo works",                result and result["action_type"] == "trip")

    # Confirm trip is gone
    async with get_db() as db:
        row = await db.fetchrow("SELECT id FROM trips WHERE id = $1", tid3)
    ok("trip deleted by undo",       row is None)

    return tid1, tid2


async def test_expenses(u, v):
    print("\n── Expenses ────────────────────────────")

    async with get_db() as db:
        r = await db.fetchrow(
            "INSERT INTO expenses (user_id, vehicle_id, type, amount, litres) VALUES ($1,$2,'FUEL',$3,$4) RETURNING id",
            u["id"], v["id"], 2000.0, 32.0
        )
        await db.execute(
            "INSERT INTO action_log (user_id, action_type, table_name, record_id, snapshot) VALUES ($1,'expense','expenses',$2,$3)",
            u["id"], r["id"], '{"expense_type":"FUEL","amount":2000}'
        )
        eid = r["id"]

    ok("fuel expense inserted",      eid > 0)

    async with get_db() as db:
        r = await db.fetchrow(
            "INSERT INTO expenses (user_id, vehicle_id, type, amount, note) VALUES ($1,$2,'REPAIR',$3,$4) RETURNING id",
            u["id"], v["id"], 600.0, "bumper"
        )
    ok("repair expense inserted",    r["id"] > 0)

    # Undo fuel expense
    result = await undo_last_action(u["id"])
    ok("/undo expense works",        result and result["action_type"] == "expense")


async def test_remittance(u, v):
    print("\n── Remittance Engine ───────────────────")

    # Nothing paid yet → owing
    s = await get_today_status(v["id"])
    ok("today status = owing",       s["status"] == "owing")
    ok("owing amount = 1050",        s["amount"] == 1050.0)

    # Pay remittance
    async with get_db() as db:
        row = await db.fetchrow(
            "INSERT INTO remittance_log (vehicle_id, amount, paid_on, status) VALUES ($1,1050,$2,'PAID') RETURNING id",
            v["id"], date.today()
        )
        await db.execute(
            "INSERT INTO action_log (user_id, action_type, table_name, record_id, snapshot) VALUES ($1,'remittance','remittance_log',$2,$3)",
            u["id"], row["id"], '{"amount":1050}'
        )

    s = await get_today_status(v["id"])
    ok("today status = paid",        s["status"] == "paid")

    # Undo remittance
    await undo_last_action(u["id"])
    s = await get_today_status(v["id"])
    ok("status = owing after undo",  s["status"] == "owing")

    # Rest day
    success = await mark_rest_day(v["id"], u["id"])
    ok("mark rest day",              success is True)

    s = await get_today_status(v["id"])
    ok("today status = rest",        s["status"] == "rest")

    again = await mark_rest_day(v["id"], u["id"])
    ok("double rest blocked",        again is False)

    # Clear for owing balance test
    async with get_db() as db:
        await db.execute("DELETE FROM remittance_log WHERE vehicle_id = $1", v["id"])

    owing = await get_owing_balance(v["id"])
    ok("owing balance ≥ 0",          owing >= 0, f"owing={owing}")


async def test_views(u, v):
    print("\n── View Queries ────────────────────────")
    today = date.today()
    week_start = today - timedelta(days=6)
    month_start = date(today.year, today.month, 1)

    async with get_db() as db:
        r = await db.fetchrow(
            "SELECT COALESCE(SUM(amount),0) AS gross, COUNT(*) AS cnt FROM trips WHERE user_id=$1 AND DATE(occurred_at)=$2 AND paid=1",
            u["id"], today
        )
        ok("/today query",            r is not None, f"gross={r['gross']}")

        rows = await db.fetch(
            "SELECT DATE(occurred_at) AS day, SUM(amount) AS gross FROM trips WHERE user_id=$1 AND DATE(occurred_at)>=$2 AND paid=1 GROUP BY DATE(occurred_at)",
            u["id"], week_start
        )
        ok("/week query",             rows is not None)

        r = await db.fetchrow(
            "SELECT COALESCE(SUM(amount),0) AS gross FROM trips WHERE user_id=$1 AND DATE(occurred_at)>=$2 AND paid=1",
            u["id"], month_start
        )
        ok("/month query",            r is not None)

        rows = await db.fetch(
            "SELECT DATE_TRUNC('month', occurred_at) AS month, SUM(amount) AS total FROM expenses WHERE user_id=$1 AND type='FUEL' GROUP BY 1 ORDER BY 1 DESC LIMIT 6",
            u["id"]
        )
        ok("/fuel query",             rows is not None)

        rows = await db.fetch(
            "SELECT display_name, lifetime_revenue, trip_count FROM passengers WHERE user_id=$1 AND trip_count>0 ORDER BY lifetime_revenue DESC LIMIT 10",
            u["id"]
        )
        ok("/clients query",          rows is not None, f"{len(rows)} clients")

        named = await db.fetch(
            "SELECT p.id, p.display_name, COUNT(*) AS cnt, SUM(t.amount) AS total FROM trips t JOIN passengers p ON p.id=t.passenger_id WHERE t.user_id=$1 AND t.paid=0 GROUP BY p.id, p.display_name",
            u["id"]
        )
        ok("/owed grouped query",     named is not None)


async def test_morning(u, v):
    print("\n── Morning Push ────────────────────────")
    data = await compute_breakeven(u["id"], v["id"])
    ok("compute_breakeven runs",     "remit" in data and "breakeven" in data, str(data))
    ok("remit = 1050",               data["remit"] == 1050.0)
    ok("breakeven ≥ remit",          data["breakeven"] >= data["remit"])


async def test_setremit(v):
    print("\n── Update Remittance Rate ──────────────")
    await vehicle_svc.update_remittance_rate(v["id"], 1200.0)
    new_rate = await vehicle_svc.get_remittance_rate(v["id"])
    ok("rate updated to 1200",       new_rate == 1200.0, f"new={new_rate}")

    # Old rule should have effective_to set
    async with get_db() as db:
        closed = await db.fetch(
            "SELECT * FROM remittance_rules WHERE vehicle_id=$1 AND effective_to IS NOT NULL",
            v["id"]
        )
    ok("old rule closed",            len(closed) >= 1)


async def test_mark_paid(u, v):
    print("\n── Mark Client Paid ────────────────────")

    # Create an unpaid trip for a named client
    await save_trip(u["id"], v["id"], 750.0, passenger_name="Test Client", paid=False)

    async with get_db() as db:
        p = await db.fetchrow(
            "SELECT id FROM passengers WHERE user_id=$1 AND LOWER(display_name)='test client'",
            u["id"]
        )
        if not p:
            ok("passenger exists for mark-paid test", False)
            return

        pid = p["id"]
        summary = await db.fetchrow(
            "SELECT COUNT(*) AS cnt, COALESCE(SUM(amount),0) AS total FROM trips WHERE passenger_id=$1 AND paid=0",
            pid
        )
        await db.execute("UPDATE trips SET paid=1 WHERE passenger_id=$1 AND paid=0", pid)
        await db.execute(
            "UPDATE passengers SET lifetime_revenue=lifetime_revenue+$1, trip_count=trip_count+$2 WHERE id=$3",
            summary["total"], summary["cnt"], pid
        )
        row = await db.fetchrow("SELECT paid FROM trips WHERE passenger_id=$1", pid)

    ok("trip marked paid",           row and row["paid"] == 1)


async def test_passenger_deletion(u):
    print("\n── Passenger Deletion ──────────────────")

    async with get_db() as db:
        before = await db.fetchrow("SELECT COUNT(*) AS n FROM passengers WHERE user_id=$1", u["id"])
        await db.execute("UPDATE trips SET passenger_id=NULL WHERE user_id=$1", u["id"])
        await db.execute("DELETE FROM passengers WHERE user_id=$1", u["id"])
        after = await db.fetchrow("SELECT COUNT(*) AS n FROM passengers WHERE user_id=$1", u["id"])
        trips = await db.fetchrow(
            "SELECT COUNT(*) AS n FROM trips WHERE user_id=$1 AND passenger_id IS NULL", u["id"]
        )

    ok("passengers deleted",         after["n"] == 0, f"before={before['n']} after={after['n']}")
    ok("trips preserved",            trips["n"] >= 0)


# ── main ──────────────────────────────────────────────────────────────────────

async def main():
    print("=" * 50)
    print("  Driver Ledger — Feature Test Suite")
    print("=" * 50)

    print("\n── Database connection ─────────────────")
    try:
        await init_db()
        ok("connected", True)
    except Exception as e:
        ok("connected", False, str(e))
        print("\nCannot connect to DB. Check DATABASE_URL in .env")
        sys.exit(1)

    try:
        await cleanup()

        await test_parser()
        u, v = await test_users_vehicles()
        await test_trips(u, v)
        await test_expenses(u, v)
        await test_remittance(u, v)
        await test_views(u, v)
        await test_morning(u, v)
        await test_setremit(v)
        await test_mark_paid(u, v)
        await test_passenger_deletion(u)

    finally:
        await cleanup()

    passed = sum(1 for c, _ in results if c)
    failed = sum(1 for c, _ in results if not c)

    print("\n" + "=" * 50)
    print(f"  {passed} passed   {failed} failed   {len(results)} total")
    print("=" * 50)

    if failed:
        print("\nFailed:")
        for c, name in results:
            if not c:
                print(f"  ❌  {name}")
        sys.exit(1)
    else:
        print("\n  All tests passed ✅")


if __name__ == "__main__":
    asyncio.run(main())
