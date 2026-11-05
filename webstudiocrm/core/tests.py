from decimal import Decimal

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse

from .choices import Currency
from .models import DocumentSequence, StudioBankAccount, StudioProfile, VatRate


class StudioProfileTests(TestCase):
    def test_get_solo_creates_the_profile_once(self):
        first = StudioProfile.get_solo()
        second = StudioProfile.get_solo()

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(StudioProfile.objects.count(), 1)

    def test_new_profile_has_document_defaults(self):
        profile = StudioProfile.get_solo()

        self.assertEqual(profile.default_currency, Currency.HUF)
        self.assertEqual(profile.default_language, "hu")
        self.assertEqual(profile.default_quote_validity_days, 30)

    def test_database_rejects_a_second_profile(self):
        StudioProfile.get_solo()

        with self.assertRaises(IntegrityError):
            StudioProfile.objects.create(pk=2)

    def test_bank_account_is_picked_by_currency(self):
        profile = StudioProfile.get_solo()
        eur = StudioBankAccount.objects.create(
            profile=profile, currency=Currency.EUR, bank_name="OTP", iban="HU00"
        )

        self.assertEqual(profile.bank_account_for(Currency.EUR), eur)
        self.assertIsNone(profile.bank_account_for(Currency.USD))

    def test_only_one_bank_account_per_currency(self):
        profile = StudioProfile.get_solo()
        StudioBankAccount.objects.create(profile=profile, currency="EUR", bank_name="OTP")

        with self.assertRaises(IntegrityError):
            StudioBankAccount.objects.create(profile=profile, currency="EUR", bank_name="K&H")


class VatRateTests(TestCase):
    def test_default_rates_are_created_by_migration(self):
        default = VatRate.get_default()

        self.assertEqual(default.rate, Decimal("27.00"))
        self.assertTrue(VatRate.objects.filter(is_reverse_charge=True).exists())

    def test_only_one_default_rate(self):
        with self.assertRaises(IntegrityError):
            VatRate.objects.create(name="Reduced", rate=Decimal("5"), is_default=True)

    def test_reverse_charge_must_be_zero_percent_with_notes(self):
        rate = VatRate(name="Bad", rate=Decimal("27"), is_reverse_charge=True)

        with self.assertRaises(ValidationError) as ctx:
            rate.full_clean()

        self.assertIn("rate", ctx.exception.message_dict)
        self.assertIn("note_en", ctx.exception.message_dict)

    def test_default_rate_must_be_active(self):
        rate = VatRate.get_default()
        rate.is_active = False

        with self.assertRaises(ValidationError):
            rate.full_clean()

    def test_rate_must_be_between_0_and_100(self):
        with self.assertRaises(ValidationError):
            VatRate(name="Bad", rate=Decimal("120")).full_clean()

    def test_str_shows_percentage(self):
        self.assertEqual(str(VatRate.get_default()), "Standard (27%)")


class DocumentSequenceTests(TestCase):
    def test_numbers_are_sequential(self):
        self.assertEqual(DocumentSequence.next_number("Q", 2026), 1)
        self.assertEqual(DocumentSequence.next_number("Q", 2026), 2)
        self.assertEqual(DocumentSequence.next_number("Q", 2026), 3)

    def test_numbering_restarts_each_year(self):
        DocumentSequence.next_number("Q", 2026)
        DocumentSequence.next_number("Q", 2026)

        self.assertEqual(DocumentSequence.next_number("Q", 2027), 1)

    def test_prefixes_are_numbered_separately(self):
        DocumentSequence.next_number("Q", 2026)

        self.assertEqual(DocumentSequence.next_number("C", 2026), 1)

    def test_reference_format(self):
        self.assertEqual(DocumentSequence.next_reference("Q", 2026), "Q-2026-0001")
        self.assertEqual(DocumentSequence.next_reference("Q", 2026), "Q-2026-0002")

    def test_number_is_not_used_up_when_saving_the_document_fails(self):
        DocumentSequence.next_number("Q", 2026)
        try:
            with transaction.atomic():
                DocumentSequence.next_number("Q", 2026)
                raise RuntimeError("saving the quote failed")
        except RuntimeError:
            pass

        self.assertEqual(DocumentSequence.next_number("Q", 2026), 2)

    def test_continues_from_manually_set_number(self):
        # e.g. continuing the numbering of a previous system
        DocumentSequence.objects.create(prefix="Q", year=2026, last_number=41)

        self.assertEqual(DocumentSequence.next_reference("Q", 2026), "Q-2026-0042")


class StudioProfileAdminTests(TestCase):
    def setUp(self):
        user = User.objects.create_superuser("admin", "admin@example.com", "pw")
        self.client.force_login(user)

    def test_list_page_opens_the_single_profile(self):
        response = self.client.get(reverse("admin:core_studioprofile_changelist"))

        self.assertRedirects(
            response, reverse("admin:core_studioprofile_change", args=[1])
        )

    def test_change_page_works_before_the_profile_exists(self):
        url = reverse("admin:core_studioprofile_change", args=[1])

        self.assertEqual(self.client.get(url).status_code, 200)

        response = self.client.post(
            url,
            {
                "company_name": "WebStudio Kft.",
                "default_currency": "EUR",
                "default_language": "en",
                "default_payment_terms_days": 15,
                "default_quote_validity_days": 30,
                "bank_accounts-TOTAL_FORMS": 1,
                "bank_accounts-INITIAL_FORMS": 0,
                "bank_accounts-0-currency": "EUR",
                "bank_accounts-0-bank_name": "OTP",
                "bank_accounts-0-iban": "HU42117730161111101800000000",
            },
        )

        self.assertEqual(response.status_code, 302)
        profile = StudioProfile.get_solo()
        self.assertEqual(profile.company_name, "WebStudio Kft.")
        self.assertEqual(profile.bank_account_for("EUR").bank_name, "OTP")

    def test_profile_cannot_be_added_or_deleted(self):
        StudioProfile.get_solo()

        self.assertEqual(
            self.client.get(reverse("admin:core_studioprofile_add")).status_code, 403
        )
        self.assertEqual(
            self.client.get(reverse("admin:core_studioprofile_delete", args=[1])).status_code,
            403,
        )
