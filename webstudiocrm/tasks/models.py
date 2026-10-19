from django.db import models
from clients.models import Client
from projects.models import Project


class Task(models.Model):
    PRIORITY_CHOICES = [
        ("low", "Low"),
        ("medium", "Medium"),
        ("high", "High"),
        ("urgent", "Urgent"),
    ]
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

    priority = models.CharField(
        "Priority", max_length=20, choices=PRIORITY_CHOICES, default="medium"
    )
    due_date = models.DateField("Due Date", null=True, blank=True)
    notes = models.TextField("Internal Notes", blank=True)

    created_at = models.DateTimeField("Created At", auto_now_add=True)
    updated_at = models.DateTimeField("Updated At", auto_now=True)

    class Meta:
        verbose_name = "Task"
        verbose_name_plural = "Tasks"
        ordering = ["due_date", "-priority"]

    def __str__(self):
        return f"{self.status} - {self.title}"
