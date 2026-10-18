"""
policy.py — What the assistant may read or write, and how personal data is recognised.

One policy, enforced at several points (SQL reads, SQL writes, delete previews).
Two independent mechanisms:

  1. Restricted columns: a fixed list of columns that can never be read, filtered
     on, joined on, or copied. Enforced by the SQLite authorizer in sql_session.py,
     so aliases, CTEs, subqueries and SELECT * cannot hide them.
  2. Pattern detectors: recognise personal data by its shape (SSN, email, phone,
     card number). Used to scan data written to tables and to mask query results,
     as a second line of defence for values the column list does not cover.

Column names are matched case-insensitively.
"""

import re

# table -> columns that are personal data. Everything else is readable.
SENSITIVE_COLUMNS: dict[str, set[str]] = {
    "customers": {"name"},
    "customer_profiles": {
        "full_name",
        "email",
        "phone",
        "ssn",
        "date_of_birth",
        "street_address",
    },
}


def is_restricted(table: str, column: str) -> bool:
    return column.lower() in SENSITIVE_COLUMNS.get(table.lower(), set())


def restricted_columns(table: str) -> set[str]:
    return set(SENSITIVE_COLUMNS.get(table.lower(), set()))


# --- pattern detectors --------------------------------------------------------

_SSN = re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_PHONE = re.compile(
    r"(?<![\d-])(?:\+?1[\s.-]?)?(?:\(\d{3}\)\s?|\d{3}[\s.-])\d{3}[\s.-]\d{4}(?!\d)"
)
_CARD = re.compile(r"(?<![\d-])(?:\d[ -]?){12,18}\d(?![\d-])")


def _luhn_ok(digits: str) -> bool:
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _card_matches(text: str):
    for m in _CARD.finditer(text):
        digits = re.sub(r"\D", "", m.group())
        if _luhn_ok(digits):
            yield m


def find_pii(value) -> set[str]:
    """Return the kinds of personal data that `value` appears to contain."""
    if isinstance(value, bool) or value is None:
        return set()
    if isinstance(value, int):
        text = str(value)
    elif isinstance(value, str):
        text = value
    else:
        return set()

    kinds = set()
    if _SSN.search(text):
        kinds.add("ssn")
    if _EMAIL.search(text):
        kinds.add("email")
    if _PHONE.search(text):
        kinds.add("phone")
    if any(True for _ in _card_matches(text)):
        kinds.add("card")
    return kinds


def mask_value(value):
    """Mask personal data inside one result cell. Returns (value, kinds_found)."""
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>", set()
    if isinstance(value, int) and not isinstance(value, bool):
        if _luhn_ok(str(abs(value))):
            return "[REDACTED:card]", {"card"}
        return value, set()
    if not isinstance(value, str):
        return value, set()

    kinds: set[str] = set()
    text = value
    for kind, pattern in (("ssn", _SSN), ("email", _EMAIL), ("phone", _PHONE)):
        text, n = pattern.subn(f"[REDACTED:{kind}]", text)
        if n:
            kinds.add(kind)
    spans = [m.span() for m in _card_matches(text)]
    for start, end in reversed(spans):
        text = text[:start] + "[REDACTED:card]" + text[end:]
        kinds.add("card")
    return text, kinds
