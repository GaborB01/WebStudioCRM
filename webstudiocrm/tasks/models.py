from django.core.exceptions import ValidationError
from django.db import models
from clients.models import Client
from projects.models import Project


class Task(models.Model):
    class Priority(models.IntegerChoices):
        # Stored as numbers so that ordering by priority follows importance
        LOW = 1, "Low"
        MEDIUM = 2, "Medium"
        HIGH = 3, "High"
        URGENT = 4, "Urgent"

    STATUS_CHOICES = [
        ("new", "New"),
        ("active", "Active"),
        ("implemented", "Implemented"),
        ("on_hold", "On Hold"),
        ("done", "Done"),
    ]

    title = models.CharField("Task Title", max_length=255)
    description = models.TextField("Description", blank=True)

    client = models.ForeignKey(
        Client,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="tasks",
        verbose_name="Client",
    )
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="tasks",
        verbose_name="Project",
    )
    status = models.CharField(
        "Status", max_length=20, choices=STATUS_CHOICES, default="new"
    )

    priority = models.PositiveSmallIntegerField(
        "Priority", choices=Priority, default=Priority.MEDIUM
    )
    due_date = models.DateField("Due Date", null=True, blank=True)
    notes = models.TextField("Internal Notes", blank=True)

    created_at = models.DateTimeField("Created At", auto_now_add=True)
    updated_at = models.DateTimeField("Updated At", auto_now=True)

    class Meta:
        verbose_name = "Task"
        verbose_name_plural = "Tasks"
        ordering = [models.F("due_date").asc(nulls_last=True), "-priority"]

    def __str__(self):
        return f"{self.get_status_display()} - {self.title}"

    def clean(self):
        super().clean()
        if (
            self.project_id
            and self.client_id
            and self.project.client_id != self.client_id
        ):
            raise ValidationError(
                {"project": "This project belongs to a different client."}
            )

    def save(self, *args, **kwargs):
        # A project task always belongs to the project's client
        if self.project_id and not self.client_id:
            self.client_id = self.project.client_id
        super().save(*args, **kwargs)
