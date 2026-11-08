from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from clients.models import Contact
from core.choices import Currency, DocumentLanguage
from core.models import VatRate
from projects.models import Project

PERCENT_VALIDATORS = [MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("100"))]


class Unit(models.TextChoices):
    HOUR = "hour", "Hour"
    DAY = "day", "Day"
    PIECE = "piece", "Piece"
    MONTH = "month", "Month"
    YEAR = "year", "Year"
    PACKAGE = "package", "Package"


class BillingType(models.TextChoices):
    ONE_OFF = "one_off", "One-off"
    MONTHLY = "monthly", "Monthly"
    YEARLY = "yearly", "Yearly"


class ServiceItem(models.Model):
    """A reusable entry of the studio's rate card."""

    class Category(models.TextChoices):
        DESIGN = "design", "Design"
        DEVELOPMENT = "development", "Development"
        HOSTING = "hosting", "Hosting"
        MAINTENANCE = "maintenance", "Maintenance"
        OTHER = "other", "Other"

    name_en = models.CharField("Name (English)", max_length=255)
    name_hu = models.CharField("Name (Hungarian)", max_length=255)
    description_en = models.TextField("Description (English)", blank=True)
    description_hu = models.TextField("Description (Hungarian)", blank=True)
    category = models.CharField(
        "Category", max_length=20, choices=Category, default=Category.DEVELOPMENT
    )
    unit = models.CharField("Unit", max_length=10, choices=Unit, default=Unit.HOUR)
    billing_type = models.CharField(
        "Billing", max_length=10, choices=BillingType, default=BillingType.ONE_OFF
    )
    vat_rate = models.ForeignKey(
        VatRate,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        verbose_name="VAT Rate",
        help_text="Leave empty to use the rate that fits the client.",
    )
    is_active = models.BooleanField("Active", default=True)

    class Meta:
        verbose_name = "Service Item"
        verbose_name_plural = "Service Items"
        ordering = ["category", "name_en"]

    def __str__(self):
        return self.name_en

    def name_in(self, language):
        return self.name_hu if language == DocumentLanguage.HUNGARIAN else self.name_en

    def description_in(self, language):
        if language == DocumentLanguage.HUNGARIAN:
            return self.description_hu
        return self.description_en

    def price_for(self, currency):
        price = self.prices.filter(currency=currency).first()
        return price.unit_price if price else None


class ServiceItemPrice(models.Model):
    """Price of a service item in one currency (no conversion between them)."""

    service_item = models.ForeignKey(
        ServiceItem, on_delete=models.CASCADE, related_name="prices"
    )
    currency = models.CharField("Currency", max_length=3, choices=Currency)
    unit_price = models.DecimalField(
        "Unit Price", max_digits=12, decimal_places=2, validators=[MinValueValidator(0)]
    )

    class Meta:
        verbose_name = "Price"
        verbose_name_plural = "Prices"
        constraints = [
            models.UniqueConstraint(
                fields=["service_item", "currency"],
                name="unique_service_price_per_currency",
                violation_error_message="This service already has a price in this currency.",
            )
        ]

    def __str__(self):
        return f"{self.unit_price} {self.currency}"


class Quote(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SENT = "sent", "Sent"
        ACCEPTED = "accepted", "Accepted"
        REJECTED = "rejected", "Rejected"
        EXPIRED = "expired", "Expired"
        SUPERSEDED = "superseded", "Superseded"

    project = models.ForeignKey(
        Project, on_delete=models.PROTECT, related_name="quotes", verbose_name="Project"
    )
    contact = models.ForeignKey(
        Contact,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="quotes",
        verbose_name="Recipient",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="Created By",
    )

    number = models.CharField(
        "Quote Number", max_length=30, unique=True, null=True, blank=True, editable=False
    )
    version = models.PositiveSmallIntegerField("Version", default=1, editable=False)
    revision_of = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="revisions",
        verbose_name="Revision Of",
        editable=False,
    )
    status = models.CharField(
        "Status", max_length=20, choices=Status, default=Status.DRAFT, editable=False
    )

    language = models.CharField(
        "Language", max_length=2, choices=DocumentLanguage, default=DocumentLanguage.HUNGARIAN
    )
    currency = models.CharField("Currency", max_length=3, choices=Currency, default=Currency.HUF)
    title = models.CharField("Title", max_length=255, blank=True)
    intro_text = models.TextField("Introduction", blank=True)
    terms_text = models.TextField("Terms", blank=True)
    validity_days = models.PositiveSmallIntegerField("Valid For (days)", default=30)
    payment_terms_days = models.PositiveSmallIntegerField("Payment Terms (days)", default=8)
    discount_percent = models.DecimalField(
        "Discount (%)",
        max_digits=5,
        decimal_places=2,
        default=0,
        validators=PERCENT_VALIDATORS,
        help_text="Applies to one-off items only.",
    )

    # Totals: written only by QuoteService.recalculate()
    subtotal_net = models.DecimalField(
        "Subtotal (net)", max_digits=14, decimal_places=2, default=0, editable=False
    )
    discount_amount = models.DecimalField(
        "Discount", max_digits=14, decimal_places=2, default=0, editable=False
    )
    net_total = models.DecimalField(
        "Total (net)", max_digits=14, decimal_places=2, default=0, editable=False
    )
    vat_amount = models.DecimalField(
        "VAT", max_digits=14, decimal_places=2, default=0, editable=False
    )
    gross_total = models.DecimalField(
        "Total (gross)", max_digits=14, decimal_places=2, default=0, editable=False
    )
    recurring_monthly_net = models.DecimalField(
        "Monthly (net)", max_digits=14, decimal_places=2, default=0, editable=False
    )
    recurring_yearly_net = models.DecimalField(
        "Yearly (net)", max_digits=14, decimal_places=2, default=0, editable=False
    )

    # Copied when the quote is sent; never changed afterwards
    issue_date = models.DateField("Issue Date", null=True, blank=True, editable=False)
    valid_until = models.DateField("Valid Until", null=True, blank=True, editable=False)
    client_snapshot = models.JSONField("Client Details", default=dict, editable=False)
    seller_snapshot = models.JSONField("Studio Details", default=dict, editable=False)
    pdf = models.FileField("PDF", upload_to="quotes/%Y/", blank=True, editable=False)

    follow_up_task = models.ForeignKey(
        "tasks.Task",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        editable=False,
    )
    sent_at = models.DateTimeField("Sent At", null=True, blank=True, editable=False)
    accepted_at = models.DateTimeField("Accepted At", null=True, blank=True, editable=False)
    rejected_at = models.DateTimeField("Rejected At", null=True, blank=True, editable=False)
    rejection_reason = models.TextField("Rejection Reason", blank=True)

    created_at = models.DateTimeField("Created At", auto_now_add=True)
    updated_at = models.DateTimeField("Updated At", auto_now=True)

    class Meta:
        verbose_name = "Quote"
        verbose_name_plural = "Quotes"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.reference} - {self.project.name}"

    @property
    def reference(self):
        return self.number or f"Draft #{self.pk}"

    @property
    def is_editable(self):
        return self.status == self.Status.DRAFT

    @property
    def client(self):
        return self.project.client

    def clean(self):
        super().clean()
        if self.contact_id and self.project_id and self.contact.client_id != self.project.client_id:
            raise ValidationError(
                {"contact": "This contact does not belong to the project's client."}
            )


