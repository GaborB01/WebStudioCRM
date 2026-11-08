import tempfile
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from clients.models import Client, Contact
from core.models import DocumentSequence, StudioBankAccount, StudioProfile, VatRate
from projects.models import Project
from tasks.models import Task

from .models import Quote, QuoteLineItem, ServiceItem, ServiceItemPrice
from .services import calculator
from .services.document_text import format_date, format_money
from .services.workflow import QuoteError, QuoteService

D = Decimal
MEDIA = tempfile.mkdtemp()


class CalculatorTests(SimpleTestCase):
    def line(self, net, rate_id=1, vat="27", billing="one_off", optional=False):
        return calculator.LineInput(D(net), rate_id, D(vat), billing, optional)

    def test_line_net_rounds_to_currency_precision(self):
        self.assertEqual(calculator.line_net(D("1.5"), D("333"), 0, "HUF"), D("500"))
        self.assertEqual(calculator.line_net(D("3"), D("10.005"), 0, "EUR"), D("30.02"))
        self.assertEqual(calculator.line_net(2, D("100"), D("10"), "EUR"), D("180.00"))

    def test_single_rate_totals(self):
        totals = calculator.calculate([self.line("100000"), self.line("50000")], 0, "HUF")

        self.assertEqual(totals.net_total, D("150000"))
        self.assertEqual(totals.vat_amount, D("40500"))
        self.assertEqual(totals.gross_total, D("190500"))

    def test_mixed_rates_are_taxed_per_group(self):
        totals = calculator.calculate(
            [self.line("1000.00", 1, "27"), self.line("500.00", 2, "0")], 0, "EUR"
        )

        self.assertEqual([g.vat for g in totals.vat_groups], [D("270.00"), D("0.00")])
        self.assertEqual(totals.gross_total, D("1770.00"))

    def test_quote_discount_reduces_the_vat_base(self):
        totals = calculator.calculate([self.line("1000.00")], D("10"), "EUR")

        self.assertEqual(totals.subtotal_net, D("1000.00"))
        self.assertEqual(totals.discount_amount, D("100.00"))
        self.assertEqual(totals.net_total, D("900.00"))
        self.assertEqual(totals.vat_amount, D("243.00"))

    def test_recurring_and_optional_lines_are_not_in_the_one_off_total(self):
        totals = calculator.calculate(
            [
                self.line("1000.00"),
                self.line("50.00", billing="monthly"),
                self.line("120.00", billing="yearly"),
                self.line("999.00", optional=True),
            ],
            D("50"),
            "EUR",
        )

        self.assertEqual(totals.net_total, D("500.00"))
        self.assertEqual(totals.recurring_monthly_net, D("50.00"))
        self.assertEqual(totals.recurring_yearly_net, D("120.00"))

    def test_two_zero_percent_rates_stay_separate(self):
        totals = calculator.calculate(
            [self.line("100", 2, "0"), self.line("100", 3, "0")], 0, "EUR"
        )

        self.assertEqual(len(totals.vat_groups), 2)


class DocumentFormatTests(SimpleTestCase):
    def test_money(self):
        # Spaces are non-breaking so amounts don't wrap in the PDF
        nb = lambda text: text.replace(" ", "\u00a0")
        self.assertEqual(format_money(D("1250000"), "HUF", "hu"), nb("1 250 000 Ft"))
        self.assertEqual(format_money(D("1250000"), "HUF", "en"), nb("1,250,000 Ft"))
        self.assertEqual(format_money(D("1250.5"), "EUR", "hu"), nb("1 250,50 €"))
        self.assertEqual(format_money(D("1250.5"), "EUR", "en"), "€1,250.50")
        self.assertEqual(format_money(D("99"), "USD", "en"), "$99.00")

    def test_date(self):
        self.assertEqual(format_date(date(2026, 11, 5), "en"), "5 November 2026")
        self.assertEqual(format_date(date(2026, 11, 5), "hu"), "2026. 11. 05.")


