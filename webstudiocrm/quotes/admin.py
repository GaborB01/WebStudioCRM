from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.http import FileResponse, HttpResponse, HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect
from django.urls import path, reverse
from django.utils.html import format_html, format_html_join

from .models import Quote, QuoteLineItem, ServiceItem, ServiceItemPrice
from .services.pdf import QuotePDFRenderer
from .services.workflow import QuoteError, QuoteService


class ServiceItemPriceInline(admin.TabularInline):
    model = ServiceItemPrice
    extra = 1


@admin.register(ServiceItem)
class ServiceItemAdmin(admin.ModelAdmin):
    list_display = ("name_en", "name_hu", "category", "unit", "billing_type", "prices_display", "is_active")
    list_filter = ("category", "billing_type", "is_active")
    search_fields = ("name_en", "name_hu")
    list_editable = ("is_active",)
    inlines = [ServiceItemPriceInline]

    @admin.display(description="Prices")
    def prices_display(self, obj):
        return ", ".join(f"{p.unit_price:,.2f} {p.currency}" for p in obj.prices.all())

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("prices")

    def get_search_results(self, request, queryset, search_term):
        # Only offer active services when picking one on a quote line
        queryset, may_have_duplicates = super().get_search_results(request, queryset, search_term)
        if "field_name" in request.GET:
            queryset = queryset.filter(is_active=True)
        return queryset, may_have_duplicates


class QuoteLineItemInline(admin.TabularInline):
    model = QuoteLineItem
    extra = 1
    autocomplete_fields = ["service_item"]
    fields = (
        "position",
        "service_item",
        "description",
        "quantity",
        "unit",
        "unit_price",
        "discount_percent",
        "vat_rate",
        "billing_type",
        "is_optional",
        "line_net",
    )
    readonly_fields = ("line_net",)

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        field = super().formfield_for_dbfield(db_field, request, **kwargs)
        if db_field.name == "description":
            field.widget.attrs.update(rows=2, cols=40)
        return field

    def _editable(self, obj):
        return obj is None or obj.is_editable

    def has_add_permission(self, request, obj=None):
        return self._editable(obj) and super().has_add_permission(request, obj)

    def has_change_permission(self, request, obj=None):
        return self._editable(obj) and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return self._editable(obj) and super().has_delete_permission(request, obj)


