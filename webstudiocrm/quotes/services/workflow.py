"""Quote lifecycle. The only code that changes a quote's status, and the
only place where quotes update Projects and Tasks.

    draft --send--> sent --accept--> accepted
                     |----reject--> rejected
                     |----(expire_overdue)--> expired
    sent/rejected/expired --revise--> new draft (version + 1);
    sending that draft marks the previous version superseded.
"""

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from core.choices import HOME_COUNTRY, Currency, DocumentLanguage
from core.models import DocumentSequence, StudioProfile
from tasks.models import Task

from . import calculator
from .pdf import QuotePDFRenderer
from .snapshots import client_snapshot, seller_snapshot

QUOTE_PREFIX = "Q"
FOLLOW_UP_DAYS_BEFORE_EXPIRY = 3


class QuoteError(Exception):
    """A workflow step is not allowed; the message is shown to the user."""


def _quote_model():
    from quotes.models import Quote

    return Quote


class QuoteService:
    # ---- creating and editing drafts -------------------------------------

    @staticmethod
    def apply_defaults(quote):
        """Fill a new draft from the studio profile, the project and the client."""
        profile = StudioProfile.get_solo()
        project = quote.project
        client = project.client
        domestic = client.country.code == HOME_COUNTRY

        quote.language = profile.default_language if domestic else DocumentLanguage.ENGLISH
        quote.currency = profile.default_currency if domestic else Currency.EUR
        quote.validity_days = profile.default_quote_validity_days
        quote.payment_terms_days = profile.default_payment_terms_days
        quote.terms_text = quote.terms_text or (
            profile.default_quote_terms_hu
            if quote.language == DocumentLanguage.HUNGARIAN
            else profile.default_quote_terms_en
        )
        quote.title = quote.title or project.name
        if quote.contact is None:
            quote.contact = project.primary_contact or client.contacts.filter(is_primary=True).first()
        return quote

    @classmethod
    def create_for_project(cls, project, user=None, **fields):
        quote = _quote_model()(project=project, created_by=user, **fields)
        cls.apply_defaults(quote)
        quote.full_clean()
        quote.save()
        return quote

    @classmethod
    def add_service_item(cls, quote, service_item, quantity=1, **fields):
        cls._require_status(quote, "draft", "Only draft quotes can be edited.")
        item = quote.items.model(
            quote=quote,
            service_item=service_item,
            quantity=quantity,
            position=quote.items.count() + 1,
            **fields,
        )
        item.full_clean()
        item.save()
        cls.recalculate(quote)
        return item

    @staticmethod
    def recalculate(quote):
        """Recompute line amounts and stored totals of a draft."""
        if not quote.is_editable:
            return quote
        items = list(quote.items.select_related("vat_rate", "service_item"))
        for item in items:
            item.save()  # refreshes line_net and vat_percent
        totals = calculator.calculate(
            [
                calculator.LineInput(
                    i.line_net, i.vat_rate_id, i.vat_percent, i.billing_type, i.is_optional
                )
                for i in items
            ],
            quote.discount_percent,
            quote.currency,
        )
        for name in (
            "subtotal_net",
            "discount_amount",
            "net_total",
            "vat_amount",
            "gross_total",
            "recurring_monthly_net",
            "recurring_yearly_net",
        ):
            setattr(quote, name, getattr(totals, name))
        quote.save()
        return quote

    # ---- lifecycle ---------------------------------------------------------

    @classmethod
    def send_problems(cls, quote):
        """Reasons the quote can't be sent yet (empty list = ready)."""
        problems = []
        if not quote.is_editable:
            return ["Only draft quotes can be sent."]
        profile = StudioProfile.get_solo()
        if not profile.company_name:
            problems.append("Fill in the company name in Core → Studio Profile.")
        if not profile.bank_account_for(quote.currency):
            problems.append(
                f"Add a {quote.currency} bank account in Core → Studio Profile."
            )
        items = list(quote.items.select_related("vat_rate"))
        if not any(not i.is_optional for i in items):
            problems.append("Add at least one non-optional line item.")
        client = quote.client
        if any(i.vat_rate.is_reverse_charge for i in items) and not client.eu_vat_number:
            problems.append(
                f"Reverse charge needs the client's EU VAT number (Clients → {client.name})."
            )
        return problems

    @classmethod
    @transaction.atomic
    def send(cls, quote, user=None):
        """Number the quote, freeze its details, render the PDF, update project/tasks.

        Everything happens in one transaction: if any step fails, the quote
        stays a draft and the number is not used up.
        """
        Quote = _quote_model()
        # Lock the row so a double click can't send the quote twice
        Quote.objects.select_for_update().filter(pk=quote.pk).exists()
        quote.refresh_from_db()
        problems = cls.send_problems(quote)
        if problems:
            raise QuoteError(" ".join(problems))

        cls.recalculate(quote)
        today = timezone.localdate()
        if quote.revision_of_id:
            base_number = quote.revision_of.number.split("-v")[0]
            quote.number = f"{base_number}-v{quote.version}"
        else:
            quote.number = DocumentSequence.next_reference(QUOTE_PREFIX, today.year)
        quote.issue_date = today
        quote.valid_until = today + timedelta(days=quote.validity_days)
        quote.client_snapshot = client_snapshot(quote)
        quote.seller_snapshot = seller_snapshot(StudioProfile.get_solo(), quote.currency)
        quote.status = Quote.Status.SENT
        quote.sent_at = timezone.now()

        # The previous version can no longer be accepted
        previous = quote.revision_of
        if previous and previous.status == Quote.Status.SENT:
            previous.status = Quote.Status.SUPERSEDED
            previous.save(update_fields=["status", "updated_at"])
            cls._close_follow_up(previous)

        project = quote.project
        if project.status == "consultation":
            project.status = "quote_sent"
            project.save(update_fields=["status", "updated_at"])

        quote.follow_up_task = Task.objects.create(
            title=f"Follow up on quote {quote.number}",
            project=project,
            client=project.client,
            priority=Task.Priority.HIGH,
            due_date=max(today, quote.valid_until - timedelta(days=FOLLOW_UP_DAYS_BEFORE_EXPIRY)),
        )

        renderer = QuotePDFRenderer(quote)
        try:
            renderer.save()
            quote.save()
        except Exception:
            if quote.pdf:
                quote.pdf.delete(save=False)  # the transaction can't undo the file
            raise
        return quote

    @classmethod
    @transaction.atomic
    def accept(cls, quote):
        Quote = _quote_model()
        cls._require_status(quote, Quote.Status.SENT, "Only sent quotes can be accepted.")
        quote.status = Quote.Status.ACCEPTED
        quote.accepted_at = timezone.now()
        quote.save(update_fields=["status", "accepted_at", "updated_at"])
        cls._close_follow_up(quote)

        # Other open quotes of this project are no longer relevant
        for other in quote.project.quotes.exclude(pk=quote.pk).filter(
            status__in=[Quote.Status.DRAFT, Quote.Status.SENT]
        ):
            other.status = Quote.Status.SUPERSEDED
            other.save(update_fields=["status", "updated_at"])
            cls._close_follow_up(other)

        project = quote.project
        project.price = quote.net_total
        project.price_currency = quote.currency
        project.price_quote = quote
        project.has_maintenance = bool(quote.recurring_monthly_net or quote.recurring_yearly_net)
        if project.status in ("consultation", "quote_sent"):
            project.status = "contracting"
        project.save()

        Task.objects.create(
            title=f"Prepare contract for quote {quote.number}",
            project=project,
            client=project.client,
            priority=Task.Priority.HIGH,
            due_date=timezone.localdate() + timedelta(days=3),
        )
        return quote

    @classmethod
    @transaction.atomic
    def reject(cls, quote, reason=""):
        Quote = _quote_model()
        cls._require_status(quote, Quote.Status.SENT, "Only sent quotes can be rejected.")
        quote.status = Quote.Status.REJECTED
        quote.rejected_at = timezone.now()
        quote.rejection_reason = reason or quote.rejection_reason
        quote.save(update_fields=["status", "rejected_at", "rejection_reason", "updated_at"])
        cls._close_follow_up(quote)
        return quote

    @classmethod
    @transaction.atomic
    def revise(cls, quote, user=None):
        """Create the next version as a draft; the original stays valid until
        the new version is sent."""
        Quote = _quote_model()
        if quote.status not in (Quote.Status.SENT, Quote.Status.REJECTED, Quote.Status.EXPIRED):
            raise QuoteError("Only sent, rejected or expired quotes can be revised.")
        if quote.revisions.filter(status=Quote.Status.DRAFT).exists():
            raise QuoteError("A draft revision of this quote already exists.")

        revision = Quote.objects.create(
            project=quote.project,
            contact=quote.contact,
            created_by=user,
            version=quote.version + 1,
            revision_of=quote,
            language=quote.language,
            currency=quote.currency,
            title=quote.title,
            intro_text=quote.intro_text,
            terms_text=quote.terms_text,
            validity_days=quote.validity_days,
            payment_terms_days=quote.payment_terms_days,
            discount_percent=quote.discount_percent,
        )
        for item in quote.items.all():
            item.pk = None
            item.quote = revision
            item.save()
        cls.recalculate(revision)
        return revision

    @classmethod
    def expire_overdue(cls, today=None):
        """Mark sent quotes past their validity date as expired; returns the count."""
        Quote = _quote_model()
        today = today or timezone.localdate()
        return Quote.objects.filter(status=Quote.Status.SENT, valid_until__lt=today).update(
            status=Quote.Status.EXPIRED, updated_at=timezone.now()
        )

    # ---- helpers -------------------------------------------------------------

    @staticmethod
    def _require_status(quote, status, message):
        if quote.status != status:
            raise QuoteError(message)

    @staticmethod
    def _close_follow_up(quote):
        task = quote.follow_up_task
        if task and task.status != "done":
            task.status = "done"
            task.save(update_fields=["status", "updated_at"])
