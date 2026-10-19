from django.contrib import admin
from .models import Client, Contact


class ContactInline(admin.TabularInline):
    model = Contact
    extra = 1


@admin.register(Client)
class ClientAdmin(admin.ModelAdmin):
    list_display = ("name", "status", "company_type", "created_at")
    list_filter = ("status", "company_type")
    search_fields = ("name", "tax_number")
    inlines = [ContactInline]


@admin.register(Contact)
class ContactAdmin(admin.ModelAdmin):
    list_display = (
        "first_name",
        "last_name",
        "client",
        "email",
        "phone",
        "is_primary",
    )
    search_fields = ("first_name", "last_name", "email", "client__name")
