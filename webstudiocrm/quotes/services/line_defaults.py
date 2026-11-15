"""The values a quote line takes from its service item.

Used twice, and deliberately in one place so the two can't drift apart:

* `QuoteLineItem.fill_defaults()` applies them when the line is saved
* the admin's service-defaults endpoint returns them so the inline row can
  show the same values the moment a service is picked
"""


def service_defaults(quote, service_item):
    """Service-derived values for a line on this quote.

    Depends on the quote: the description follows its language, the price its
    currency, and the VAT rate falls back to the one that fits the client.
    """
    from core.models import VatRate

    from quotes.models import BillingType, Unit

    description = ""
    unit_price = None
    unit = ""
    billing_type = ""
    vat_rate = None

    if service_item:
        description = "\n".join(
            filter(
                None,
                [
                    service_item.name_in(quote.language),
                    service_item.description_in(quote.language),
                ],
            )
        )
        unit_price = service_item.price_for(quote.currency)
        unit = service_item.unit
        billing_type = service_item.billing_type
        vat_rate = service_item.vat_rate

    return {
        "description": description,
        "unit_price": unit_price,
        "unit": unit or Unit.PIECE,
        "billing_type": billing_type or BillingType.ONE_OFF,
        "vat_rate": vat_rate or VatRate.suggested_for(quote.client.vat_treatment),
    }
