from django.contrib import admin
from django.core.exceptions import ValidationError
from django.forms.models import BaseInlineFormSet
from .models import Client, Contact


class ClientFilteredAutocompleteMixin:
    """Limits autocomplete results to the client selected on the form.

    The client id is added to autocomplete requests by
    client_filtered_autocomplete.js on the form that holds the field.
    """

    def get_search_results(self, request, queryset, search_term):
        queryset, may_have_duplicates = super().get_search_results(
            request, queryset, search_term
        )
        # "field_name" is only sent by autocomplete requests, not the changelist
        client_id = request.GET.get("client", "")
        if "field_name" in request.GET and client_id.isdigit():
            queryset = queryset.filter(client_id=client_id)
        return queryset, may_have_duplicates


class ClientFilteredAutocompleteMedia:
    js = (
        "admin/js/jquery.init.js",
        "clients/js/client_filtered_autocomplete.js",
    )


class ContactInlineFormSet(BaseInlineFormSet):
    def clean(self):
        super().clean()
        primary_count = sum(
            1
            for form in self.forms
            if form.cleaned_data
            and not form.cleaned_data.get("DELETE")
            and form.cleaned_data.get("is_primary")
        )
        if primary_count > 1:
            raise ValidationError("Only one contact can be marked as primary.")


class ContactInline(admin.TabularInline):
    model = Contact
    formset = ContactInlineFormSet
    extra = 1


@admin.register(Client)
class ClientAdmin(admin.ModelAdmin):
    list_display = ("name", "status", "company_type", "created_at")
    list_filter = ("status", "company_type")
    search_fields = ("name", "tax_number")
    inlines = [ContactInline]


@admin.register(Contact)
class ContactAdmin(ClientFilteredAutocompleteMixin, admin.ModelAdmin):
    list_display = (
        "first_name",
        "last_name",
        "client",
        "email",
        "phone",
        "is_primary",
    )
    search_fields = ("first_name", "last_name", "email", "client__name")
