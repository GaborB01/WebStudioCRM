import re

from django.core.exceptions import ValidationError
from django.db import models
from django_countries.fields import CountryField

from core.choices import EU_COUNTRIES, HOME_COUNTRY, VatTreatment

# EU VAT numbers start with the country code; Greece uses EL instead of GR
EU_VAT_PREFIXES = {"GR": "EL"}
EU_VAT_NUMBER_PATTERN = re.compile(r"^[A-Z]{2}[0-9A-Z+*]{2,12}$")


class Client(models.Model):
    STATUS_CHOICES = [
        ("inquiry", "Inquiry"),
        ("active", "Active"),
        ("ghosted", "Ghosted"),
        ("inactive", "Inactive"),
    ]
    COMPANY_TYPE_CHOICES = [
        ("smb", "SMB"),
        ("startup", "Startup"),
        ("sole_trader", "Sole Trader"),
        ("agency", "Agency"),
        ("enterprise", "Enterprise"),
    ]

    name = models.CharField("Company Name", max_length=255)
    tax_number = models.CharField("Tax / VAT Number", max_length=50, blank=True)
    eu_vat_number = models.CharField(
        "EU VAT Number",
        max_length=20,
        blank=True,
        help_text="With country prefix, e.g. DE123456789. Needed for EU reverse charge.",
    )
    address = models.TextField("Billing Address", blank=True)
    country = CountryField("Country", default=HOME_COUNTRY)
    website = models.URLField("Website", blank=True)

    status = models.CharField(
        "Status", max_length=20, choices=STATUS_CHOICES, default="inquiry"
    )
    company_type = models.CharField(
        "Company Type",
        max_length=20,
        choices=COMPANY_TYPE_CHOICES,
        default="smb",
    )

    notes = models.TextField("Internal Notes", blank=True)
    created_at = models.DateTimeField("Created At", auto_now_add=True)
    updated_at = models.DateTimeField("Updated At", auto_now=True)

    class Meta:
        verbose_name = "Client"
        verbose_name_plural = "Clients"
        ordering = ["-created_at"]

    def __str__(self):
        return self.name

    def clean(self):
        super().clean()
        self.eu_vat_number = self.eu_vat_number.replace(" ", "").upper()
        if not self.eu_vat_number:
            return
        if self.country.code not in EU_COUNTRIES:
            raise ValidationError(
                {"eu_vat_number": "Only clients in the EU have an EU VAT number."}
            )
        prefix = EU_VAT_PREFIXES.get(self.country.code, self.country.code)
        if not (
            EU_VAT_NUMBER_PATTERN.match(self.eu_vat_number)
            and self.eu_vat_number.startswith(prefix)
        ):
            raise ValidationError(
                {"eu_vat_number": f"Must start with {prefix}, e.g. {prefix}12345678."}
            )

    @property
    def vat_treatment(self):
        """Which VAT applies when the studio bills this client.

        EU clients without an EU VAT number (consumers, unregistered
        businesses) are charged Hungarian VAT.
        """
        code = self.country.code
        if code == HOME_COUNTRY:
            return VatTreatment.STANDARD
        if code in EU_COUNTRIES:
            if self.eu_vat_number:
                return VatTreatment.EU_REVERSE_CHARGE
            return VatTreatment.STANDARD
        return VatTreatment.OUTSIDE_EU


class Contact(models.Model):
    client = models.ForeignKey(
        Client,
        on_delete=models.CASCADE,
        related_name="contacts",
        verbose_name="Client",
    )

    first_name = models.CharField("First Name", max_length=100)
    last_name = models.CharField("Last Name", max_length=100)
    role = models.CharField(
        "Role / Position",
        max_length=100,
        blank=True,
    )
    email = models.EmailField("Email Address")
    phone = models.CharField("Phone Number", max_length=30, blank=True)
    is_primary = models.BooleanField("Primary Contact", default=False)
    notes = models.TextField("Notes", blank=True)

    class Meta:
        verbose_name = "Contact"
        verbose_name_plural = "Contacts"
        ordering = ["last_name", "first_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["client"],
                condition=models.Q(is_primary=True),
                name="unique_primary_contact_per_client",
                violation_error_message="This client already has a primary contact.",
            )
        ]

    def __str__(self):
        return f"{self.first_name} {self.last_name} ({self.client.name})"
