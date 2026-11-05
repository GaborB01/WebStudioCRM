from datetime import date

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from clients.models import Client, Contact
from projects.models import Project
from .models import Task


class TaskClientProjectTests(TestCase):
    def setUp(self):
        self.acme = Client.objects.create(name="Acme")
        self.globex = Client.objects.create(name="Globex")
        self.project = Project.objects.create(
            client=self.acme, name="Website", project_type="website"
        )

    def test_project_from_another_client_is_rejected(self):
        task = Task(title="Design", client=self.globex, project=self.project)

        with self.assertRaises(ValidationError) as ctx:
            task.full_clean()

        self.assertIn("project", ctx.exception.message_dict)

    def test_project_from_same_client_is_accepted(self):
        Task(title="Design", client=self.acme, project=self.project).full_clean()

    def test_client_only_task_is_accepted(self):
        Task(title="Call back", client=self.globex).full_clean()

    def test_client_is_filled_from_project_on_save(self):
        task = Task.objects.create(title="Design", project=self.project)

        self.assertEqual(task.client, self.acme)

    def test_changing_project_client_moves_its_tasks(self):
        task = Task.objects.create(title="Design", project=self.project)

        self.project.client = self.globex
        self.project.save()

        task.refresh_from_db()
        self.assertEqual(task.client, self.globex)


class TaskOrderingTests(TestCase):
    def test_orders_by_due_date_then_most_important_first(self):
        Task.objects.create(title="no date", priority=Task.Priority.URGENT)
        Task.objects.create(title="low", priority=Task.Priority.LOW, due_date=date(2026, 1, 1))
        Task.objects.create(title="urgent", priority=Task.Priority.URGENT, due_date=date(2026, 1, 1))
        Task.objects.create(title="high", priority=Task.Priority.HIGH, due_date=date(2026, 1, 1))
        Task.objects.create(title="later", priority=Task.Priority.MEDIUM, due_date=date(2026, 2, 1))

        self.assertEqual(
            [t.title for t in Task.objects.all()],
            ["urgent", "high", "low", "later", "no date"],
        )

    def test_str_uses_status_label(self):
        self.assertEqual(str(Task(title="Design", status="on_hold")), "On Hold - Design")


class ClientFilteredAutocompleteTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser("admin", "admin@example.com", "pw")
        self.client.force_login(self.user)
        self.acme = Client.objects.create(name="Acme")
        self.globex = Client.objects.create(name="Globex")

    def autocomplete(self, model_name, field_name, **extra):
        app_label = {"project": "projects", "task": "tasks"}[model_name]
        params = {
            "app_label": app_label,
            "model_name": model_name,
            "field_name": field_name,
            "term": "",
            **extra,
        }
        response = self.client.get(reverse("admin:autocomplete"), params)
        self.assertEqual(response.status_code, 200)
        return sorted(r["text"] for r in response.json()["results"])

    def test_project_contacts_are_limited_to_selected_client(self):
        Contact.objects.create(client=self.acme, first_name="Anna", last_name="K", email="a@x.com")
        Contact.objects.create(client=self.globex, first_name="Bela", last_name="N", email="b@x.com")

        self.assertEqual(
            self.autocomplete("project", "primary_contact", client=self.acme.pk),
            ["Anna K (Acme)"],
        )

    def test_task_projects_are_limited_to_selected_client(self):
        Project.objects.create(client=self.acme, name="Site", project_type="website")
        Project.objects.create(client=self.globex, name="Shop", project_type="webshop")

        self.assertEqual(
            self.autocomplete("task", "project", client=self.acme.pk), ["Site - Acme"]
        )

    def test_without_client_all_results_are_listed(self):
        Project.objects.create(client=self.acme, name="Site", project_type="website")
        Project.objects.create(client=self.globex, name="Shop", project_type="webshop")

        self.assertEqual(
            self.autocomplete("task", "project"), ["Shop - Globex", "Site - Acme"]
        )

    def test_invalid_client_value_is_ignored(self):
        Project.objects.create(client=self.acme, name="Site", project_type="website")

        self.assertEqual(
            self.autocomplete("task", "project", client="abc"), ["Site - Acme"]
        )

    def test_admin_forms_load_the_filter_script(self):
        for url in (reverse("admin:projects_project_add"), reverse("admin:tasks_task_add")):
            self.assertContains(
                self.client.get(url), "clients/js/client_filtered_autocomplete.js"
            )
