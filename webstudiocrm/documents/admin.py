"""Admin for contracts and signed paperwork.

Follows the quote admin: a draft is freely editable, and once a document has
been sent for signature it becomes a record - fields, files and signers lock
down, and only the notes stay open.

The workflow buttons (send, mark signed, terminate, amend) arrive with
documents.services.workflow in the next step; until then signed paperwork is
brought in by uploading the executed file and filling in the dates.
"""

from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.core.files.base import ContentFile
from django.http import Http404, HttpResponseNotAllowed, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import path, reverse
from django.utils.dateparse import parse_date
from django.utils.html import format_html

from clients.models import Contact

from .choices import DocumentStatus, SignerStatus
from .models import Document, DocumentFile, DocumentSigner, DocumentType, SignatureRequest
from .services.workflow import DocumentError, DocumentService


@admin.register(DocumentType)
class DocumentTypeAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "prefix",
        "requires_countersignature",
        "default_validity_months",
        "default_renewal_notice_days",
        "is_default_for_projects",
        "document_count",
        "is_active",
    )
    list_filter = ("is_active", "requires_countersignature", "is_default_for_projects")
    search_fields = ("name", "prefix")
    list_editable = ("is_active",)
    fieldsets = (
        (
            None,
            {
                "fields": (
                    "name",
                    "prefix",
                    "description",
                    "is_default_for_projects",
                    "is_active",
                )
            },
        ),
        (
            "Defaults For New Documents",
            {
                "fields": (
                    "requires_countersignature",
                    "default_validity_months",
                    "default_renewal_notice_days",
                )
            },
        ),
        ("Templates", {"fields": ("template_en", "template_hu")}),
    )

    @admin.display(description="Documents")
    def document_count(self, obj):
        return obj.documents.count()

    def get_search_results(self, request, queryset, search_term):
        # Only offer active types when picking one on a document
        queryset, may_have_duplicates = super().get_search_results(
            request, queryset, search_term
        )
        if "field_name" in request.GET:
            queryset = queryset.filter(is_active=True)
        return queryset, may_have_duplicates


class DocumentFileInline(admin.TabularInline):
    model = DocumentFile
    extra = 0
    fields = ("kind", "file", "description", "size_display", "checksum_short", "uploaded_at")
    readonly_fields = ("size_display", "checksum_short", "uploaded_at")

    @admin.display(description="Size")
    def size_display(self, obj):
        from cloudstorage.models import format_bytes

        return format_bytes(obj.size) if obj.size else "-"

    @admin.display(description="SHA-256")
    def checksum_short(self, obj):
        return obj.checksum_sha256[:12] or "-"

    def has_change_permission(self, request, obj=None):
        # A stored file is evidence; replace it by adding a new row instead.
        return False

    def has_delete_permission(self, request, obj=None):
        return (obj is None or obj.is_editable) and super().has_delete_permission(request, obj)


class DocumentSignerInline(admin.TabularInline):
    model = DocumentSigner
    extra = 1
    autocomplete_fields = ["contact"]
    fields = (
        "signing_order",
        "contact",
        "name",
        "email",
        "is_studio_side",
        "status",
        "signed_at",
    )
    readonly_fields = ("status", "signed_at")

    def _editable(self, obj):
        return obj is None or obj.is_editable

    def has_add_permission(self, request, obj=None):
        return self._editable(obj) and super().has_add_permission(request, obj)

    def has_change_permission(self, request, obj=None):
        return self._editable(obj) and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return self._editable(obj) and super().has_delete_permission(request, obj)


class SignatureRequestInline(admin.TabularInline):
    """Read-only history of send-outs; written by the workflow service."""

    model = SignatureRequest
    extra = 0
    can_delete = False
    fields = ("provider", "status", "provider_envelope_id", "sent_at", "completed_at")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