class QuoteFixtureMixin:
    """A ready-to-quote studio: profile, bank accounts, client, project, catalog."""

    def setUp(self):
        self.profile = StudioProfile.get_solo()
        self.profile.company_name = "WebStudio Kft."
        self.profile.default_quote_terms_hu = "Az árak nettó árak."
        self.profile.default_quote_terms_en = "Prices are net."
        self.profile.save()
        for currency in ("HUF", "EUR"):
            StudioBankAccount.objects.create(
                profile=self.profile, currency=currency, bank_name="OTP", iban=f"HU00{currency}"
            )

        self.client_obj = Client.objects.create(name="Pékség Kft.", country="HU")
        self.contact = Contact.objects.create(
            client=self.client_obj, first_name="Anna", last_name="Kovács",
            email="anna@pekseg.hu", is_primary=True,
        )
        self.project = Project.objects.create(
            client=self.client_obj, name="Webshop", project_type="webshop"
        )

        self.design = ServiceItem.objects.create(
            name_en="UI design", name_hu="Arculat- és UI tervezés", unit="hour"
        )
        ServiceItemPrice.objects.create(service_item=self.design, currency="HUF", unit_price=D("15000"))
        ServiceItemPrice.objects.create(service_item=self.design, currency="EUR", unit_price=D("40"))
        self.maintenance = ServiceItem.objects.create(
            name_en="Maintenance", name_hu="Karbantartás", unit="month", billing_type="monthly"
        )
        ServiceItemPrice.objects.create(service_item=self.maintenance, currency="HUF", unit_price=D("25000"))

        self.standard = VatRate.get_default()
        self.reverse_charge = VatRate.objects.get(name="EU reverse charge")

    def make_quote(self, project=None):
        quote = QuoteService.create_for_project(project or self.project)
        QuoteService.add_service_item(quote, self.design, quantity=D("10"))
        return quote


class QuoteDefaultsTests(QuoteFixtureMixin, TestCase):
    def test_domestic_client_gets_studio_defaults(self):
        quote = QuoteService.create_for_project(self.project)

        self.assertEqual((quote.language, quote.currency), ("hu", "HUF"))
        self.assertEqual(quote.terms_text, "Az árak nettó árak.")
        self.assertEqual(quote.contact, self.contact)
        self.assertEqual(quote.title, "Webshop")
        self.assertIsNone(quote.number)

    def test_foreign_client_gets_english_and_eur(self):
        client = Client.objects.create(name="Bäckerei GmbH", country="DE", eu_vat_number="DE123456789")
        project = Project.objects.create(client=client, name="Site", project_type="website")

        quote = QuoteService.create_for_project(project)

        self.assertEqual((quote.language, quote.currency), ("en", "EUR"))
        self.assertEqual(quote.terms_text, "Prices are net.")

    def test_line_is_filled_from_catalog_in_quote_language_and_currency(self):
        quote = self.make_quote()
        line = quote.items.get()

        self.assertEqual(line.description, "Arculat- és UI tervezés")
        self.assertEqual(line.unit_price, D("15000"))
        self.assertEqual(line.unit, "hour")
        self.assertEqual(line.vat_rate, self.standard)
        self.assertEqual(line.line_net, D("150000"))

    def test_eu_business_client_lines_default_to_reverse_charge(self):
        client = Client.objects.create(name="Bäckerei GmbH", country="DE", eu_vat_number="DE123456789")
        project = Project.objects.create(client=client, name="Site", project_type="website")

        quote = self.make_quote(project)

        self.assertEqual(quote.items.get().vat_rate, self.reverse_charge)
        self.assertEqual(quote.vat_amount, D("0.00"))

    def test_missing_catalog_price_in_quote_currency_is_reported(self):
        quote = QuoteService.create_for_project(self.project, currency="USD")
        quote.currency = "USD"
        quote.save()

        with self.assertRaises(ValidationError) as ctx:
            QuoteService.add_service_item(quote, self.design)

        self.assertIn("unit_price", ctx.exception.message_dict)

    def test_contact_must_belong_to_project_client(self):
        other = Client.objects.create(name="Other")
        stranger = Contact.objects.create(client=other, first_name="X", last_name="Y", email="x@y.z")

        with self.assertRaises(ValidationError):
            QuoteService.create_for_project(self.project, contact=stranger)


