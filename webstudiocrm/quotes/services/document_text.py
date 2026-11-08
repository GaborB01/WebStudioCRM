"""Fixed texts and number/date formats of quote documents (English, Hungarian).

Kept in one plain dictionary instead of Django's .po translation files: the
documents need exactly two languages, regardless of the admin's language,
and this needs no gettext tooling to edit.
"""

from decimal import Decimal

from .calculator import CURRENCY_DECIMALS

LABELS = {
    "en": {
        "quote": "Quote",
        "draft": "DRAFT",
        "number": "Quote number",
        "version": "Version",
        "issue_date": "Issue date",
        "valid_until": "Valid until",
        "payment_terms": "Payment terms",
        "days": "days",
        "from": "From",
        "to": "To",
        "attention": "Attn.",
        "tax_number": "Tax number",
        "eu_vat_number": "EU VAT number",
        "registration_number": "Reg. number",
        "description": "Description",
        "quantity": "Qty",
        "unit_price": "Unit price",
        "discount": "Discount",
        "vat": "VAT",
        "net": "Net",
        "one_off": "One-off items",
        "recurring": "Recurring fees",
        "optional": "Optional items (not included in the total)",
        "subtotal": "Subtotal (net)",
        "quote_discount": "Discount",
        "net_total": "Total (net)",
        "gross_total": "Total (gross)",
        "per_month": "per month",
        "per_year": "per year",
        "plus_vat": "net, plus VAT",
        "bank": "Bank details",
        "account_number": "Account number",
        "terms": "Terms",
        "units": {
            "hour": "hour", "day": "day", "piece": "pc", "month": "month",
            "year": "year", "package": "package",
        },
        "months": [
            "January", "February", "March", "April", "May", "June", "July",
            "August", "September", "October", "November", "December",
        ],
    },
    "hu": {
        "quote": "Árajánlat",
        "draft": "PISZKOZAT",
        "number": "Ajánlat száma",
        "version": "Verzió",
        "issue_date": "Kelte",
        "valid_until": "Érvényes",
        "payment_terms": "Fizetési határidő",
        "days": "nap",
        "from": "Ajánlattevő",
        "to": "Megrendelő",
        "attention": "Címzett",
        "tax_number": "Adószám",
        "eu_vat_number": "Közösségi adószám",
        "registration_number": "Cégjegyzékszám",
        "description": "Megnevezés",
        "quantity": "Menny.",
        "unit_price": "Egységár",
        "discount": "Kedv.",
        "vat": "ÁFA",
        "net": "Nettó",
        "one_off": "Egyszeri tételek",
        "recurring": "Rendszeres díjak",
        "optional": "Opcionális tételek (a végösszeg nem tartalmazza)",
        "subtotal": "Részösszeg (nettó)",
        "quote_discount": "Kedvezmény",
        "net_total": "Összesen (nettó)",
        "gross_total": "Összesen (bruttó)",
        "per_month": "havonta",
        "per_year": "évente",
        "plus_vat": "nettó, + ÁFA",
        "bank": "Bankszámla",
        "account_number": "Számlaszám",
        "terms": "Feltételek",
        "units": {
            "hour": "óra", "day": "nap", "piece": "db", "month": "hó",
            "year": "év", "package": "csomag",
        },
        "months": [],
    },
}

CURRENCY_SYMBOLS = {"HUF": "Ft", "EUR": "€", "USD": "$"}

# Non-breaking space: a number never gets split across two lines in the PDF
NBSP = "\u00a0"


def _group_thousands(digits, separator):
    groups = []
    while len(digits) > 3:
        groups.insert(0, digits[-3:])
        digits = digits[:-3]
    groups.insert(0, digits)
    return separator.join(groups)


def format_number(amount, language, decimals=2):
    """1234567.5 -> en: 1,234,567.50  hu: 1 234 567,50"""
    amount = Decimal(amount).quantize(Decimal(1).scaleb(-decimals))
    sign = "-" if amount < 0 else ""
    integer, _, fraction = f"{abs(amount):f}".partition(".")
    thousands, decimal_mark = (",", ".") if language == "en" else (NBSP, ",")
    result = sign + _group_thousands(integer, thousands)
    return f"{result}{decimal_mark}{fraction}" if fraction else result


def format_money(amount, currency, language):
    """en: €1,250.00 / 450,000 Ft   hu: 1 250,00 € / 450 000 Ft"""
    number = format_number(amount, language, CURRENCY_DECIMALS.get(currency, 2))
    symbol = CURRENCY_SYMBOLS.get(currency, currency)
    if language == "en" and currency != "HUF":
        return f"{symbol}{number}"
    return f"{number}{NBSP}{symbol}"


def format_quantity(quantity, language):
    # 24 -> "24", 12.50 -> "12.5", 0.25 -> "0.25"
    quantity = Decimal(quantity).normalize()
    decimals = max(0, -quantity.as_tuple().exponent)
    return format_number(quantity, language, decimals)


def format_date(value, language):
    """en: 5 November 2026   hu: 2026. 11. 05."""
    if value is None:
        return ""
    if language == "en":
        return f"{value.day} {LABELS['en']['months'][value.month - 1]} {value.year}"
    return f"{value.year}. {value.month:02d}. {value.day:02d}."
