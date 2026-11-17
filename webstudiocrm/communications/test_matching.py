from io import StringIO

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from clients.models import Client, ClientEmailDomain, Contact, ContactEmail
from core.models import EmailIgnoreRule, StudioProfile
from documents.models import Document, DocumentType
from projects.models import Project
from quotes.models import Quote

from .choices import Direction, MatchMethod
from .models import Activity, EmailMessage, EmailThread
from .services.matching import MatchInput, apply_match, find_references, match, match_message

YEAR = timezone.localdate().year


class MatchingFixtureMixin:
    """Two clients, their contacts and projects, and the studio's own address."""

    def setUp(self):
        profile = StudioProfile.get_solo()
        profile.email = "hello@studio.hu"
        profile.save()

        self.acme = Client.objects.create(name="Acme")
        self.globex = Client.objects.create(name="Globex")
        self.anna = Contact.objects.create(
            client=self.acme, first_name="Anna", last_name="K", email="anna@acme.com"
        )
        self.gus = Contact.objects.create(
            client=self.globex, first_name="Gus", last_name="G", email="gus@globex.com"
        )
        self.website = Project.objects.create(
            client=self.acme, name="Website", project_type="website"
        )
        self.shop = Project.objects.create(
            client=self.globex, name="Shop", project_type="webshop"
        )

    def inbound(self, sender="anna@acme.com", subject="Hello", body="", **fields):
        return MatchInput(
            direction=Direction.INBOUND, from_address=sender, to=("hello@studio.hu",),
            subject=subject, body_text=body, **fields,
        )

    def outbound(self, *recipients, subject="Hello", body="", **fields):
        return MatchInput(
            direction=Direction.OUTBOUND, from_address="hello@studio.hu", to=recipients,
            subject=subject, body_text=body, **fields,
        )

    def stored(self, data=None, message_id=None, **fields):
        """Save a MatchInput as an EmailMessage, unmatched."""
        data = data or self.inbound()
        thread = EmailThread.objects.create(subject=data.subject)
        return EmailMessage.objects.create(
            thread=thread,
            message_id=message_id or f"<{EmailMessage.objects.count() + 1}@test>",
            direction=data.direction,
            from_address=data.from_address,
            to_addresses=[{"email": a} for a in data.to],
            cc_addresses=[{"email": a} for a in data.cc],
            subject=data.subject,
            body_text=data.body_text,
            in_reply_to=data.in_reply_to,
            references=list(data.references),
            sent_at=timezone.now(),
            **fields,
        )


class FindReferencesTests(MatchingFixtureMixin, TestCase):
    def test_subject_references_in_any_case(self):
        self.assertEqual(
            find_references(f"Re: [prj-{YEAR}-0001] copy and Q-{YEAR}-0002-V2", ""),
            [f"PRJ-{YEAR}-0001", f"Q-{YEAR}-0002-v2"],
        )

    def test_body_is_read_only_when_the_subject_has_none(self):
        body = f"About PRJ-{YEAR}-0002."
        self.assertEqual(find_references("Question", body), [f"PRJ-{YEAR}-0002"])
        self.assertEqual(
            find_references(f"PRJ-{YEAR}-0001", body), [f"PRJ-{YEAR}-0001"]
        )

    def test_quoted_reply_lines_are_ignored(self):
        body = f"Sounds good.\n\n> On Monday you wrote:\n> about PRJ-{YEAR}-0009"

        self.assertEqual(find_references("Re: question", body), [])

    def test_lookalikes_are_not_references(self):
        for text in [f"XPRJ-{YEAR}-0001", f"PRJ-{YEAR}-0001X", f"PRJ-{YEAR}-01", "PRJ-26-0001"]:
            with self.subTest(text):
                self.assertEqual(find_references(text, ""), [])

    def test_document_type_prefixes_count_including_dashed_ones(self):
        DocumentType.objects.create(name="Agency Agreement", prefix="DEV-AGR")

        self.assertEqual(
            find_references(f"NDA-{YEAR}-0001 and DEV-AGR-{YEAR}-0002", ""),
            [f"NDA-{YEAR}-0001", f"DEV-AGR-{YEAR}-0002"],
        )

    def test_repeats_are_listed_once(self):
        ref = self.website.number
        self.assertEqual(find_references(f"{ref} / {ref.lower()}", ""), [ref])


