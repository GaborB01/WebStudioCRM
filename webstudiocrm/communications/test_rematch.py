"""Scoped re-matching, the background jobs and what deleting a contact does."""

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from clients.models import Client, ClientEmailDomain, Contact, ContactEmail
from projects.models import Project

from .choices import Direction, MatchMethod
from .models import Activity, EmailMessage, EmailParty
from .services import rematch as scopes
from .services.matching import match_message
from .tasks import queue, rematch_for_client, rematch_for_contact, rematch_messages
from .test_matching import MatchingFixtureMixin


class EmailPartyTests(MatchingFixtureMixin, TestCase):
    def test_parties_mirror_the_headers(self):
        message = self.stored(self.inbound(cc=("Boss@Acme.com",)))

        parties = {(p.role, p.address) for p in message.parties.all()}
        self.assertEqual(parties, {
            (EmailParty.Role.FROM, "anna@acme.com"),
            (EmailParty.Role.TO, "hello@studio.hu"),
            (EmailParty.Role.CC, "boss@acme.com"),
        })

    def test_parties_follow_changes(self):
        message = self.stored(self.inbound())
        message.cc_addresses = [{"name": "Boss", "email": "boss@acme.com"}]
        message.to_addresses = []
        message.save()

        self.assertEqual(
            sorted(message.parties.values_list("address", flat=True)),
            ["anna@acme.com", "boss@acme.com"],
        )
        self.assertEqual(message.parties.get(role="cc").name, "Boss")


class ScopeTests(MatchingFixtureMixin, TestCase):
    def test_waiting_means_unmatched_flagged_or_stale(self):
        unmatched = self.stored(self.inbound(sender="x@gmail.com"))
        flagged = self.stored(self.inbound(), client=self.acme, match_method=MatchMethod.DOMAIN,
                              needs_review=True)
        stale = self.stored(self.inbound(), client=self.acme, match_method=MatchMethod.STALE)
        confirmed = self.stored(self.inbound(), client=self.acme, match_method=MatchMethod.CONTACT)
        manual = self.stored(self.inbound())
        manual.assign(self.acme)

        self.assertEqual(set(scopes.waiting_messages()), {unmatched, flagged, stale})

    def test_contact_scope_is_emails_involving_its_addresses(self):
        new = Contact.objects.create(client=self.acme, first_name="N", last_name="N",
                                     email="new@acme.com")
        ContactEmail.objects.create(contact=new, email="new.personal@gmail.com")
        by_main = self.stored(self.inbound(sender="new@acme.com"))
        by_alias = self.stored(self.outbound("New.Personal@gmail.com"))
        self.stored(self.inbound(sender="other@gmail.com"))

        self.assertEqual(set(scopes.contact_scope(new)), {by_main, by_alias})

    def test_client_scope_covers_contacts_and_domains_with_subdomains(self):
        ClientEmailDomain.objects.create(client=self.acme, domain="acme.com")
        by_contact = self.stored(self.inbound(sender="anna@acme.com"))
        by_domain = self.stored(self.inbound(sender="someone@mail.acme.com"))
        self.stored(self.inbound(sender="x@notacme.com"))
        self.stored(self.inbound(sender="gus@globex.com"))

        self.assertEqual(set(scopes.client_scope(self.acme)), {by_contact, by_domain})


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
class TaskTests(MatchingFixtureMixin, TestCase):
    def test_rematch_for_contact_matches_only_waiting_emails_in_scope(self):
        waiting = self.stored(self.inbound(sender="new@acme.com"))
        other = self.stored(self.inbound(sender="x@gmail.com"))
        new = Contact.objects.create(client=self.acme, first_name="N", last_name="N",
                                     email="new@acme.com")

        report = rematch_for_contact.delay(new.pk).result

        waiting.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(waiting.client, self.acme)
        self.assertEqual(waiting.match_method, MatchMethod.CONTACT)
        self.assertIn("Contact N N (Acme) added", waiting.match_note)
        self.assertIsNone(other.client)
        self.assertEqual((report["checked"], report["matched"]), (1, 1))

    def test_rematch_for_client_uses_its_domains(self):
        waiting = self.stored(self.inbound(sender="boss@acme.com"))
        ClientEmailDomain.objects.create(client=self.acme, domain="acme.com")

        report = rematch_for_client.delay(self.acme.pk).result

        waiting.refresh_from_db()
        self.assertEqual((waiting.client, waiting.match_method), (self.acme, MatchMethod.DOMAIN))
        self.assertTrue(waiting.needs_review)
        self.assertEqual(report["matched"], 1)

    def test_missing_objects_do_nothing(self):
        self.assertEqual(rematch_for_contact.delay(999).result["checked"], 0)
        self.assertEqual(rematch_for_client.delay(999).result["checked"], 0)

    def test_queue_reports_inline_results(self):
        self.assertIn("0 email(s) checked", queue(rematch_messages, [], "test"))

    @override_settings(CELERY_TASK_ALWAYS_EAGER=False)
    def test_queue_describes_a_background_job(self):
        from unittest import mock

        pending = mock.Mock()
        pending.ready.return_value = False
        with mock.patch.object(rematch_messages, "delay", return_value=pending):
            text = queue(rematch_messages, [1], "test")

        self.assertIn("started in the background", text)


