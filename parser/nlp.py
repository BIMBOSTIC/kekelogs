import re
import json
import logging
from anthropic import AsyncAnthropic
from config import ANTHROPIC_API_KEY

_logger = logging.getLogger(__name__)

_VALID_EXPENSE_TYPES = {"FUEL", "REPAIR", "ACCESSORY", "WASHING", "FINE", "INSURANCE", "TYRE", "OTHER"}


def _valid_parsed(data: dict) -> bool:
    if not isinstance(data, dict):
        return False
    t = data.get("type")
    if t not in ("trip", "expense", "remittance"):
        return False
    if not isinstance(data.get("amount"), (int, float)):
        return False
    if t == "expense" and data.get("expense_type") not in _VALID_EXPENSE_TYPES:
        return False
    return True

_client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

_EXPENSE_LEAD = re.compile(
    r"^(fuel|petrol|gas|repair|fix|washing|wash|clean|fine|insurance|tyre|tire|accessory|accessories|remit|remittance)\b",
    re.IGNORECASE,
)
_AMOUNT_FIRST = re.compile(r"^(\d[\d,.]*)(.*)$")
_LITRES = re.compile(r"(\d+(?:\.\d+)?)\s*l(?:itres?|iters?)?$", re.IGNORECASE)
_ODOMETER = re.compile(r"(\d+(?:\.\d+)?)\s*km\b", re.IGNORECASE)
_UNPAID = re.compile(r"\b(unpaid|credit|owe|owed)\b", re.IGNORECASE)
_TRANSFER = re.compile(r"\b(transfer|bank)\b", re.IGNORECASE)
_DATE_ON = re.compile(r"(?:^|\s+)on\s+(\S+(?:\s+\S+)?)\s*$", re.IGNORECASE)

_EXPENSE_MAP = {
    "fuel": "FUEL", "petrol": "FUEL", "gas": "FUEL",
    "repair": "REPAIR", "fix": "REPAIR",
    "washing": "WASHING", "wash": "WASHING", "clean": "WASHING",
    "fine": "FINE",
    "insurance": "INSURANCE",
    "tyre": "TYRE", "tire": "TYRE",
    "accessory": "ACCESSORY", "accessories": "ACCESSORY",
}

_CLAUDE_SYSTEM = """\
You are a parser for a driver earnings/expense tracking bot. Parse the message into JSON.
Output ONLY valid JSON, no explanation.

Schemas:
Trip: {"type":"trip","amount":450,"destination":null,"passenger":null,"paid":true,"payment_method":"CASH"}
Expense: {"type":"expense","expense_type":"FUEL","amount":2000,"note":null,"litres":null,"odometer":null}
  expense_type options: FUEL REPAIR ACCESSORY WASHING FINE INSURANCE TYRE OTHER
Remittance: {"type":"remittance","amount":1050}

Rules:
- A bare number is ALWAYS a trip
- fuel/petrol/gas → FUEL; repair/fix → REPAIR; washing/wash → WASHING; remit/remittance → remittance
- Word after amount = destination (title case); words after destination = passenger name
- unpaid/credit/owe → paid:false; transfer/bank → payment_method:"TRANSFER"
- 32l or 32 litres suffix → litres field; km suffix → odometer field
"""


def _parse_amount(raw: str) -> float | None:
    cleaned = raw.replace(",", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _fast_parse(text: str) -> dict | None:
    text = text.strip()

    # Remittance: "remit 1050" or "remittance 1050"
    em = _EXPENSE_LEAD.match(text)
    if em:
        kw = em.group(1).lower()
        if kw in ("remit", "remittance"):
            m = re.search(r"\d[\d,.]*", text[em.end():])
            if m:
                amt = _parse_amount(m.group())
                if amt is not None:
                    date_str = text[em.end() + m.end():].strip() or None
                    return {"type": "remittance", "amount": amt, "date_str": date_str}
            return None

        expense_type = _EXPENSE_MAP.get(kw, "OTHER")
        remainder = text[em.end():].strip()
        am = re.search(r"\d[\d,.]*", remainder)
        if not am:
            return None
        amount = _parse_amount(am.group())
        if amount is None:
            return None

        tail = remainder[am.end():].strip()
        litres = odometer = note = None

        # Extract odometer first (unanchored) so its removal leaves litres at tail end
        om = _ODOMETER.search(tail)
        if om:
            odometer = float(om.group(1))
            tail = (tail[:om.start()] + tail[om.end():]).strip()

        lm = _LITRES.search(tail)
        if lm:
            litres = float(lm.group(1))
            tail = tail[: lm.start()].strip()

        if tail:
            note = tail

        return {
            "type": "expense",
            "expense_type": expense_type,
            "amount": amount,
            "note": note,
            "litres": litres,
            "odometer": odometer,
        }

    # Trip: number [destination [passenger...]]
    am = _AMOUNT_FIRST.match(text)
    if am:
        amount = _parse_amount(am.group(1))
        if amount is None:
            return None

        remainder = am.group(2).strip()
        paid = True
        payment_method = "CASH"
        date_str = None

        dom = _DATE_ON.search(remainder)
        if dom:
            date_str = dom.group(1).strip()
            remainder = remainder[:dom.start()].strip()

        if _UNPAID.search(remainder):
            paid = False
            remainder = _UNPAID.sub("", remainder).strip()

        if _TRANSFER.search(remainder):
            payment_method = "TRANSFER"
            remainder = _TRANSFER.sub("", remainder).strip()

        parts = remainder.split() if remainder else []
        destination = parts[0].title() if parts else None
        passenger = " ".join(parts[1:]).title() if len(parts) > 1 else None

        return {
            "type": "trip",
            "amount": amount,
            "destination": destination,
            "passenger": passenger,
            "paid": paid,
            "payment_method": payment_method,
            "date_str": date_str,
        }

    return None


async def parse_message(text: str) -> dict | None:
    fast = _fast_parse(text)
    if fast is not None:
        return fast
    try:
        resp = await _client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=_CLAUDE_SYSTEM,
            messages=[{"role": "user", "content": text}],
        )
        if not resp.content or not hasattr(resp.content[0], "text"):
            return None
        result = json.loads(resp.content[0].text.strip())
        if not _valid_parsed(result):
            _logger.warning("Claude returned invalid schema: %r", result)
            return None
        return result
    except Exception:
        _logger.error("Claude parse failed for input %r", text[:80], exc_info=True)
        return None
