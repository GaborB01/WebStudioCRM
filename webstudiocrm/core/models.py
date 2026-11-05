from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import IntegrityError, models, transaction
from django.utils import timezone

from .choices import Currency, DocumentLanguage


class StudioProfile(models.Model):
    """The studio's own company details and document defaults.

    There is only ever one row (id=1); always access it via get_solo().
    """

    SOLO_PK = 1

    company_name = models.CharField("Company Name", max_length=255, blank=True)
    address = models.TextField("Registered Address", blank=True)
    tax_number = models.CharField("Tax Number", max_length=50, blank=True)
    eu_vat_number = models.CharField(
        "EU VAT Number",
        max_length=50,
        blank=True,
        help_text="Community tax number, required on EU reverse charge documents.",
    )
    registration_number = models.CharField(
        "Company Registration Number", max_length=50, blank=True
    )
    email = models.EmailField("Email Address", blank=True)
    phone = models.CharField("Phone Number", max_length=30, blank=True)
    website = models.URLField("Website", blank=True)
    logo = models.ImageField("Logo", upload_to="studio/", blank=True)

    default_currency = models.CharField(
        "Default Currency",
        max_length=3,
        choices=Currency,
        default=Currency.HUF,
    )
    default_language = models.CharField(
        "Default Document Language",
        max_length=2,
        choices=DocumentLanguage,
        default=DocumentLanguage.HUNGARIAN,
    )
    default_payment_terms_days = models.PositiveSmallIntegerField(
        "Default Payment Terms (days)", default=8
    )
    default_quote_validity_days = models.PositiveSmallIntegerField(
        "Default Quote Validity (days)", default=30
    )
    default_quote_terms_en = models.TextField("Default Quote Terms (English)", blank=True)
    default_quote_terms_hu = models.TextField("Default Quote Terms (Hungarian)", blank=True)

    updated_at = models.DateTimeField("Updated At", auto_now=True)

    class Meta:
        verbose_name = "Studio Profile"
        verbose_name_plural = "Studio Profile"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(id=1), name="single_studio_profile"
            )
        ]

    def __str__(self):
        return self.company_name or "Studio Profile"

    @classmethod
    def get_solo(cls):
        profile, _ = cls.objects.get_or_create(pk=cls.SOLO_PK)
        return profile

    def bank_account_for(self, currency):
        return self.bank_accounts.filter(currency=currency).first()


class StudioBankAccount(models.Model):
    """A studio bank account; documents show the one matching their currency."""

    profile = models.ForeignKey(
        StudioProfile,
        on_delete=models.CASCADE,
        related_name="bank_accounts",
        verbose_name="Studio Profile",
    )
    currency = models.CharField("Currency", max_length=3, choices=Currency)
    bank_name = models.CharField("Bank Name", max_length=255)
    account_number = models.CharField(
        "Domestic Account Number", max_length=50, blank=True
    )
    iban = models.CharField("IBAN", max_length=34, blank=True)
    swift_bic = models.CharField("SWIFT / BIC", max_length=11, blank=True)

    class Meta:
        verbose_name = "Bank Account"
        verbose_name_plural = "Bank Accounts"
        constraints = [
            models.UniqueConstraint(
                fields=["profile", "currency"],
                name="unique_bank_account_per_currency",
                violation_error_message="There is already a bank account for this currency.",
            )
        ]

    def __str__(self):
        return f"{self.bank_name} ({self.currency})"


class VatRate(models.Model):
    """A VAT rate that can be applied to a document line.

    Several rates can have the same percentage (e.g. 0% reverse charge and
    0% out of scope), so each one carries the note printed on the document.
    """

    name = models.CharField("Name", max_length=100)
    rate = models.DecimalField(
        "Rate (%)",
        max_digits=5,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("100"))],
    )
    is_reverse_charge = models.BooleanField(
        "EU Reverse Charge",
        default=False,
        help_text="The client accounts for the VAT; the client's EU VAT number is required.",
    )
    note_en = models.TextField("Document Note (English)", blank=True)
    note_hu = models.TextField("Document Note (Hungarian)", blank=True)
    is_default = models.BooleanField("Default", default=False)
    is_active = models.BooleanField("Active", default=True)

    class Meta:
        verbose_name = "VAT Rate"
        verbose_name_plural = "VAT Rates"
        ordering = ["-rate", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["is_default"],
                condition=models.Q(is_default=True),
                name="single_default_vat_rate",
                violation_error_message="Another VAT rate is already the default.",
            )
        ]

    def __str__(self):
        return f"{self.name} ({self.rate.normalize():f}%)"

    def clean(self):
        super().clean()
        errors = {}
        if self.is_reverse_charge and self.rate:
            errors["rate"] = "A reverse charge rate must be 0%."
        if self.is_reverse_charge and not (self.note_en and self.note_hu):
            errors["note_en"] = "Reverse charge needs a document note in both languages."
        if self.is_default and not self.is_active:
            errors["is_default"] = "The default VAT rate must be active."
        if errors:
            raise ValidationError(errors)

    @classmethod
    def get_default(cls):
        return cls.objects.filter(is_default=True, is_active=True).first()


class DocumentSequence(models.Model):
    """Running number per document prefix and year (e.g. Q-2026-0007)."""

    prefix = models.CharField("Prefix", max_length=10)
    year = models.PositiveSmallIntegerField("Year")
    last_number = models.PositiveIntegerField(
        "Last Used Number",
        default=0,
        help_text="The next document gets this number + 1.",
    )

    class Meta:
        verbose_name = "Document Sequence"
        verbose_name_plural = "Document Sequences"
        ordering = ["-year", "prefix"]
        constraints = [
            models.UniqueConstraint(
                fields=["prefix", "year"], name="unique_sequence_per_prefix_year"
            )
        ]

    def __str__(self):
        return f"{self.prefix}-{self.year} (last: {self.last_number})"

    @classmethod
    def next_number(cls, prefix, year=None):
        """Reserve and return the next number for prefix/year.

        Call this inside the caller's transaction.atomic() block: if saving
        the document fails, the number is rolled back and not skipped.
        """
        year = year or timezone.localdate().year
        sequence = cls.objects.filter(prefix=prefix, year=year)
        with transaction.atomic():
            # Incrementing in the database (not in Python) locks the row,
            # so two documents saved at the same time get different numbers.
            if not sequence.update(last_number=models.F("last_number") + 1):
                try:
                    with transaction.atomic():
                        cls.objects.create(prefix=prefix, year=year, last_number=1)
                    return 1
                except IntegrityError:
                    # Another request created this year's row first
                    sequence.update(last_number=models.F("last_number") + 1)
            return sequence.get().last_number

    @classmethod
    def next_reference(cls, prefix, year=None):
        year = year or timezone.localdate().year
        return f"{prefix}-{year}-{cls.next_number(prefix, year):04d}"
