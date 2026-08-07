def format_currency(currency: str, amount: float) -> str:
    if amount == int(amount):
        return f"{currency}{int(amount):,}"
    return f"{currency}{amount:,.2f}"