class ContactDeletionTests(MatchingFixtureMixin, TestCase):
    """Deleting a contact re-matches its emails and keeps the history."""

    def delete(self, obj):
        with self.captureOnCommitCallbacks(execute=True):
            obj.delete()

    def test_an_email_with_another_explanation_moves_there_with_a_note(self):
        # Anna's address is also a Globex contact's extra address
        ContactEmail.objects.create(contact=self.gus, email="anna@acme.com")
        message = self.stored(self.inbound())
        message.client, message.match_method = self.acme, MatchMethod.CONTACT
        message.save()
        message.contacts.add(self.anna)

        self.delete(self.anna)

        message.refresh_from_db()
        self.assertEqual((message.client, message.match_method), (self.globex, MatchMethod.CONTACT))
        self.assertIn("Contact Anna K (Acme) was deleted; Moved here from Acme", message.match_note)
        activity = Activity.objects.get()
        self.assertEqual(activity.client, self.globex)
        self.assertFalse(activity.needs_review)

    def test_an_email_with_no_other_explanation_stays_flagged(self):
        message = self.stored(self.inbound())
        match_message(message)

        self.delete(self.anna)

        message.refresh_from_db()
        self.assertEqual(message.client, self.acme)
        self.assertEqual(message.match_method, MatchMethod.STALE)
        self.assertTrue(message.needs_review)
        self.assertEqual(message.contacts.count(), 0)
        activity = Activity.objects.get()
        self.assertEqual(activity.client, self.acme)
        self.assertTrue(activity.needs_review)
        self.assertIn("Contact Anna K (Acme) was deleted", activity.note)

    def test_a_reference_matched_email_is_unaffected_in_substance(self):
        message = self.stored(self.inbound(subject=self.website.number))
        match_message(message)

        self.delete(self.anna)

        message.refresh_from_db()
        self.assertEqual((message.project, message.match_method), (self.website, MatchMethod.REFERENCE))
        self.assertFalse(message.needs_review)

    def test_manual_assignments_are_untouched(self):
        message = self.stored(self.inbound())
        message.assign(self.acme, self.website)

        self.delete(self.anna)

        message.refresh_from_db()
        self.assertEqual(message.match_method, MatchMethod.MANUAL)
        self.assertFalse(message.needs_review)

    def test_removing_an_extra_address_re_matches_its_emails(self):
        alias = ContactEmail.objects.create(contact=self.anna, email="anna.k@gmail.com")
        message = self.stored(self.inbound(sender="anna.k@gmail.com"))
        match_message(message)

        self.delete(alias)

        message.refresh_from_db()
        self.assertEqual(message.match_method, MatchMethod.STALE)
        self.assertIn("Address anna.k@gmail.com was removed", message.match_note)

    def test_deleting_the_client_still_deletes_everything(self):
        message = self.stored(self.inbound())
        match_message(message)

        self.delete(self.acme)

        self.assertFalse(EmailMessage.objects.exists())
        self.assertFalse(Activity.objects.exists())


