from django.contrib import admin
from django.shortcuts import redirect
from django.urls import reverse

from .models import DocumentSequence, StudioBankAccount, StudioProfile, VatRate


class SingletonModelAdmin(admin.ModelAdmin):
    """Admin for a model with a single row: the list page opens that row."""

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_object(self, request, object_id, from_field=None):
        # Also create the row when the change page is opened directly
        return self.model.get_solo()

    def changelist_view(self, request, extra_context=None):
        obj = self.model.get_solo()
        opts = self.model._meta
        return redirect(
            reverse(f"admin:{opts.app_label}_{opts.model_name}_change", args=[obj.pk])
        )


class StudioBankAccountInline(admin.TabularInline):
    model = StudioBankAccount
    extra = 1


@admin.register(StudioProfile)
class StudioProfileAdmin(SingletonModelAdmin):
    inlines = [StudioBankAccountInline]

    fieldsets = (
        (
            "Company Details",
            {
                "fields": (
                    "company_name",
                    "address",
                    "tax_number",
                    "eu_vat_number",
                    "registration_number",
                    "logo",
                )
            },
        ),
        ("Contact", {"fields": ("email", "phone", "website")}),
        (
            "Document Defaults",
            {
                "fields": (
                    "default_currency",
                    "default_language",
                    "default_payment_terms_days",
                    "default_quote_validity_days",
                    "default_quote_terms_en",
                    "default_quote_terms_hu",
                )
            },
        ),
    )


@admin.register(VatRate)
class VatRateAdmin(admin.ModelAdmin):
    list_display = ("name", "rate", "treatment", "is_default", "is_active")
    list_filter = ("treatment", "is_active")
    search_fields = ("name",)


@admin.register(DocumentSequence)
class DocumentSequenceAdmin(admin.ModelAdmin):
    list_display = ("prefix", "year", "last_number")
    list_filter = ("prefix", "year")
