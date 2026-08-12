import math


def format_currency(currency: str, amount: float) -> str:
    if not math.isfinite(amount):
        return f"{currency}?"
    if amount == int(amount):
        return f"{currency}{int(amount):,}"
    return f"{currency}{amount:,.2f}"


def snapshot_to_hint(action_type: str, snapshot: dict) -> str:
    """Format a snapshot back into the text the user would have typed."""
    def _amt(v):
        return str(int(v)) if v == int(v) else str(v)

    if action_type == "trip":
        parts = [_amt(snapshot.get("amount", 0))]
        if snapshot.get("destination"):
            parts.append(snapshot["destination"])
        if snapshot.get("passenger_name"):
            parts.append(snapshot["passenger_name"])
        if not snapshot.get("paid", True):
            parts.append("unpaid")
        if snapshot.get("payment_method") == "TRANSFER":
            parts.append("transfer")
        return " ".join(parts)

    if action_type == "expense":
        etype = snapshot.get("expense_type", "other").lower()
        parts = [etype, _amt(snapshot.get("amount", 0))]
        if snapshot.get("litres"):
            parts.append(f"{snapshot['litres']:.0f}l")
        if snapshot.get("note"):
            parts.append(snapshot["note"])
        return " ".join(parts)

    if action_type == "remittance":
        if snapshot.get("status") == "REST":
            return "rest"
        return f"remit {_amt(snapshot.get('amount', 0))}"

    return ""


def format_log_label(action_type: str, snapshot: dict, currency: str) -> str:
    """One-line label for an action_log entry (used in edit buttons)."""
    if action_type == "trip":
        label = format_currency(currency, snapshot.get("amount", 0))
        if snapshot.get("destination"):
            label += f" → {snapshot['destination']}"
        if snapshot.get("passenger_name"):
            label += f" ({snapshot['passenger_name']})"
        if not snapshot.get("paid", True):
            label += " [unpaid]"
        return label
    if action_type == "expense":
        return f"{snapshot.get('expense_type', 'Expense').title()} {format_currency(currency, snapshot.get('amount', 0))}"
    if action_type == "remittance":
        if snapshot.get("status") == "REST":
            return "Rest day"
        return f"Remit {format_currency(currency, snapshot.get('amount', 0))}"
    return "Entry"