class StaleReviewTests(MatchingFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_superuser("admin", "a@x.hu", "pw")
        self.client.force_login(self.user)
        self.message = self.stored(self.inbound())
        match_message(self.message)
        with self.captureOnCommitCallbacks(execute=True):
            self.anna.delete()
        self.message.refresh_from_db()
        self.url = reverse("admin:communications_emailmessage_changelist")

    def test_confirming_records_a_manual_decision(self):
        self.client.post(self.url, {"action": "mark_reviewed", "_selected_action": [self.message.pk]})

        self.message.refresh_from_db()
        self.assertEqual((self.message.client, self.message.match_method), (self.acme, MatchMethod.MANUAL))
        self.assertEqual(self.message.matched_by, self.user)
        self.assertFalse(Activity.objects.get().needs_review)

    def test_moving_it_by_hand(self):
        self.message.assign(self.globex, user=self.user)

        self.assertEqual(Activity.objects.get().client, self.globex)

    def test_deleting_it_removes_the_timeline_entry(self):
        self.message.delete()

        self.assertFalse(Activity.objects.exists())

    def test_review_queue_lists_stale_emails(self):
        response = self.client.get(self.url, {"match": "review"})

        self.assertEqual({m.pk for m in response.context["cl"].result_list}, {self.message.pk})


class ClientAdminRematchTests(MatchingFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_superuser("admin", "a@x.hu", "pw")
        self.client.force_login(self.user)

    def contact_data(self, **extra):
        return {
            "client": self.acme.pk, "first_name": "New", "last_name": "Person",
            "email": "new@acme.com",
            "email_addresses-TOTAL_FORMS": "0", "email_addresses-INITIAL_FORMS": "0",
            **extra,
        }

    def test_adding_a_contact_with_the_box_ticked_matches_its_emails(self):
        waiting = self.stored(self.inbound(sender="new@acme.com"))

        response = self.client.post(
            reverse("admin:clients_contact_add"), self.contact_data(run_matching="on"), follow=True
        )

        self.assertContains(response, "1 email(s) checked, 1 matched")
        waiting.refresh_from_db()
        self.assertEqual(waiting.client, self.acme)

    def test_adding_a_contact_with_the_box_clear_does_not(self):
        waiting = self.stored(self.inbound(sender="new@acme.com"))

        self.client.post(reverse("admin:clients_contact_add"), self.contact_data())

        waiting.refresh_from_db()
        self.assertIsNone(waiting.client)

    def test_the_box_is_offered_on_add_but_not_on_change(self):
        add = self.client.get(reverse("admin:clients_contact_add"))
        change = self.client.get(reverse("admin:clients_contact_change", args=[self.anna.pk]))

        self.assertContains(add, 'name="run_matching"')
        self.assertNotContains(change, 'name="run_matching"')
        self.assertContains(change, "Match waiting emails")  # the button instead

    def test_the_button_runs_the_contact_scope(self):
        ContactEmail.objects.create(contact=self.anna, email="anna.k@gmail.com")
        waiting = self.stored(self.inbound(sender="anna.k@gmail.com"))

        response = self.client.post(
            reverse("admin:clients_contact_rematch", args=[self.anna.pk]), follow=True
        )

        self.assertContains(response, "1 email(s) checked, 1 matched")
        waiting.refresh_from_db()
        self.assertEqual(waiting.client, self.acme)

    def test_the_button_requires_post(self):
        response = self.client.get(reverse("admin:clients_contact_rematch", args=[self.anna.pk]))

        self.assertEqual(response.status_code, 405)

    def test_the_client_button_covers_domains(self):
        ClientEmailDomain.objects.create(client=self.acme, domain="acme.com")
        waiting = self.stored(self.inbound(sender="boss@acme.com"))

        response = self.client.post(
            reverse("admin:clients_client_rematch", args=[self.acme.pk]), follow=True
        )

        self.assertContains(response, "1 matched")
        waiting.refresh_from_db()
        self.assertEqual(waiting.match_method, MatchMethod.DOMAIN)

    def test_adding_a_client_with_contacts_and_domains_inline(self):
        waiting = self.stored(self.inbound(sender="ceo@initech.com"))
        response = self.client.post(
            reverse("admin:clients_client_add"),
            {
                "name": "Initech", "status": "inquiry", "company_type": "smb", "country": "HU",
                "run_matching": "on",
                "contacts-TOTAL_FORMS": "1", "contacts-INITIAL_FORMS": "0",
                "contacts-0-first_name": "Bill", "contacts-0-last_name": "L",
                "contacts-0-email": "ceo@initech.com",
                "email_domains-TOTAL_FORMS": "0", "email_domains-INITIAL_FORMS": "0",
                "documents-TOTAL_FORMS": "0", "documents-INITIAL_FORMS": "0",
                "activities-TOTAL_FORMS": "0", "activities-INITIAL_FORMS": "0",
            },
            follow=True,
        )

        self.assertContains(response, "1 email(s) checked, 1 matched")
        waiting.refresh_from_db()
        self.assertEqual(waiting.client.name, "Initech")