class QuoteLineItem(models.Model):
    quote = models.ForeignKey(Quote, on_delete=models.CASCADE, related_name="items")
    service_item = models.ForeignKey(
        ServiceItem,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name="Service",
        help_text="Empty description and price are filled from the service.",
    )
    position = models.PositiveSmallIntegerField("Position", default=0)
    description = models.TextField("Description", blank=True)
    quantity = models.DecimalField(
        "Quantity",
        max_digits=10,
        decimal_places=2,
        default=1,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    unit = models.CharField("Unit", max_length=10, choices=Unit, blank=True)
    unit_price = models.DecimalField(
        "Unit Price",
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(0)],
    )
    discount_percent = models.DecimalField(
        "Discount (%)", max_digits=5, decimal_places=2, default=0, validators=PERCENT_VALIDATORS
    )
    vat_rate = models.ForeignKey(
        VatRate,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        verbose_name="VAT Rate",
        help_text="Empty: the rate that fits the client.",
    )
    vat_percent = models.DecimalField(
        "VAT (%)", max_digits=5, decimal_places=2, default=0, editable=False
    )
    billing_type = models.CharField(
        "Billing", max_length=10, choices=BillingType, blank=True
    )
    is_optional = models.BooleanField(
        "Optional", default=False, help_text="Shown on the quote, not included in totals."
    )
    line_net = models.DecimalField(
        "Net Amount", max_digits=14, decimal_places=2, default=0, editable=False
    )

    class Meta:
        verbose_name = "Line Item"
        verbose_name_plural = "Line Items"
        ordering = ["position", "id"]

    def __str__(self):
        return self.description.splitlines()[0] if self.description else "Line item"

    def fill_defaults(self):
        """Fill empty fields from the service item and the client's VAT rate."""
        quote = self.quote
        service = self.service_item
        if service:
            if not self.description:
                self.description = "\n".join(
                    filter(None, [service.name_in(quote.language), service.description_in(quote.language)])
                )
            if self.unit_price is None:
                self.unit_price = service.price_for(quote.currency)
            self.unit = self.unit or service.unit
            self.billing_type = self.billing_type or service.billing_type
            if self.vat_rate_id is None and service.vat_rate_id:
                self.vat_rate = service.vat_rate
        self.unit = self.unit or Unit.PIECE
        self.billing_type = self.billing_type or BillingType.ONE_OFF
        if self.vat_rate_id is None:
            self.vat_rate = VatRate.suggested_for(quote.client.vat_treatment)

    def clean(self):
        super().clean()
        if not self.quote_id:
            return
        self.fill_defaults()
        errors = {}
        if not self.description:
            errors["description"] = "Enter a description or choose a service."
        if self.unit_price is None:
            if self.service_item:
                errors["unit_price"] = (
                    f"The service has no {self.quote.currency} price; enter one."
                )
            else:
                errors["unit_price"] = "Enter a unit price or choose a service."
        if self.vat_rate is None:
            errors["vat_rate"] = "Choose a VAT rate (no default VAT rate is set)."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        from .services.calculator import line_net

        self.fill_defaults()
        self.vat_percent = self.vat_rate.rate
        self.line_net = line_net(
            self.quantity, self.unit_price, self.discount_percent, self.quote.currency
        )
        super().save(*args, **kwargs)
