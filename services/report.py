import calendar
import io
from datetime import date, timedelta

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill

from db.database import get_db

_PERIOD_LABELS = {
    "today": "Today",
    "daily": "Today",
    "week": "Last 7 days",
    "weekly": "Last 7 days",
    "month": "This month",
    "monthly": "This month",
    "last_week": "Last week",
    "last_month": "Last month",
}

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2,
    "mar": 3, "march": 3, "apr": 4, "april": 4,
    "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}


def parse_report_period(text: str) -> tuple[date, date, str, str] | None:
    """Parse flexible period text → (start, end, display_label, filename_label) or None."""
    today = date.today()
    t = text.strip().lower()

    if t in ("today", "daily"):
        return today, today, "Today", today.strftime("%Y-%m-%d")

    if t in ("week", "weekly", "this week"):
        start = today - timedelta(days=6)
        return start, today, "Last 7 days", f"{start.strftime('%Y-%m-%d')}_to_{today.strftime('%Y-%m-%d')}"

    if t in ("last week", "last_week"):
        end = today - timedelta(days=7)
        start = end - timedelta(days=6)
        return start, end, "Last week", f"{start.strftime('%Y-%m-%d')}_to_{end.strftime('%Y-%m-%d')}"

    if t in ("month", "monthly", "this month"):
        start = date(today.year, today.month, 1)
        return start, today, today.strftime("%B %Y"), today.strftime("%Y-%m")

    if t in ("last month", "last_month"):
        first_this = date(today.year, today.month, 1)
        end = first_this - timedelta(days=1)
        start = date(end.year, end.month, 1)
        return start, end, end.strftime("%B %Y"), end.strftime("%Y-%m")

    # Named month: "august", "aug", "aug 2025", "august 2025"
    parts = t.split()
    month_num = _MONTHS.get(parts[0])
    if month_num:
        year = today.year
        if len(parts) == 2 and parts[1].isdigit() and len(parts[1]) == 4:
            year = int(parts[1])
        start = date(year, month_num, 1)
        if start > today:
            year -= 1
            start = date(year, month_num, 1)
        end_day = calendar.monthrange(year, month_num)[1]
        end = min(date(year, month_num, end_day), today)
        return start, end, start.strftime("%B %Y"), start.strftime("%Y-%m")

    return None


def _header_row(ws, headers: list[str]) -> None:
    ws.append(headers)
    fill = PatternFill("solid", fgColor="2E75B6")
    font = Font(bold=True, color="FFFFFF")
    align = Alignment(horizontal="center")
    for cell in ws[1]:
        cell.fill = fill
        cell.font = font
        cell.alignment = align


def _auto_width(ws) -> None:
    for col in ws.columns:
        max_len = max((len(str(cell.value or "")) for cell in col), default=8)
        ws.column_dimensions[col[0].column_letter].width = min(max_len + 4, 45)


