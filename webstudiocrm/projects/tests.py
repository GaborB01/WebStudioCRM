from django.core.exceptions import ValidationError
from django.test import TestCase

from clients.models import Client, Contact
from .models import Project


class ProjectPrimaryContactTests(TestCase):
    def setUp(self):
        self.acme = Client.objects.create(name="Acme")
        self.globex = Client.objects.create(name="Globex")

    def make_contact(self, client):
        return Contact.objects.create(
            client=client, first_name="Anna", last_name="Kovacs", email="anna@example.com"
        )

    def make_project(self, contact):
        return Project(
            client=self.acme,
            primary_contact=contact,
            name="Website",
            project_type="website",
        )

    def test_contact_from_another_client_is_rejected(self):
        project = self.make_project(self.make_contact(self.globex))

        with self.assertRaises(ValidationError) as ctx:
            project.full_clean()

        self.assertIn("primary_contact", ctx.exception.message_dict)

    def test_contact_from_same_client_is_accepted(self):
        self.make_project(self.make_contact(self.acme)).full_clean()

    def test_project_without_contact_is_accepted(self):
        self.make_project(None).full_clean()
