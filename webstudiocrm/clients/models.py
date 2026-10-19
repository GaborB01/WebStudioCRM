from django.db import models


class Client(models.Model):
    STATUS_CHOICES = [
        ("inquiry", "Inquiry"),
        ("consultation", "Consultation"),
        ("proposal", "Proposal Sent"),
        ("contracting", "Contracting"),
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
    address = models.TextField("Billing Address", blank=True)
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


class Contact(models.Model):
    client = models.ForeignKey(
        Client,
        on_delete=models.Case,
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
    is_primary = models.BooleanField("Primary Contact", default=True)
    notes = models.TextField("Notes", blank=True)

    class Meta:
        verbose_name = "Contact"
        verbose_name_plural = "Contacts"

    def __str__(self):
        return f"{self.first_name} {self.last_name} ({self.client.name})"
