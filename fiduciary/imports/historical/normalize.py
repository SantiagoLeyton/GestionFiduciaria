import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Any


MONTHS = {
    "ENE": 1,
    "FEB": 2,
    "MAR": 3,
    "ABR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AGO": 8,
    "SEP": 9,
    "SEPT": 9,
    "OCT": 10,
    "NOV": 11,
    "DIC": 12,
}


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def compact_normalized(value: Any) -> str:
    return normalize_text(value).replace(" ", "")


def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def parse_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        amount = value
    elif isinstance(value, int | float):
        amount = Decimal(str(value))
    else:
        text = str(value).strip()
        if not text:
            return None
        text = text.replace("$", "").replace(" ", "")
        if "," in text and "." in text:
            text = text.replace(".", "").replace(",", ".")
        elif "," in text:
            text = text.replace(",", ".")
        try:
            amount = Decimal(text)
        except InvalidOperation:
            return None
    if amount <= 0:
        return None
    return amount.quantize(Decimal("0.01"))


def parse_document_type(value: Any) -> str | None:
    normalized = compact_normalized(value)
    if not normalized:
        return None
    if normalized.startswith("nit"):
        return "nit"
    if normalized.startswith("cc") or "cedula" in normalized:
        return "cc"
    if normalized.startswith("ce"):
        return "ce"
    if "pasaporte" in normalized or normalized.startswith("pass"):
        return "passport"
    return None


def excel_column_name(index: int) -> str:
    name = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def excel_column_index(name: str) -> int:
    value = 0
    for char in name.upper():
        value = value * 26 + (ord(char) - 64)
    return value