class ReferenceRuleTests(MatchingFixtureMixin, TestCase):
    def test_project_number(self):
        result = match(self.inbound(subject=f"[{self.website.number}] Homepage"))

        self.assertEqual(result.method, MatchMethod.REFERENCE)
        self.assertEqual((result.client, result.project), (self.acme, self.website))
        self.assertEqual(result.contacts, [self.anna])
        self.assertFalse(result.needs_review)

    def test_quote_number_and_its_revision(self):
        Quote.objects.create(project=self.website, number=f"Q-{YEAR}-0001")
        Quote.objects.create(project=self.website, number=f"Q-{YEAR}-0001-v2")

        for subject in [f"Quote Q-{YEAR}-0001", f"Quote q-{YEAR}-0001-v2"]:
            with self.subTest(subject):
                result = match(self.inbound(subject=subject))
                self.assertEqual(result.project, self.website)

    def test_document_number_with_and_without_a_project(self):
        nda = DocumentType.objects.get(prefix="NDA")
        Document.objects.create(document_type=nda, client=self.acme, title="NDA",
                                number=f"NDA-{YEAR}-0001")
        Document.objects.create(document_type=nda, client=self.acme, title="NDA 2",
                                project=self.website, number=f"NDA-{YEAR}-0002")
        Project.objects.create(client=self.acme, name="App", project_type="web_app")

        loose = match(self.inbound(subject=f"Signed NDA-{YEAR}-0001"))
        tied = match(self.inbound(subject=f"Signed NDA-{YEAR}-0002"))

        # Two open projects: a client-only reference stays client-only
        self.assertEqual((loose.method, loose.client, loose.project),
                         (MatchMethod.REFERENCE, self.acme, None))
        self.assertEqual(tied.project, self.website)

    def test_an_unknown_number_falls_through_to_the_next_rule(self):
        result = match(self.inbound(subject=f"PRJ-{YEAR}-9999"))

        self.assertEqual(result.method, MatchMethod.CONTACT)

    def test_references_to_different_projects_are_flagged(self):
        result = match(self.inbound(subject=f"{self.website.number} vs {self.shop.number}"))

        self.assertEqual(result.project, self.website)  # the first one
        self.assertTrue(result.needs_review)
        self.assertIn("Several references", result.note)

    def test_a_reference_against_the_people_on_the_email_is_flagged(self):
        result = match(self.inbound(sender="gus@globex.com", subject=self.website.number))

        self.assertEqual(result.client, self.acme)
        self.assertTrue(result.needs_review)
        self.assertIn("no contact on the email belongs to it", result.note)
        # Globex's contact is not recorded on an Acme email
        self.assertEqual(result.contacts, [])

    def test_a_reference_from_an_unknown_sender_is_trusted(self):
        result = match(self.inbound(sender="new.person@gmail.com", subject=self.website.number))

        self.assertEqual(result.project, self.website)
        self.assertFalse(result.needs_review)


