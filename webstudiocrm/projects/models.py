from django.db import models
from clients.models import Client, Contact


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
        "Project Price",
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
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