class ExpiryFilter(admin.SimpleListFilter):
    """Answers "what needs attention soon?" without reading every row."""

    title = "expiry"
    parameter_name = "expiry"

    def lookups(self, request, model_admin):
        return [
            ("expiring_30", "Expiring within 30 days"),
            ("expiring_90", "Expiring within 90 days"),
            ("expired", "Already past expiry"),
            ("open_ended", "No expiry date"),
        ]

    def queryset(self, request, queryset):
        from datetime import timedelta

        from django.utils import timezone

        today = timezone.localdate()
        if self.value() == "expiring_30":
            return queryset.filter(expiry_date__gte=today, expiry_date__lte=today + timedelta(days=30))
        if self.value() == "expiring_90":
            return queryset.filter(expiry_date__gte=today, expiry_date__lte=today + timedelta(days=90))
        if self.value() == "expired":
            return queryset.filter(expiry_date__lt=today)
        if self.value() == "open_ended":
            return queryset.filter(expiry_date__isnull=True)
        return queryset


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    change_form_template = "admin/documents/document/change_form.html"

    class Media:
        js = (
            "admin/js/jquery.init.js",
            # Signer contact autocomplete only offers this client's contacts
            "clients/js/client_filtered_autocomplete.js",
            # Fills the signer's name and email when a contact is picked
            "documents/js/signer_defaults.js",
        )

    list_display = (
        "reference",
        "title",
        "document_type",
        "client",
        "status",
        "signing_display",
        "signed_date",
        "expiry_display",
    )
    list_filter = ("status", "document_type", ExpiryFilter, "language", "auto_renews")
    search_fields = ("number", "title", "client__name", "project__name", "summary")
    autocomplete_fields = ["document_type", "client", "project", "source_quote"]
    list_select_related = ("document_type", "client")
    date_hierarchy = "created_at"
    inlines = [DocumentSignerInline, DocumentFileInline, SignatureRequestInline]
    readonly_fields = (
        "number",
        "status",
        "version",
        "revision_of",
        "sent_at",
        "created_by",
        "created_at",
        "updated_at",
        "signing_display",
        "executed_file_link",
    )

    @admin.display(description="Document", ordering="number")
    def reference(self, obj):
        return obj.reference

    @admin.display(description="Signatures")
    def signing_display(self, obj):
        signed, required = obj.signing_progress
        if not required:
            return "no signers"
        label = f"{signed}/{required} signed"
        if signed == required:
            return format_html('<span style="color:#0a7a0a">{}</span>', label)
        return label

    @admin.display(description="Expires", ordering="expiry_date")
    def expiry_display(self, obj):
        if not obj.expiry_date:
            return "open-ended"
        remaining = obj.days_until_expiry
        if remaining < 0:
            return format_html(
                '<span style="color:#ba2121">{} (expired)</span>', obj.expiry_date
            )
        if obj.needs_renewal_attention:
            return format_html(
                '<span style="color:#996a00">{} (in {} days)</span>', obj.expiry_date, remaining
            )
        return obj.expiry_date

    @admin.display(description="Executed File")
    def executed_file_link(self, obj):
        stored = obj.executed_file
        if not stored:
            return "-"
        return format_html(
            '<a href="{}" target="_blank" rel="noopener">{}</a>',
            stored.file.url,
            stored.filename,
        )

    def get_fieldsets(self, request, obj=None):
        if obj is None:
            # Add page: identify the document; the rest follows once it exists
            return (
                (
                    None,
                    {
                        "fields": (
                            "document_type",
                            "client",
                            "project",
                            "source_quote",
                            "title",
                            "language",
                        )
                    },
                ),
            )
        return (
            (
                "Document",
                {
                    "fields": (
                        "document_type",
                        "client",
                        "project",
                        "source_quote",
                        "number",
                        "status",
                        "version",
                        "revision_of",
                    )
                },
            ),
            ("Content", {"fields": ("title", "language", "summary")}),
            (
                "Signature",
                {"fields": ("signing_display", "sent_at", "signed_date", "executed_file_link")},
            ),
            (
                "Term",
                {
                    "fields": (
                        "effective_date",
                        "expiry_date",
                        "auto_renews",
                        "renewal_notice_days",
                        "terminated_on",
                        "termination_reason",
                    )
                },
            ),
            ("Internal", {"fields": ("notes", "created_by", "created_at", "updated_at")}),
        )

    def get_readonly_fields(self, request, obj=None):
        if obj is None or obj.is_editable:
            return self.readonly_fields
        # Sent documents are records. Dates and the outcome stay editable so a
        # wet-ink signature or a termination can still be recorded by hand.
        editable = {
            "signed_date",
            "effective_date",
            "expiry_date",
            "auto_renews",
            "renewal_notice_days",
            "terminated_on",
            "termination_reason",
            "notes",
        }
        all_fields = [
            field
            for _, options in self.get_fieldsets(request, obj)
            for field in options["fields"]
        ]
        return [field for field in all_fields if field not in editable]

    def get_inlines(self, request, obj):
        if obj is None:
            return []
        # The send-out history is noise until there is one
        if not obj.signature_requests.exists():
            return [DocumentSignerInline, DocumentFileInline]
        return self.inlines

    def has_delete_permission(self, request, obj=None):
        # Once sent, a document is a business record
        if obj is not None and not obj.is_editable:
            return False
        return super().has_delete_permission(request, obj)

    def get_urls(self):
        extra = [
            path(
                "<int:pk>/contact-details/",
                self.admin_site.admin_view(self.contact_details_view),
                name="documents_document_contact_details",
            ),
            path(
                "<int:pk>/workflow/<str:step>/",
                self.admin_site.admin_view(self.workflow_view),
                name="documents_document_workflow",
            ),
        ]
        return extra + super().get_urls()

    # ---- workflow buttons ---------------------------------------------------

    def workflow_view(self, request, pk, step):
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        document = get_object_or_404(Document, pk=pk)
        if not self.has_change_permission(request, document):
            raise PermissionDenied

        uploaded = request.FILES.get("executed_file")
        steps = {
            "send": lambda: DocumentService.send_for_signature(document, request.user),
            "mark_signed": lambda: DocumentService.mark_signed_manually(
                document,
                executed_file=ContentFile(uploaded.read()) if uploaded else None,
                filename=uploaded.name if uploaded else "",
                signed_date=parse_date(request.POST.get("signed_date", "") or ""),
                user=request.user,
            ),
            "terminate": lambda: DocumentService.terminate(
                document,
                on_date=parse_date(request.POST.get("terminated_on", "") or ""),
                reason=request.POST.get("reason", ""),
            ),
            "renew": lambda: DocumentService.renew(document),
            "amend": lambda: DocumentService.amend(document, request.user),
        }
        if step not in steps:
            raise PermissionDenied

        try:
            result = steps[step]()
        except DocumentError as error:
            messages.error(request, str(error))
            return redirect(reverse("admin:documents_document_change", args=[pk]))

        done = {
            "send": lambda d: f"{d.number} sent for signature; a chase-up task was created.",
            "mark_signed": lambda d: (
                f"{d.number} recorded as signed on {d.signed_date}"
                + (f", in force until {d.expiry_date}." if d.expiry_date else ", open-ended.")
            ),
            "terminate": lambda d: f"{d.number} terminated on {d.terminated_on}.",
            "renew": lambda d: f"{d.number} renewed until {d.expiry_date}.",
            "amend": lambda d: f"Version {d.version} created as a draft.",
        }
        messages.success(request, done[step](result))
        return redirect(reverse("admin:documents_document_change", args=[result.pk]))

    def change_view(self, request, object_id, form_url="", extra_context=None):
        extra_context = extra_context or {}
        document = self.get_object(request, object_id)
        if document is not None:
            extra_context["document_obj"] = document
            extra_context["send_problems"] = (
                DocumentService.send_problems(document) if document.is_editable else []
            )
        return super().change_view(request, object_id, form_url, extra_context)

    def contact_details_view(self, request, pk):
        """Name and email of a contact, for the signer row to show as soon as
        the contact is picked (signer_defaults.js) - the same values
        DocumentSigner.fill_from_contact() copies on save.

        Only this document's client's contacts are served, mirroring the
        validation in DocumentSigner.clean().
        """
        document = get_object_or_404(Document, pk=pk)
        if not self.has_view_permission(request, document):
            raise PermissionDenied
        contact_id = request.GET.get("contact", "")
        contact = (
            Contact.objects.filter(pk=contact_id, client_id=document.client_id).first()
            if contact_id.isdigit()
            else None
        )
        if contact is None:
            raise Http404("No such contact for this document's client.")
        return JsonResponse(
            {"name": f"{contact.first_name} {contact.last_name}", "email": contact.email}
        )

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
            obj.apply_type_defaults()
        super().save_model(request, obj, form, change)

    def save_formset(self, request, form, formset, change):
        instances = formset.save(commit=False)
        for instance in instances:
            if isinstance(instance, DocumentFile) and not instance.uploaded_by_id:
                instance.uploaded_by = request.user
            instance.save()
        formset.save_m2m()
        for instance in formset.deleted_objects:
            instance.delete()

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        # Countersigned types sign with the studio's own signatory, who is
        # configured once in the studio profile rather than typed per document.
        added = DocumentService.ensure_studio_signer(form.instance)
        if added:
            messages.info(
                request,
                f"{added.name} was added as the studio's signatory "
                "(configured in Core -> Studio Profile).",
            )


