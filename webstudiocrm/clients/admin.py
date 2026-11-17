from django import forms
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.forms.models import BaseInlineFormSet
from django.http import HttpResponseNotAllowed
from django.shortcuts import redirect
from django.urls import path, reverse

from communications.admin import recent_activity_context, review_badge
from communications.tasks import queue, rematch_for_client, rematch_for_contact
from documents.admin import ClientDocumentInline
from projects.models import Project

from .models import Client, ClientEmailDomain, Contact, ContactEmail


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


class RunMatchingFormMixin(forms.ModelForm):
    """Adds the "match waiting emails" checkbox to an add form."""

    run_matching = forms.BooleanField(
        label="Match waiting emails",
        required=False,
        initial=True,
        help_text="After saving, look through emails that are unmatched or awaiting "
        "review for ones this explains. Runs in the background.",
    )


class ClientForm(RunMatchingFormMixin):
    class Meta:
        model = Client
        fields = "__all__"


class ContactForm(RunMatchingFormMixin):
    class Meta:
        model = Contact
        fields = "__all__"


class RematchAdminMixin:
    """Add-form checkbox and change-page button that queue a scoped re-match.

    Subclasses set `rematch_task` (takes the object's pk) and `rematch_url_name`.
    """

    rematch_task = None
    rematch_url_name = ""

    def get_fields(self, request, obj=None):
        fields = list(super().get_fields(request, obj))
        if obj is not None and "run_matching" in fields:
            fields.remove("run_matching")  # add form only; the button does it later
        return fields

    def save_related(self, request, form, formsets, change):
        # After the inlines: new addresses and domains must be saved first
        super().save_related(request, form, formsets, change)
        if not change and form.cleaned_data.get("run_matching"):
            messages.info(request, queue(self.rematch_task, form.instance.pk))

    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        opts = self.model._meta
        extra_context = {
            **(extra_context or {}),
            "rematch_url_name": f"admin:{opts.app_label}_{opts.model_name}_rematch",
            "compose_field": opts.model_name,  # "client" or "contact"
        }
        return super().changeform_view(request, object_id, form_url, extra_context)

    def get_urls(self):
        opts = self.model._meta
        custom = [
            path(
                "<int:pk>/match-emails/",
                self.admin_site.admin_view(self.rematch_view),
                name=f"{opts.app_label}_{opts.model_name}_rematch",
            ),
        ]
        return custom + super().get_urls()

    def rematch_view(self, request, pk):
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        obj = self.get_object(request, pk)
        if obj is None or not self.has_change_permission(request, obj):
            raise PermissionDenied
        messages.info(request, queue(self.rematch_task, obj.pk))
        opts = self.model._meta
        return redirect(reverse(f"admin:{opts.app_label}_{opts.model_name}_change", args=[pk]))


class ClientProjectInline(admin.TabularInline):
    """The client's projects, read-only; open one to work on it."""

    model = Project
    fk_name = "client"
    extra = 0
    can_delete = False
    show_change_link = True
    fields = ("number", "name", "status", "project_type", "deadline")
    readonly_fields = fields
    verbose_name_plural = "Projects"

    def has_add_permission(self, request, obj=None):
        return False  # "New project" at the top of the page


class ClientEmailDomainInline(admin.TabularInline):
    model = ClientEmailDomain
    extra = 0


class ContactEmailInline(admin.TabularInline):
    model = ContactEmail
    extra = 0
    fields = ("email", "label", "created_at")
    readonly_fields = ("created_at",)


def client_summary(client):
    """One line under the title: where things stand with the client."""
    from django.utils.html import format_html

    open_projects = client.projects.exclude(status__in=["completed", "cancelled"]).count()
    last_email = client.emails.order_by("-sent_at").values_list("sent_at", flat=True).first()
    flagged = client.emails.filter(needs_review=True).count()
    parts = [
        client.get_status_display(),
        client.get_company_type_display(),
        client.country.name,
        f"{open_projects} open project{'s' if open_projects != 1 else ''}",
        f"last email {last_email:%Y-%m-%d}" if last_email else "no email yet",
    ]
    if flagged:
        url = reverse("admin:communications_emailmessage_changelist")
        parts.append(format_html('<a href="{}?match=review&amp;client__id__exact={}">{}</a>',
                                 url, client.pk, review_badge(True, f"{flagged} to review")))
    return parts


@admin.register(Client)
class ClientAdmin(RematchAdminMixin, admin.ModelAdmin):
    form = ClientForm
    rematch_task = rematch_for_client
    change_form_template = "admin/clients/rematch_change_form.html"
    list_display = ("name", "country", "status", "company_type", "created_at")
    list_filter = ("status", "company_type", "country")
    search_fields = ("name", "tax_number", "eu_vat_number", "email_domains__domain")
    fields = (
        "name",
        "status",
        "company_type",
        "country",
        "address",
        "tax_number",
        "eu_vat_number",
        "website",
        "notes",
        "run_matching",
    )
    # Daily things first; every section open
    inlines = [ContactInline, ClientProjectInline, ClientDocumentInline, ClientEmailDomainInline]

    def get_inlines(self, request, obj):
        # A client being added has no projects yet: no empty table on the add form
        if obj is None:
            return [i for i in self.inlines if i is not ClientProjectInline]
        return self.inlines

    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        client = self.get_object(request, object_id) if object_id else None
        if client is not None:
            extra_context = {**(extra_context or {}), "record_summary": client_summary(client),
                             **recent_activity_context(client=client)}
        return super().changeform_view(request, object_id, form_url, extra_context)


@admin.register(Contact)
class ContactAdmin(RematchAdminMixin, ClientFilteredAutocompleteMixin, admin.ModelAdmin):
    form = ContactForm
    rematch_task = rematch_for_contact
    change_form_template = "admin/clients/rematch_change_form.html"
    list_display = (
        "first_name",
        "last_name",
        "client",
        "email",
        "phone",
        "is_primary",
    )
    search_fields = (
        "first_name",
        "last_name",
        "email",
        "email_addresses__email",
        "client__name",
    )
    inlines = [ContactEmailInline]
