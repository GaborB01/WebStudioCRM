from django.core.exceptions import ValidationError
from django.db import models
from clients.models import Client, Contact
from core.choices import Currency


class Project(models.Model):
    STATUS_CHOICES = [
        ("consultation", "Consultation"),
        ("quote_sent", "Quote Sent"),
        ("contracting", "Contracting"),
        ("new", "New"),
        ("design", "Design"),
        ("development", "Under Development"),
        ("review", "Client Review"),
        ("completed", "Completed"),
        ("cancelled", "Cancelled"),
    ]

    PROJECT_TYPE_CHOICES = [
        ("web_app", "Web Application"),
        ("website", "Website"),
        ("webshop", "Webshop"),
    ]

    client = models.ForeignKey(
        Client,
        on_delete=models.CASCADE,
        related_name="projects",
        verbose_name="Client",
    )
    primary_contact = models.ForeignKey(
        Contact,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="projects",
        verbose_name="Primary Contact",
    )

    name = models.CharField("Project Name", max_length=255)
    description = models.TextField("Description", blank=True)

    status = models.CharField(
        "Status", max_length=20, choices=STATUS_CHOICES, default="consultation"
    )
    project_type = models.CharField(
        "Project Type",
        max_length=30,
        choices=PROJECT_TYPE_CHOICES,
    )
    has_maintenance = models.BooleanField(
        "Includes Maintenance",
        default=False,
    )

    price = models.DecimalField(
        "Project Price (net)",
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
    )
    price_currency = models.CharField(
        "Currency", max_length=3, choices=Currency, default=Currency.HUF
    )
    price_quote = models.ForeignKey(
        # String reference: quotes imports projects, so no import here
        "quotes.Quote",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="Price From Quote",
        help_text="Set automatically when a quote is accepted.",
    )

    start_date = models.DateField("Start Date", null=True, blank=True)
    deadline = models.DateField("Deadline", null=True, blank=True)

    created_at = models.DateTimeField("Created At", auto_now_add=True)
    updated_at = models.DateTimeField("Updated At", auto_now=True)

    class Meta:
        verbose_name = "Project"
        verbose_name_plural = "Projects"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} - {self.client.name}"

    @property
    def price_display(self):
        if self.price is None:
            return ""
        text = f"{self.price:,.2f} {self.price_currency}"
        if self.price_quote_id:
            text += f" ({self.price_quote.number})"
        return text

    def clean(self):
        super().clean()
        if (
            self.primary_contact_id
            and self.client_id
            and self.primary_contact.client_id != self.client_id
        ):
            raise ValidationError(
                {"primary_contact": "This contact does not belong to the selected client."}
            )

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # Keep the project's tasks on the same client if the client changes
        self.tasks.exclude(client=self.client).update(client=self.client)
