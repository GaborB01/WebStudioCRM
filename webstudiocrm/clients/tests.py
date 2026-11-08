from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.forms import inlineformset_factory
from django.test import TestCase

from .admin import ContactInlineFormSet
from .models import Client, Contact


def make_contact(client, **kwargs):
    defaults = {"first_name": "Anna", "last_name": "Kovacs", "email": "anna@example.com"}
    defaults.update(kwargs)
    return Contact.objects.create(client=client, **defaults)


class ContactDeletionTests(TestCase):
    def test_deleting_client_deletes_its_contacts(self):
        client = Client.objects.create(name="Acme")
        make_contact(client)

        client.delete()

        self.assertFalse(Contact.objects.exists())


class PrimaryContactTests(TestCase):
    def setUp(self):
        self.client_obj = Client.objects.create(name="Acme")

    def test_new_contact_is_not_primary_by_default(self):
        self.assertFalse(make_contact(self.client_obj).is_primary)

    def test_database_rejects_second_primary_contact(self):
        make_contact(self.client_obj, is_primary=True)

        with self.assertRaises(IntegrityError):
            make_contact(self.client_obj, email="b@example.com", is_primary=True)

    def test_validation_rejects_second_primary_contact(self):
        make_contact(self.client_obj, is_primary=True)
        second = Contact(
            client=self.client_obj,
            first_name="Bela",
            last_name="Nagy",
            email="bela@example.com",
            is_primary=True,
        )

        with self.assertRaisesMessage(
            ValidationError, "This client already has a primary contact."
        ):
            second.full_clean()

    def test_different_clients_can_each_have_a_primary_contact(self):
        other = Client.objects.create(name="Globex")
        make_contact(self.client_obj, is_primary=True)
        make_contact(other, is_primary=True)

        self.assertEqual(Contact.objects.filter(is_primary=True).count(), 2)


class ContactInlineFormSetTests(TestCase):
    FormSet = inlineformset_factory(
        Client,
        Contact,
        formset=ContactInlineFormSet,
        fields=("first_name", "last_name", "email", "is_primary"),
        extra=2,
    )

    def build_formset(self, client, first_primary, second_primary):
        data = {
            "contacts-TOTAL_FORMS": "2",
            "contacts-INITIAL_FORMS": "0",
            "contacts-0-first_name": "Anna",
            "contacts-0-last_name": "Kovacs",
            "contacts-0-email": "anna@example.com",
            "contacts-1-first_name": "Bela",
            "contacts-1-last_name": "Nagy",
            "contacts-1-email": "bela@example.com",
        }
        if first_primary:
            data["contacts-0-is_primary"] = "on"
        if second_primary:
            data["contacts-1-is_primary"] = "on"
        return self.FormSet(data, instance=client, prefix="contacts")

    def test_rejects_two_primary_contacts_in_one_submission(self):
        client = Client.objects.create(name="Acme")
        formset = self.build_formset(client, first_primary=True, second_primary=True)

        self.assertFalse(formset.is_valid())
        self.assertIn(
            "Only one contact can be marked as primary.", formset.non_form_errors()
        )

    def test_accepts_one_primary_contact(self):
        client = Client.objects.create(name="Acme")
        formset = self.build_formset(client, first_primary=True, second_primary=False)

        self.assertTrue(formset.is_valid())


class ClientVatTests(TestCase):
    def make(self, country, eu_vat_number=""):
        return Client(name="Acme", country=country, eu_vat_number=eu_vat_number)

    def test_vat_treatment_by_country_and_vat_number(self):
        from core.choices import VatTreatment

        self.assertEqual(self.make("HU").vat_treatment, VatTreatment.STANDARD)
        self.assertEqual(self.make("DE", "DE123456789").vat_treatment, VatTreatment.EU_REVERSE_CHARGE)
        # EU client without a VAT number pays Hungarian VAT
        self.assertEqual(self.make("DE").vat_treatment, VatTreatment.STANDARD)
        self.assertEqual(self.make("US").vat_treatment, VatTreatment.OUTSIDE_EU)

    def test_new_client_defaults_to_hungary(self):
        self.assertEqual(Client.objects.create(name="Acme").country.code, "HU")

    def test_eu_vat_number_is_normalised(self):
        client = self.make("DE", " de 123 456 789 ")
        client.full_clean()

        self.assertEqual(client.eu_vat_number, "DE123456789")

    def test_eu_vat_number_must_match_country(self):
        with self.assertRaises(ValidationError) as ctx:
            self.make("DE", "FR12345678901").full_clean()

        self.assertIn("eu_vat_number", ctx.exception.message_dict)

    def test_greece_uses_el_prefix(self):
        self.make("GR", "EL123456789").full_clean()

    def test_non_eu_client_cannot_have_eu_vat_number(self):
        with self.assertRaises(ValidationError):
            self.make("US", "US123456").full_clean()
