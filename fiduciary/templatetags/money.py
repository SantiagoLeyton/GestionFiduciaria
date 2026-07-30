from decimal import Decimal, InvalidOperation

from django import template


register = template.Library()


@register.filter
def money_co(value):
    if value is None or value == "":
        return "-"
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return value
    sign = "-" if amount < 0 else ""
    amount = abs(amount)
    integral = int(amount)
    decimals = amount - integral
    formatted = f"{integral:,}".replace(",", ".")
    if decimals:
        decimal_text = f"{decimals:.2f}".split(".")[1].rstrip("0")
        if decimal_text:
            return f"{sign}{formatted},{decimal_text}"
    return f"{sign}{formatted}"
