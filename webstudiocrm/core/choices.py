from django.db import models


class Currency(models.TextChoices):
    HUF = "HUF", "Hungarian Forint (HUF)"
    EUR = "EUR", "Euro (EUR)"
    USD = "USD", "US Dollar (USD)"


class DocumentLanguage(models.TextChoices):
    ENGLISH = "en", "English"
    HUNGARIAN = "hu", "Hungarian"


class VatTreatment(models.TextChoices):
    """How VAT applies to a client; each VAT rate belongs to one treatment."""

    STANDARD = "standard", "Hungarian VAT"
    EU_REVERSE_CHARGE = "eu_reverse_charge", "EU reverse charge (B2B)"
    OUTSIDE_EU = "outside_eu", "Outside the EU"


HOME_COUNTRY = "HU"

# ISO 3166 codes of EU member states
EU_COUNTRIES = frozenset(
    "AT BE BG CY CZ DE DK EE ES FI FR GR HR HU IE IT LT LU LV MT NL PL PT RO SE SI SK".split()
)