async def build_report(
    user_id: int, vehicle_id: int,
    start: date, end: date, display_label: str, filename_label: str,
    currency: str, cleared_at=None,
) -> tuple[bytes, str]:
    effective_start = start
    if cleared_at:
        clear_date = cleared_at.date() if hasattr(cleared_at, "date") else cleared_at
        if clear_date > start:
            effective_start = clear_date

    async with get_db() as db:
        trips = await db.fetch(
            """SELECT t.occurred_at, t.amount, t.destination,
                      p.display_name AS passenger, t.paid, t.payment_method
               FROM trips t
               LEFT JOIN passengers p ON p.id = t.passenger_id
               WHERE t.user_id = $1
                 AND t.occurred_at::date >= $2
                 AND t.occurred_at::date <= $3
                 AND ($4::timestamptz IS NULL OR t.occurred_at >= $4)
               ORDER BY t.occurred_at""",
            user_id, effective_start, end, cleared_at,
        )
        expenses = await db.fetch(
            """SELECT occurred_at, type, amount, note, litres
               FROM expenses
               WHERE user_id = $1
                 AND occurred_at::date >= $2
                 AND occurred_at::date <= $3
                 AND ($4::timestamptz IS NULL OR occurred_at >= $4)
               ORDER BY occurred_at""",
            user_id, effective_start, end, cleared_at,
        )
        remittance = await db.fetch(
            """SELECT paid_on, amount, status
               FROM remittance_log
               WHERE vehicle_id = $1 AND paid_on >= $2 AND paid_on <= $3
                 AND ($4::timestamptz IS NULL OR created_at >= $4)
               ORDER BY paid_on""",
            vehicle_id, effective_start, end, cleared_at,
        )

    wb = openpyxl.Workbook()

    # ── Sheet 1: Trips ───────────────────────────────────────────────────────
    ws_trips = wb.active
    ws_trips.title = "Trips"
    _header_row(ws_trips, ["Date", "Time", f"Amount ({currency})", "Destination", "Client", "Paid", "Method"])
    for r in trips:
        ws_trips.append([
            r["occurred_at"].strftime("%Y-%m-%d"),
            r["occurred_at"].strftime("%H:%M"),
            float(r["amount"]),
            r["destination"] or "",
            r["passenger"] or "",
            "Yes" if r["paid"] else "No",
            r["payment_method"] or "CASH",
        ])
    _auto_width(ws_trips)

    # ── Sheet 2: Expenses ────────────────────────────────────────────────────
    ws_exp = wb.create_sheet("Expenses")
    _header_row(ws_exp, ["Date", "Time", "Type", f"Amount ({currency})", "Note", "Litres"])
    for r in expenses:
        ws_exp.append([
            r["occurred_at"].strftime("%Y-%m-%d"),
            r["occurred_at"].strftime("%H:%M"),
            r["type"].title(),
            float(r["amount"]),
            r["note"] or "",
            float(r["litres"]) if r["litres"] else "",
        ])
    _auto_width(ws_exp)

    # ── Sheet 3: Remittance ──────────────────────────────────────────────────
    ws_remit = wb.create_sheet("Remittance")
    _header_row(ws_remit, ["Date", f"Amount ({currency})", "Status"])
    for r in remittance:
        ws_remit.append([
            r["paid_on"].strftime("%Y-%m-%d"),
            float(r["amount"]) if r["status"] != "REST" else 0.0,
            r["status"].title(),
        ])
    _auto_width(ws_remit)

    # ── Sheet 4: Summary / Ledger ────────────────────────────────────────────
    ws_sum = wb.create_sheet("Summary")
    ws_sum.column_dimensions["A"].width = 36
    ws_sum.column_dimensions["B"].width = 20
    ws_sum.column_dimensions["C"].width = 26

    # Aggregate values
    exp_by_type: dict[str, float] = {}
    for r in expenses:
        exp_by_type[r["type"]] = exp_by_type.get(r["type"], 0.0) + float(r["amount"])

    paid_list = [r for r in trips if r["paid"]]
    unpaid_list = [r for r in trips if not r["paid"]]
    gross = sum(float(r["amount"]) for r in paid_list)
    unpaid_total = sum(float(r["amount"]) for r in unpaid_list)
    total_expenses = sum(exp_by_type.values())
    remit_paid = sum(float(r["amount"]) for r in remittance if r["status"] == "PAID")
    remit_days = sum(1 for r in remittance if r["status"] == "PAID")
    rest_days = sum(1 for r in remittance if r["status"] == "REST")
    net = gross - total_expenses - remit_paid

    paid_count = len(paid_list)
    unpaid_count = len(unpaid_list)
    working_days = len(set(r["occurred_at"].date() for r in paid_list)) if paid_list else 0
    avg_per_day = gross / working_days if working_days else 0.0
    avg_per_trip = gross / paid_count if paid_count else 0.0
    margin = net / gross * 100 if gross > 0 else 0.0

    # Styles
    _green_hdr  = PatternFill("solid", fgColor="375623")
    _orange_hdr = PatternFill("solid", fgColor="833C00")
    _grey_hdr   = PatternFill("solid", fgColor="595959")
    _navy_hdr   = PatternFill("solid", fgColor="1F4E79")
    _sub_fill   = PatternFill("solid", fgColor="D9E1F2")
    _net_fill   = PatternFill("solid", fgColor="E2EFDA" if net >= 0 else "FFDDC1")
    _wbold      = Font(bold=True, color="FFFFFF", size=11)
    _bold       = Font(bold=True)
    _bigbold    = Font(bold=True, size=12)
    _note_font  = Font(italic=True, color="767676", size=9)
    _cur_fmt    = f'"{currency}"#,##0.00'
    _indent     = Alignment(indent=2)

    _type_labels = {
        "FUEL": "Fuel", "REPAIR": "Repair / Maintenance", "WASHING": "Washing",
        "FINE": "Fine / Penalty", "INSURANCE": "Insurance",
        "TYRE": "Tyre", "ACCESSORY": "Accessory", "OTHER": "Other",
    }

    def _sec(title, fill):
        ws_sum.append([title, "", ""])
        r = ws_sum.max_row
        ws_sum.merge_cells(f"A{r}:C{r}")
        ws_sum.cell(r, 1).fill = fill
        ws_sum.cell(r, 1).font = _wbold
        ws_sum.cell(r, 1).alignment = Alignment(horizontal="left", indent=1)

    def _row(label, value="", note="", bold=False, big=False, cur=True, rfill=None):
        ws_sum.append([label, value if value != "" else None, note or None])
        r = ws_sum.max_row
        ws_sum.cell(r, 1).alignment = _indent
        ws_sum.cell(r, 3).font = _note_font
        if cur and isinstance(value, (int, float)):
            ws_sum.cell(r, 2).number_format = _cur_fmt
        fnt = _bigbold if big else (_bold if bold else None)
        if fnt:
            ws_sum.cell(r, 1).font = fnt
            ws_sum.cell(r, 2).font = fnt
        if rfill:
            for c in range(1, 4):
                ws_sum.cell(r, c).fill = rfill

    # Title block
    ws_sum.append([f"Driver Ledger  —  {display_label}", "", ""])
    ws_sum.merge_cells("A1:C1")
    ws_sum.cell(1, 1).font = Font(bold=True, size=15)
    ws_sum.cell(1, 1).alignment = Alignment(horizontal="left")
    ws_sum.append([f"Period:  {effective_start}  →  {end}", "", ""])
    ws_sum.cell(2, 1).font = Font(italic=True, color="595959")
    ws_sum.append([""])

    # INCOME
    _sec("INCOME", _green_hdr)
    _row("Gross Earnings  (paid trips)", gross, f"{paid_count} trips")
    if unpaid_total > 0:
        _row("Owed / Unpaid", unpaid_total, f"{unpaid_count} trips · not yet collected")
    ws_sum.append([""])
    _row("Working Days", working_days, "", cur=False)
    _row("Average Earnings / Day", avg_per_day, "paid trips only")
    _row("Average Earnings / Trip", avg_per_trip, "paid trips only")
    ws_sum.append([""])

    # EXPENSES
    _sec("EXPENSES", _orange_hdr)
    for etype, amount in sorted(exp_by_type.items(), key=lambda x: -x[1]):
        _row(_type_labels.get(etype, etype.title()), amount)
    if not exp_by_type:
        _row("No expenses recorded this period", "", "")
    _row("Total Expenses", total_expenses, "", bold=True, rfill=_sub_fill)
    ws_sum.append([""])

    # REMITTANCE
    _sec("REMITTANCE", _grey_hdr)
    _row("Days Paid", remit_days, "", cur=False)
    if rest_days:
        _row("Rest Days  (no charge)", rest_days, "", cur=False)
    _row("Total Remittance Paid", remit_paid, "", bold=True, rfill=_sub_fill)
    ws_sum.append([""])

    # PROFIT SUMMARY
    _sec("PROFIT SUMMARY", _navy_hdr)
    _row("Gross Earnings", gross)
    _row("Less:  Total Expenses", total_expenses)
    _row("Less:  Remittance Paid", remit_paid)
    ws_sum.append([""])
    _row("Net Profit", net, "", bold=True, big=True, rfill=_net_fill)
    if gross > 0:
        _row("Profit Margin", f"{margin:.1f}%", "net ÷ gross earnings", bold=True, cur=False)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.getvalue(), f"driver_ledger_{filename_label}.xlsx"
