from django.contrib import admin
from .models import Project
from clients.admin import ClientFilteredAutocompleteMedia, ClientFilteredAutocompleteMixin
from quotes.admin import QuoteInline
from tasks.admin import TaskInline


@admin.register(Project)
class ProjectAdmin(ClientFilteredAutocompleteMixin, admin.ModelAdmin):
    list_display = (
        "name",
        "client",
        "status",
        "project_type",
        "has_maintenance",
        "price_column",
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
    inlines = [QuoteInline, TaskInline]
    readonly_fields = ("price_quote",)
    change_form_template = "admin/projects/project/change_form.html"
    Media = ClientFilteredAutocompleteMedia

    fieldsets = (
        (
            "Basic Information",
            {"fields": ("client", "primary_contact", "name", "description")},
        ),
        (
            "Sales & Production Status",
            {
                "fields": (
                    "status",
                    "project_type",
                    "has_maintenance",
                    ("price", "price_currency"),
                    "price_quote",
                    "start_date",
                    "deadline",
                )
            },
        ),
    )

    @admin.display(description="Price", ordering="price")
    def price_column(self, obj):
        return obj.price_display

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("client", "price_quote")

    def save_model(self, request, obj, form, change):
        # A manually edited price no longer comes from the linked quote
        if {"price", "price_currency"} & set(form.changed_data):
            obj.price_quote = None
        super().save_model(request, obj, form, change)