class _DocumentInline(admin.TabularInline):
    """Read-only list of related documents, shown on clients and projects."""

    model = Document
    extra = 0
    show_change_link = True
    fields = ("reference", "document_type", "title", "status", "signed_date", "expiry_date")
    readonly_fields = fields
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    @admin.display(description="Document")
    def reference(self, obj):
        return obj.reference


class ClientDocumentInline(_DocumentInline):
    fk_name = "client"
    verbose_name_plural = "Documents"


class ProjectDocumentInline(_DocumentInline):
    fk_name = "project"
    verbose_name_plural = "Documents"


@admin.register(DocumentSigner)
class DocumentSignerAdmin(admin.ModelAdmin):
    """Cross-document view: who still owes a signature."""

    list_display = ("name", "email", "document", "side", "status", "signed_at")
    list_filter = ("status", "is_studio_side", "document__document_type")
    search_fields = ("name", "email", "document__number", "document__title")
    list_select_related = ("document",)
    autocomplete_fields = ["document", "contact"]

    actions = ["record_signature", "record_decline"]

    @admin.display(description="Side")
    def side(self, obj):
        return "Studio" if obj.is_studio_side else "Client"

    def has_add_permission(self, request):
        # Signers belong to a document; add them there
        return False

    @admin.action(description="Record as signed")
    def record_signature(self, request, queryset):
        self._apply(
            request, queryset, DocumentService.record_signature, "recorded as signed"
        )

    @admin.action(description="Record as declined")
    def record_decline(self, request, queryset):
        self._apply(
            request, queryset, DocumentService.record_decline, "recorded as declined"
        )

    def _apply(self, request, queryset, action, verb):
        done, skipped = [], []
        for signer in queryset.select_related("document"):
            try:
                action(signer)
            except DocumentError as error:
                skipped.append(f"{signer.name} ({error})")
                continue
            done.append(f"{signer.name} on {signer.document.reference}")
        if done:
            self.message_user(
                request, f"{verb.capitalize()}: {'; '.join(done)}", messages.SUCCESS
            )
        if skipped:
            self.message_user(request, f"Skipped: {'; '.join(skipped)}", messages.WARNING)

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("document", "contact")
