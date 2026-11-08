"""Quote arithmetic. Pure functions: no database access.

Rounding rules:
- Amounts are rounded half-up to the currency's precision
  (HUF: whole forints, EUR/USD: cents) at the line level.
- The quote discount applies to one-off lines only and is spread over the
  VAT groups, so VAT is calculated on the discounted amount.
- VAT is calculated per VAT rate on the group total, not per line.
"""

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

CURRENCY_DECIMALS = {"HUF": 0, "EUR": 2, "USD": 2}
HUNDRED = Decimal("100")


def round_money(amount, currency):
    exponent = Decimal(1).scaleb(-CURRENCY_DECIMALS.get(currency, 2))
    return Decimal(amount).quantize(exponent, rounding=ROUND_HALF_UP)


def line_net(quantity, unit_price, discount_percent, currency):
    gross = Decimal(quantity) * Decimal(unit_price)
    return round_money(gross * (1 - Decimal(discount_percent) / HUNDRED), currency)


@dataclass(frozen=True)
class LineInput:
    net: Decimal
    vat_rate_id: int
    vat_percent: Decimal
    billing_type: str
    is_optional: bool = False


@dataclass(frozen=True)
class VatGroup:
    vat_rate_id: int
    vat_percent: Decimal
    net: Decimal
    vat: Decimal


@dataclass(frozen=True)
class QuoteTotals:
    subtotal_net: Decimal
    discount_amount: Decimal
    net_total: Decimal
    vat_amount: Decimal
    gross_total: Decimal
    recurring_monthly_net: Decimal
    recurring_yearly_net: Decimal
    vat_groups: list[VatGroup] = field(default_factory=list)


def calculate(lines, discount_percent, currency):
    zero = round_money(0, currency)
    discount_factor = 1 - Decimal(discount_percent) / HUNDRED

    one_off_by_rate = {}
    monthly = yearly = zero
    for line in lines:
        if line.is_optional:
            continue
        if line.billing_type == "monthly":
            monthly += line.net
        elif line.billing_type == "yearly":
            yearly += line.net
        else:
            key = (line.vat_rate_id, Decimal(line.vat_percent))
            one_off_by_rate[key] = one_off_by_rate.get(key, zero) + line.net

    groups = []
    for (rate_id, percent), net in one_off_by_rate.items():
        discounted = round_money(net * discount_factor, currency)
        vat = round_money(discounted * percent / HUNDRED, currency)
        groups.append(VatGroup(rate_id, percent, discounted, vat))
    groups.sort(key=lambda g: g.vat_percent, reverse=True)

    subtotal = sum(one_off_by_rate.values(), zero)
    net_total = sum((g.net for g in groups), zero)
    vat_total = sum((g.vat for g in groups), zero)
    return QuoteTotals(
        subtotal_net=subtotal,
        discount_amount=subtotal - net_total,
        net_total=net_total,
        vat_amount=vat_total,
        gross_total=net_total + vat_total,
        recurring_monthly_net=monthly,
        recurring_yearly_net=yearly,
        vat_groups=groups,
    )
