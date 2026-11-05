from django.contrib import admin
from .models import Task
from clients.admin import ClientFilteredAutocompleteMedia


class TaskInline(admin.TabularInline):
    model = Task
    extra = 1
    fields = ("title", "priority", "due_date", "status")


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "client",
        "project",
        "priority",
        "due_date",
        "status",
    )
    list_filter = ("status", "priority", "due_date", "created_at")
    search_fields = ("title", "description", "client__name", "project__name")
    autocomplete_fields = ["client", "project"]
    list_editable = ("status", "priority")
    Media = ClientFilteredAutocompleteMedia