class ThreadRuleTests(MatchingFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.parent = self.stored(
            message_id="<parent@studio.hu>", project=self.website,
            match_method=MatchMethod.REFERENCE,
        )

    def test_a_reply_inherits_client_and_project(self):
        result = match(self.inbound(sender="stranger@gmail.com", subject="Re: Hello",
                                    in_reply_to="<parent@studio.hu>"))

        self.assertEqual(result.method, MatchMethod.THREAD)
        self.assertEqual((result.client, result.project), (self.acme, self.website))

    def test_the_nearest_matched_ancestor_wins(self):
        self.stored(message_id="<middle@acme.com>", client=self.globex,
                    match_method=MatchMethod.CONTACT)
        self.stored(message_id="<unmatched@x.com>")

        result = match(self.inbound(
            sender="stranger@gmail.com",
            in_reply_to="<unmatched@x.com>",
            references=("<parent@studio.hu>", "<middle@acme.com>", "<unmatched@x.com>"),
        ))

        self.assertEqual(result.client, self.globex)

    def test_a_reply_to_an_unreviewed_email_is_flagged_too(self):
        EmailMessage.objects.filter(pk=self.parent.pk).update(needs_review=True)

        result = match(self.inbound(in_reply_to="<parent@studio.hu>"))

        self.assertTrue(result.needs_review)

    def test_a_reference_beats_the_thread(self):
        result = match(self.inbound(sender="gus@globex.com", subject=f"Re: {self.shop.number}",
                                    in_reply_to="<parent@studio.hu>"))

        self.assertEqual((result.method, result.project), (MatchMethod.REFERENCE, self.shop))


class ContactRuleTests(MatchingFixtureMixin, TestCase):
    def test_received_from_a_contact(self):
        result = match(self.inbound())

        self.assertEqual((result.method, result.client), (MatchMethod.CONTACT, self.acme))
        self.assertEqual(result.contacts, [self.anna])
        self.assertFalse(result.needs_review)

    def test_sent_to_a_contact(self):
        result = match(self.outbound("Anna@ACME.com"))

        self.assertEqual(result.client, self.acme)

    def test_by_an_extra_address(self):
        ContactEmail.objects.create(contact=self.anna, email="anna.k@gmail.com")

        self.assertEqual(match(self.inbound(sender="anna.k@gmail.com")).client, self.acme)

    def test_studio_and_ignored_addresses_are_never_the_client(self):
        # A Globex contact who is also on the studio's ignore list
        EmailIgnoreRule.objects.create(pattern="gus@globex.com")

        result = match(self.outbound("gus@globex.com"))

        self.assertEqual(result.method, MatchMethod.UNMATCHED)

    def test_a_contact_shared_by_two_clients_is_flagged(self):
        Contact.objects.create(client=self.globex, first_name="Anna", last_name="K",
                               email="anna@acme.com")

        result = match(self.inbound())

        self.assertTrue(result.needs_review)
        self.assertIn("Acme, Globex", result.note)

    def test_most_parties_decide_and_the_sender_breaks_ties(self):
        Contact.objects.create(client=self.globex, first_name="B", last_name="B",
                               email="boss@globex.com")
        two_globex = self.inbound(cc=("gus@globex.com", "boss@globex.com"))
        tie = self.inbound(cc=("gus@globex.com",))

        self.assertEqual(match(two_globex).client, self.globex)
        self.assertEqual(match(tie).client, self.acme)
        self.assertTrue(match(tie).needs_review)


class OpenProjectTests(MatchingFixtureMixin, TestCase):
    def test_the_only_open_project_is_filled_in(self):
        Project.objects.create(client=self.acme, name="Old", project_type="website",
                               status="completed")

        result = match(self.inbound())

        self.assertEqual(result.project, self.website)
        self.assertIn(f"Only open project {self.website.number}", result.note)

    def test_with_several_open_projects_the_contact_led_one_is_chosen(self):
        Project.objects.create(client=self.acme, name="App", project_type="web_app")
        self.website.primary_contact = self.anna
        self.website.save()

        self.assertEqual(match(self.inbound()).project, self.website)

    def test_otherwise_the_project_is_left_empty(self):
        Project.objects.create(client=self.acme, name="App", project_type="web_app")

        result = match(self.inbound())

        self.assertEqual((result.client, result.project), (self.acme, None))
        self.assertFalse(result.needs_review)


class DomainRuleTests(MatchingFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        ClientEmailDomain.objects.create(client=self.acme, domain="acme.com")

    def test_an_unknown_colleague_is_matched_and_flagged(self):
        result = match(self.inbound(sender="boss@eu.acme.com"))

        self.assertEqual((result.method, result.client), (MatchMethod.DOMAIN, self.acme))
        self.assertTrue(result.needs_review)
        self.assertIn("Unknown sender boss@eu.acme.com", result.note)
        self.assertEqual(result.contacts, [])

    def test_a_known_contact_beats_the_domain(self):
        result = match(self.inbound(sender="gus@globex.com", cc=("boss@acme.com",)))

        self.assertEqual((result.method, result.client), (MatchMethod.CONTACT, self.globex))

    def test_nothing_found_is_unmatched(self):
        result = match(self.inbound(sender="someone@gmail.com"))

        self.assertFalse(result.is_matched)
        self.assertEqual(result.method, MatchMethod.UNMATCHED)
        self.assertIn("No reference", result.note)


class ApplyMatchTests(MatchingFixtureMixin, TestCase):
    def test_stores_the_result_and_puts_it_on_the_timeline(self):
        message = self.stored(self.inbound(subject=self.website.number))

        match_message(message)

        message.refresh_from_db()
        self.assertEqual((message.client, message.project), (self.acme, self.website))
        self.assertEqual(message.match_method, MatchMethod.REFERENCE)
        self.assertEqual(list(message.contacts.all()), [self.anna])
        self.assertEqual(message.match_note, f"Reference {self.website.number}")
        self.assertEqual(Activity.objects.get().project, self.website)

    def test_domain_matches_are_always_flagged(self):
        ClientEmailDomain.objects.create(client=self.acme, domain="acme.com")
        message = self.stored(self.inbound(sender="boss@acme.com"))

        match_message(message)

        self.assertTrue(message.needs_review)

    def test_manual_assignments_are_left_alone(self):
        message = self.stored(self.inbound(subject=self.website.number))
        message.assign(self.globex, self.shop)

        result = match(MatchInput.from_message(message))
        changed = apply_match(message, result)

        message.refresh_from_db()
        self.assertFalse(changed)
        self.assertEqual((message.client, message.match_method), (self.globex, MatchMethod.MANUAL))

    def test_a_message_is_not_its_own_ancestor(self):
        message = self.stored(self.inbound(sender="x@gmail.com"), message_id="<self@x>",
                              client=self.acme, match_method=MatchMethod.CONTACT)
        message.in_reply_to = "<self@x>"

        self.assertEqual(match(MatchInput.from_message(message)).method, MatchMethod.UNMATCHED)

    def test_rematching_with_no_result_keeps_the_history_as_stale(self):
        message = self.stored(self.inbound())
        match_message(message)
        Contact.objects.filter(pk=self.anna.pk).delete()  # see test_rematch for the signal

        match_message(message, reason="Contact Anna K was deleted")

        message.refresh_from_db()
        self.assertEqual(message.client, self.acme)
        self.assertEqual(message.match_method, MatchMethod.STALE)
        self.assertTrue(message.needs_review)
        self.assertIn("Contact Anna K was deleted; Nothing matches any more", message.match_note)
        activity = Activity.objects.get()
        self.assertTrue(activity.needs_review)
        self.assertEqual(activity.note, message.match_note)


class MatchEmailsCommandTests(MatchingFixtureMixin, TestCase):
    def run_command(self, *args):
        out = StringIO()
        call_command("match_emails", *args, stdout=out)
        return out.getvalue()

    def test_matches_what_still_needs_it_and_reports(self):
        waiting = self.stored(self.inbound(sender="new@acme.com"))
        ClientEmailDomain.objects.create(client=self.acme, domain="acme.com")

        output = self.run_command()

        waiting.refresh_from_db()
        self.assertEqual(waiting.client, self.acme)
        self.assertIn("1 email(s) checked, 1 changed", output)

    def test_dry_run_saves_nothing(self):
        waiting = self.stored(self.inbound())

        output = self.run_command("--dry-run")

        waiting.refresh_from_db()
        self.assertIsNone(waiting.client)
        self.assertIn("1 would change", output)

    def test_confirmed_matches_only_with_all_and_manual_never(self):
        confirmed = self.stored(self.inbound(), client=self.globex,
                                match_method=MatchMethod.CONTACT)
        manual = self.stored(self.inbound())
        manual.assign(self.globex)

        self.run_command()
        confirmed.refresh_from_db()
        self.assertEqual(confirmed.client, self.globex)

        self.run_command("--all")
        confirmed.refresh_from_db()
        manual.refresh_from_db()
        self.assertEqual(confirmed.client, self.acme)
        self.assertEqual(manual.client, self.globex)


class RematchAdminActionTests(MatchingFixtureMixin, TestCase):
    def test_rematches_selected_and_skips_manual(self):
        user = User.objects.create_superuser("admin", "a@x.hu", "pw")
        self.client.force_login(user)
        waiting = self.stored(self.inbound())
        manual = self.stored(self.inbound())
        manual.assign(self.globex)

        response = self.client.post(
            reverse("admin:communications_emailmessage_changelist"),
            {"action": "rematch", "_selected_action": [waiting.pk, manual.pk]},
            follow=True,
        )

        waiting.refresh_from_db()
        self.assertEqual(waiting.client, self.acme)
        self.assertContains(response, "1 email(s) checked, 1 matched. 1 assigned by hand were left alone.")