@admin.register(Quote)
class QuoteAdmin(admin.ModelAdmin):
    change_form_template = "admin/quotes/quote/change_form.html"
    list_display = (
        "reference",
        "project",
        "client_name",
        "status",
        "version",
        "gross_display",
        "valid_until",
    )
    list_filter = ("status", "currency", "language")
    search_fields = ("number", "title", "project__name", "project__client__name")
    autocomplete_fields = ["project", "contact"]
    list_select_related = ("project__client",)
    readonly_fields = (
        "number",
        "status",
        "version",
        "revision_of",
        "issue_date",
        "valid_until",
        "totals_display",
        "pdf_link",
        "sent_at",
        "accepted_at",
        "rejected_at",
        "created_by",
    )

    @admin.display(description="Quote", ordering="number")
    def reference(self, obj):
        return obj.reference

    @admin.display(description="Client", ordering="project__client__name")
    def client_name(self, obj):
        return obj.project.client.name

    @admin.display(description="Total (gross)", ordering="gross_total")
    def gross_display(self, obj):
        return f"{obj.gross_total:,.2f} {obj.currency}"

    @admin.display(description="Totals")
    def totals_display(self, obj):
        rows = [
            ("Subtotal (net)", obj.subtotal_net),
            ("Discount", -obj.discount_amount),
            ("Total (net)", obj.net_total),
            ("VAT", obj.vat_amount),
            ("Total (gross)", obj.gross_total),
            ("Monthly (net)", obj.recurring_monthly_net),
            ("Yearly (net)", obj.recurring_yearly_net),
        ]
        return format_html(
            "<table>{}</table>",
            format_html_join(
                "",
                "<tr><td>{}</td><td style='text-align:right'>{} {}</td></tr>",
                ((label, f"{value:,.2f}", obj.currency) for label, value in rows),
            ),
        )

    @admin.display(description="PDF")
    def pdf_link(self, obj):
        if not obj.pdf:
            return "-"
        url = reverse("admin:quotes_quote_pdf", args=[obj.pk])
        return format_html('<a href="{}">{}</a>', url, obj.pdf.name.rsplit("/", 1)[-1])

    def get_fieldsets(self, request, obj=None):
        if obj is None:
            # Add page: only what's needed; the rest is filled from defaults
            return ((None, {"fields": ("project", "contact")}),)
        fieldsets = [
            ("Quote", {"fields": ("project", "contact", "number", "status", "version", "revision_of")}),
            (
                "Content",
                {"fields": ("title", "language", "currency", "intro_text", "terms_text")},
            ),
            (
                "Commercial Terms",
                {"fields": ("validity_days", "payment_terms_days", "discount_percent", "totals_display")},
            ),
            ("Document", {"fields": ("issue_date", "valid_until", "pdf_link", "sent_at", "created_by")}),
        ]
        if obj.status in (Quote.Status.SENT, Quote.Status.ACCEPTED, Quote.Status.REJECTED):
            fieldsets.append(("Outcome", {"fields": ("accepted_at", "rejected_at", "rejection_reason")}))
        return fieldsets

    def get_readonly_fields(self, request, obj=None):
        if obj is None or obj.is_editable:
            return self.readonly_fields
        # Sent quotes are frozen; only the rejection reason can still be noted
        editable = {"rejection_reason"} if obj.status == Quote.Status.SENT else set()
        all_fields = [f for fs in self.get_fieldsets(request, obj) for f in fs[1]["fields"]]
        return [f for f in all_fields if f not in editable]

    def get_inlines(self, request, obj):
        return [QuoteLineItemInline] if obj else []

    def has_delete_permission(self, request, obj=None):
        # Sent quotes are business records; only drafts can be deleted
        if obj is not None and not obj.is_editable:
            return False
        return super().has_delete_permission(request, obj)

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
            QuoteService.apply_defaults(obj)
        elif "currency" in form.changed_data and obj.items.exists():
            messages.warning(
                request,
                "Currency changed: existing line prices were not converted. Check them.",
            )
        super().save_model(request, obj, form, change)

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        QuoteService.recalculate(form.instance)

    def response_add(self, request, obj, post_url_continue=None):
        # Continue to the full form to add line items
        messages.success(request, f"Quote created for {obj.project}. Now add the line items.")
        return redirect(reverse("admin:quotes_quote_change", args=[obj.pk]))

    # ---- workflow buttons ----------------------------------------------------

    def get_urls(self):
        workflow = [
            path(
                "<int:pk>/workflow/<str:step>/",
                self.admin_site.admin_view(self.workflow_view),
                name="quotes_quote_workflow",
            ),
            path("<int:pk>/pdf/", self.admin_site.admin_view(self.pdf_view), name="quotes_quote_pdf"),
            path(
                "<int:pk>/preview/",
                self.admin_site.admin_view(self.preview_view),
                name="quotes_quote_preview",
            ),
        ]
        return workflow + super().get_urls()

    def _get_quote(self, request, pk):
        quote = get_object_or_404(Quote, pk=pk)
        if not self.has_view_permission(request, quote):
            raise PermissionDenied
        return quote

    def workflow_view(self, request, pk, step):
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        quote = self._get_quote(request, pk)
        if not self.has_change_permission(request, quote):
            raise PermissionDenied
        steps = {
            "send": lambda: QuoteService.send(quote, request.user),
            "accept": lambda: QuoteService.accept(quote),
            "reject": lambda: QuoteService.reject(quote, request.POST.get("reason", "")),
            "revise": lambda: QuoteService.revise(quote, request.user),
        }
        if step not in steps:
            raise PermissionDenied
        try:
            result = steps[step]()
        except QuoteError as error:
            messages.error(request, str(error))
            return redirect(reverse("admin:quotes_quote_change", args=[pk]))
        done = {
            "send": f"Quote {result.number} sent: PDF generated, project and tasks updated.",
            "accept": f"Quote {result.number} accepted: project price set to {result.project.price_display}.",
            "reject": f"Quote {result.number} rejected.",
            "revise": f"Version {result.version} created as a draft.",
        }
        messages.success(request, done[step])
        return redirect(reverse("admin:quotes_quote_change", args=[result.pk]))

    def pdf_view(self, request, pk):
        quote = self._get_quote(request, pk)
        if not quote.pdf:
            raise PermissionDenied
        return FileResponse(quote.pdf.open("rb"), content_type="application/pdf",
                            filename=quote.pdf.name.rsplit("/", 1)[-1])

    def preview_view(self, request, pk):
        quote = self._get_quote(request, pk)
        response = HttpResponse(QuotePDFRenderer(quote).render(), content_type="application/pdf")
        response["Content-Disposition"] = f'inline; filename="preview-{quote.pk}.pdf"'
        return response

    def change_view(self, request, object_id, form_url="", extra_context=None):
        extra_context = extra_context or {}
        quote = self.get_object(request, object_id)
        if quote is not None:
            extra_context["quote_obj"] = quote
            extra_context["send_problems"] = QuoteService.send_problems(quote) if quote.is_editable else []
        return super().change_view(request, object_id, form_url, extra_context)


class QuoteInline(admin.TabularInline):
    """Read-only list of a project's quotes on the project page."""

    model = Quote
    fk_name = "project"
    extra = 0
    show_change_link = True
    fields = ("reference", "version", "status", "gross_display", "valid_until")
    readonly_fields = fields
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False

    @admin.display(description="Quote")
    def reference(self, obj):
        return obj.reference

    @admin.display(description="Total (gross)")
    def gross_display(self, obj):
        return f"{obj.gross_total:,.2f} {obj.currency}"
