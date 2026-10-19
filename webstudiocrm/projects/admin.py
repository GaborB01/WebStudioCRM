from django.contrib import admin
from .models import Project
from tasks.admin import TaskInline


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "client",
        "status",
        "project_type",
        "has_maintenance",
        "price",
        "deadline",
    )
    list_filter = (
        "status",
        "client",
        "project_type",
        "created_at",
    )
    search_fields = ("name", "client__name", "description")
    autocomplete_fields = ["client", "primary_contact"]
    inlines = [TaskInline]

    fieldsets = (
        (
            "Basic Information",
            {"fields": ("client", "primary_contact", "name", "description")},
        ),
        (
            "Sales & Production Status",
            {"fields": ("status", "project_type", "price", "start_date", "deadline")},
        ),
    )