@override_settings(MEDIA_ROOT=MEDIA)
class QuoteWorkflowTests(QuoteFixtureMixin, TestCase):
    def test_send_numbers_freezes_and_renders(self):
        quote = QuoteService.send(self.make_quote())

        year = timezone.localdate().year
        self.assertEqual(quote.number, f"Q-{year}-0001")
        self.assertEqual(quote.status, Quote.Status.SENT)
        self.assertEqual(quote.valid_until, quote.issue_date + timedelta(days=30))
        self.assertEqual(quote.client_snapshot["name"], "Pékség Kft.")
        self.assertEqual(quote.seller_snapshot["bank"]["iban"], "HU00HUF")
        with quote.pdf.open("rb") as f:
            self.assertEqual(f.read(5), b"%PDF-")

    def test_send_updates_project_and_creates_follow_up_task(self):
        quote = QuoteService.send(self.make_quote())

        self.project.refresh_from_db()
        self.assertEqual(self.project.status, "quote_sent")
        task = quote.follow_up_task
        self.assertEqual(task.project, self.project)
        self.assertEqual(task.due_date, quote.valid_until - timedelta(days=3))

    def test_sent_quote_keeps_old_client_details(self):
        quote = QuoteService.send(self.make_quote())
        self.client_obj.address = "New address"
        self.client_obj.save()

        quote.refresh_from_db()
        self.assertEqual(quote.client_snapshot["address"], "")

    def test_send_is_blocked_with_reasons(self):
        StudioBankAccount.objects.filter(currency="HUF").delete()
        quote = QuoteService.create_for_project(self.project)

        problems = QuoteService.send_problems(quote)

        self.assertEqual(len(problems), 2)  # no HUF bank account, no items
        with self.assertRaises(QuoteError):
            QuoteService.send(quote)

    def test_reverse_charge_requires_client_eu_vat_number(self):
        quote = QuoteService.create_for_project(self.project)
        QuoteService.add_service_item(quote, self.design, vat_rate=self.reverse_charge)

        self.assertIn("EU VAT number", " ".join(QuoteService.send_problems(quote)))

    def test_failed_pdf_keeps_draft_and_number(self):
        quote = self.make_quote()
        with mock.patch(
            "quotes.services.workflow.QuotePDFRenderer.render", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                QuoteService.send(quote)

        quote.refresh_from_db()
        self.assertEqual(quote.status, Quote.Status.DRAFT)
        self.assertFalse(DocumentSequence.objects.exists())
        self.assertFalse(Task.objects.exists())

    def test_accept_sets_project_price_with_quote_number(self):
        quote = QuoteService.create_for_project(self.project)
        QuoteService.add_service_item(quote, self.design, quantity=D("10"))
        QuoteService.add_service_item(quote, self.maintenance)
        quote = QuoteService.send(quote)

        QuoteService.accept(quote)

        self.project.refresh_from_db()
        self.assertEqual(self.project.price, D("150000"))
        self.assertEqual(self.project.price_quote, quote)
        self.assertTrue(self.project.has_maintenance)
        self.assertEqual(self.project.status, "contracting")
        self.assertEqual(self.project.price_display, f"150,000.00 HUF ({quote.number})")
        quote.follow_up_task.refresh_from_db()
        self.assertEqual(quote.follow_up_task.status, "done")
        self.assertTrue(Task.objects.filter(title__startswith="Prepare contract").exists())

    def test_reject_closes_follow_up(self):
        quote = QuoteService.send(self.make_quote())

        QuoteService.reject(quote, "Too expensive")

        self.assertEqual(quote.rejection_reason, "Too expensive")
        quote.follow_up_task.refresh_from_db()
        self.assertEqual(quote.follow_up_task.status, "done")

    def test_revision_flow(self):
        original = QuoteService.send(self.make_quote())

        revision = QuoteService.revise(original)
        self.assertEqual((revision.version, revision.status), (2, Quote.Status.DRAFT))
        self.assertEqual(revision.items.count(), 1)
        original.refresh_from_db()
        self.assertEqual(original.status, Quote.Status.SENT)  # still valid until v2 is sent

        with self.assertRaises(QuoteError):
            QuoteService.revise(original)  # only one open draft revision

        QuoteService.send(revision)
        original.refresh_from_db()
        self.assertEqual(original.status, Quote.Status.SUPERSEDED)
        self.assertEqual(revision.number, f"{original.number}-v2")

    def test_wrong_transitions_are_refused(self):
        draft = self.make_quote()
        with self.assertRaises(QuoteError):
            QuoteService.accept(draft)
        with self.assertRaises(QuoteError):
            QuoteService.revise(draft)

    def test_expire_overdue(self):
        quote = QuoteService.send(self.make_quote())

        self.assertEqual(QuoteService.expire_overdue(quote.valid_until), 0)
        self.assertEqual(QuoteService.expire_overdue(quote.valid_until + timedelta(days=1)), 1)
        quote.refresh_from_db()
        self.assertEqual(quote.status, Quote.Status.EXPIRED)

    def test_sent_line_items_keep_their_vat_percent(self):
        quote = QuoteService.send(self.make_quote())
        self.standard.rate = D("25")
        self.standard.save()

        QuoteService.recalculate(quote)  # no-op for sent quotes

        self.assertEqual(quote.items.get().vat_percent, D("27.00"))


@override_settings(MEDIA_ROOT=MEDIA)
class QuoteAdminTests(QuoteFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(User.objects.create_superuser("admin", "a@a.hu", "pw"))

    def line_formset(self, rows, initial=0):
        data = {
            "items-TOTAL_FORMS": str(len(rows)),
            "items-INITIAL_FORMS": str(initial),
        }
        for i, row in enumerate(rows):
            for key, value in row.items():
                data[f"items-{i}-{key}"] = value
        return data

    def change_data(self, quote, **extra):
        data = {
            "project": quote.project_id,
            "contact": quote.contact_id or "",
            "title": quote.title,
            "language": quote.language,
            "currency": quote.currency,
            "intro_text": "",
            "terms_text": quote.terms_text,
            "validity_days": quote.validity_days,
            "payment_terms_days": quote.payment_terms_days,
            "discount_percent": "0",
        }
        data.update(extra)
        return data

    def test_full_flow_through_admin(self):
        # Project page offers "Create quote"
        project_page = self.client.get(reverse("admin:projects_project_change", args=[self.project.pk]))
        self.assertContains(project_page, f"/admin/quotes/quote/add/?project={self.project.pk}")

        # Add page: only project + contact, then redirect to the full form
        response = self.client.post(
            reverse("admin:quotes_quote_add"), {"project": self.project.pk, "contact": ""}
        )
        quote = Quote.objects.get()
        self.assertRedirects(response, reverse("admin:quotes_quote_change", args=[quote.pk]))
        self.assertEqual(quote.language, "hu")

        # Add a catalog line (description/price filled automatically) and a manual line
        data = self.change_data(quote, discount_percent="10")
        data.update(self.line_formset([
            {"position": "1", "service_item": self.design.pk, "quantity": "10", "discount_percent": "0"},
            {"position": "2", "description": "Domain registration", "quantity": "1",
             "unit_price": "5000", "discount_percent": "0"},
        ]))
        response = self.client.post(reverse("admin:quotes_quote_change", args=[quote.pk]), data)
        self.assertEqual(response.status_code, 302, getattr(response, "context", None) and response.context["errors"])
        quote.refresh_from_db()
        self.assertEqual(quote.net_total, D("139500"))  # (150000 + 5000) * 0.9
        self.assertEqual(quote.gross_total, D("177165"))

        # Preview, send, download, accept
        self.assertEqual(self.client.get(reverse("admin:quotes_quote_preview", args=[quote.pk]))["Content-Type"], "application/pdf")
        self.client.post(reverse("admin:quotes_quote_workflow", args=[quote.pk, "send"]))
        quote.refresh_from_db()
        self.assertEqual(quote.status, Quote.Status.SENT)
        pdf = self.client.get(reverse("admin:quotes_quote_pdf", args=[quote.pk]))
        self.assertEqual(b"".join(pdf.streaming_content)[:5], b"%PDF-")

        self.client.post(reverse("admin:quotes_quote_workflow", args=[quote.pk, "accept"]))
        self.project.refresh_from_db()
        self.assertEqual(self.project.price_display, f"139,500.00 HUF ({quote.number})")

        # Project list shows the price with the quote number
        self.assertContains(self.client.get(reverse("admin:projects_project_changelist")), quote.number)

    def test_change_page_shows_send_problems(self):
        quote = QuoteService.create_for_project(self.project)

        response = self.client.get(reverse("admin:quotes_quote_change", args=[quote.pk]))

        self.assertContains(response, "Add at least one non-optional line item.")

    def test_sent_quote_is_read_only_and_cannot_be_deleted(self):
        quote = QuoteService.send(self.make_quote())

        response = self.client.get(reverse("admin:quotes_quote_change", args=[quote.pk]))
        self.assertNotContains(response, 'name="title"')
        self.assertNotContains(response, 'name="items-0-quantity"')
        self.assertEqual(
            self.client.get(reverse("admin:quotes_quote_delete", args=[quote.pk])).status_code, 403
        )

    def test_workflow_requires_post(self):
        quote = self.make_quote()

        response = self.client.get(reverse("admin:quotes_quote_workflow", args=[quote.pk, "send"]))

        self.assertEqual(response.status_code, 405)

    def test_workflow_error_is_shown_as_message(self):
        quote = QuoteService.create_for_project(self.project)

        response = self.client.post(
            reverse("admin:quotes_quote_workflow", args=[quote.pk, "send"]), follow=True
        )

        self.assertContains(response, "Add at least one non-optional line item.")

    def test_manual_price_edit_removes_quote_link(self):
        quote = QuoteService.send(self.make_quote())
        QuoteService.accept(quote)
        self.project.refresh_from_db()

        self.client.post(
            reverse("admin:projects_project_change", args=[self.project.pk]),
            {
                "client": self.client_obj.pk, "primary_contact": "", "name": "Webshop",
                "description": "", "status": "contracting", "project_type": "webshop",
                "price": "200000", "price_currency": "HUF", "start_date": "", "deadline": "",
                "quotes-TOTAL_FORMS": "1", "quotes-INITIAL_FORMS": "1",
                "quotes-0-id": quote.pk, "quotes-0-project": self.project.pk,
                "tasks-TOTAL_FORMS": "0", "tasks-INITIAL_FORMS": "0",
            },
        )

        self.project.refresh_from_db()
        self.assertEqual(self.project.price, D("200000"))
        self.assertIsNone(self.project.price_quote)
