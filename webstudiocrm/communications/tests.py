from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import IntegrityError
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from clients.models import Client, Contact
from cloudstorage.testing import temp_bucket
from documents.services.workflow import DocumentService
from documents.tests import WorkflowFixtureMixin
from projects.models import Project
from quotes.services.workflow import QuoteService
from quotes.tests import QuoteFixtureMixin

from .choices import ActivityKind, Direction, MatchMethod
from .models import Activity, EmailAttachment, EmailMessage, EmailThread, normalize_subject
from .services.activities import log_activity, log_note

BUCKET = temp_bucket()
NOW = timezone.now()


class SubjectNormalisationTests(SimpleTestCase):
    def test_reply_and_forward_prefixes_are_removed(self):
        cases = {
            "Re: Homepage copy": "Homepage copy",
            "RE: FW: Fwd: Homepage copy": "Homepage copy",
            "Re[2]: Homepage copy": "Homepage copy",
            "VÁ: Homepage copy": "Homepage copy",
            "Vá: TOV: Homepage copy": "Homepage copy",
            "AW: Homepage copy": "Homepage copy",
        }
        for subject, expected in cases.items():
            with self.subTest(subject):
                self.assertEqual(normalize_subject(subject), expected)

    def test_bracket_tags_are_removed(self):
        self.assertEqual(
            normalize_subject("Re: [PRJ-2026-0001] [EXTERNAL] Homepage copy"), "Homepage copy"
        )

    def test_inner_text_is_kept_and_whitespace_collapsed(self):
        self.assertEqual(normalize_subject("  Homepage   copy: review  "), "Homepage copy: review")
        self.assertEqual(normalize_subject("Rebuild the menu"), "Rebuild the menu")
        self.assertEqual(normalize_subject(None), "")


class EmailFixtureMixin:
    def setUp(self):
        self.acme = Client.objects.create(name="Acme")
        self.globex = Client.objects.create(name="Globex")
        self.anna = Contact.objects.create(
            client=self.acme, first_name="Anna", last_name="K", email="anna@acme.com"
        )
        self.website = Project.objects.create(
            client=self.acme, name="Website", project_type="website"
        )
        self.thread = EmailThread.objects.create(subject="Homepage copy")

    def make_message(self, **fields):
        defaults = {
            "thread": self.thread,
            "message_id": f"<{EmailMessage.objects.count() + 1}@acme.com>",
            "direction": Direction.INBOUND,
            "from_name": "Anna K",
            "from_address": "anna@acme.com",
            "to_addresses": [{"name": "Studio", "email": "hello@studio.hu"}],
            "subject": "Re: Homepage copy",
            "body_text": "Looks good,\n\nplease  go ahead.",
            "sent_at": NOW,
        }
        defaults.update(fields)
        return EmailMessage.objects.create(**defaults)


class EmailMessageTests(EmailFixtureMixin, TestCase):
    def test_message_ids_are_unique(self):
        self.make_message(message_id="<a@acme.com>")

        with self.assertRaises(IntegrityError):
            self.make_message(message_id="<a@acme.com>")

    def test_thread_bounds_follow_the_messages(self):
        self.make_message(sent_at=NOW - timedelta(days=2))
        self.make_message(sent_at=NOW)

        self.thread.refresh_from_db()
        self.assertEqual(self.thread.first_message_at, NOW - timedelta(days=2))
        self.assertEqual(self.thread.last_message_at, NOW)

    def test_participants_are_lowercased_sender_first_without_repeats(self):
        message = self.make_message(
            from_address="Anna@Acme.com",
            to_addresses=[{"email": "hello@studio.hu"}, {"email": "ANNA@acme.com"}],
            cc_addresses=[{"name": "Boss", "email": "boss@acme.com"}, {"email": ""}],
        )

        self.assertEqual(message.participants, ["anna@acme.com", "hello@studio.hu", "boss@acme.com"])

    def test_snippet_collapses_whitespace_and_shortens(self):
        message = self.make_message(body_text="word " * 100)

        self.assertEqual(self.make_message().snippet, "Looks good, please go ahead.")
        self.assertEqual(len(message.snippet), 160)
        self.assertTrue(message.snippet.endswith("..."))

    def test_a_project_implies_its_client(self):
        message = self.make_message(project=self.website, match_method=MatchMethod.REFERENCE)

        self.assertEqual(message.client, self.acme)
        self.assertTrue(message.is_matched)

    def test_project_of_another_client_is_rejected(self):
        message = self.make_message(client=self.globex, match_method=MatchMethod.CONTACT)
        message.project = self.website

        with self.assertRaises(ValidationError) as ctx:
            message.full_clean()

        self.assertIn("project", ctx.exception.message_dict)

    def test_match_method_and_client_must_agree(self):
        with_client_no_method = self.make_message(client=self.acme)
        with_method_no_client = self.make_message(match_method=MatchMethod.CONTACT)

        with self.assertRaises(ValidationError) as ctx:
            with_client_no_method.full_clean()
        self.assertIn("match_method", ctx.exception.message_dict)

        with self.assertRaises(ValidationError) as ctx:
            with_method_no_client.full_clean()
        self.assertIn("client", ctx.exception.message_dict)

    def test_an_unmatched_message_is_never_flagged_for_review(self):
        message = self.make_message(needs_review=True)

        self.assertFalse(message.needs_review)

    def test_manual_assignment_is_final(self):
        user = User.objects.create_user("gabi")
        message = self.make_message(client=self.globex, match_method=MatchMethod.DOMAIN,
                                    needs_review=True)

        message.assign(self.acme, self.website, user=user)

        message.refresh_from_db()
        self.assertEqual((message.client, message.project), (self.acme, self.website))
        self.assertEqual(message.match_method, MatchMethod.MANUAL)
        self.assertEqual(message.matched_by, user)
        self.assertFalse(message.needs_review)


class EmailActivityTests(EmailFixtureMixin, TestCase):
    def test_an_unmatched_message_has_no_timeline_entry(self):
        self.make_message()

        self.assertFalse(Activity.objects.exists())

    def test_a_matched_message_appears_once_on_the_timeline(self):
        message = self.make_message(project=self.website, match_method=MatchMethod.REFERENCE)
        message.save()  # saving again must not add a second entry

        activity = Activity.objects.get()
        self.assertEqual(activity.kind, ActivityKind.EMAIL_IN)
        self.assertEqual((activity.client, activity.project), (self.acme, self.website))
        self.assertEqual(activity.summary, "Re: Homepage copy")
        self.assertEqual(activity.details, "Looks good, please go ahead.")
        self.assertEqual(activity.happened_at, NOW)
        self.assertEqual(activity.subject, message)

    def test_sent_mail_is_an_outgoing_entry(self):
        self.make_message(direction=Direction.OUTBOUND, client=self.acme,
                          match_method=MatchMethod.CONTACT)

        self.assertEqual(Activity.objects.get().kind, ActivityKind.EMAIL_OUT)

    def test_reassigning_moves_the_entry_and_unmatching_removes_it(self):
        message = self.make_message(client=self.globex, match_method=MatchMethod.DOMAIN)

        message.assign(self.acme, self.website)
        self.assertEqual(Activity.objects.get().client, self.acme)
        self.assertEqual(self.acme.activities.count(), 1)
        self.assertEqual(self.globex.activities.count(), 0)

        message.client = message.project = None
        message.match_method = MatchMethod.UNMATCHED
        message.save()
        self.assertFalse(Activity.objects.exists())

    def test_deleting_the_message_removes_its_entry(self):
        message = self.make_message(client=self.acme, match_method=MatchMethod.CONTACT)

        message.delete()

        self.assertFalse(Activity.objects.exists())

    def test_deleting_the_client_removes_its_mail_but_a_project_does_not(self):
        self.make_message(project=self.website, match_method=MatchMethod.REFERENCE)

        self.website.delete()
        message = EmailMessage.objects.get()
        self.assertEqual((message.client, message.project), (self.acme, None))
        self.assertEqual(Activity.objects.get().project, None)

        self.acme.delete()
        self.assertFalse(EmailMessage.objects.exists())
        self.assertFalse(Activity.objects.exists())

    def test_latest_matched_message_stands_for_the_thread(self):
        self.make_message(sent_at=NOW - timedelta(days=1), project=self.website,
                          match_method=MatchMethod.REFERENCE)
        self.make_message(sent_at=NOW)  # newer, but unmatched

        self.assertEqual(self.thread.latest_matched_message.project, self.website)
        self.assertIsNone(EmailThread.objects.create().latest_matched_message)


@override_settings(CLOUD_STORAGE=BUCKET)
class EmailAttachmentTests(EmailFixtureMixin, TestCase):
    def test_checksum_and_size_are_computed_from_the_stored_file(self):
        import hashlib

        message = self.make_message()
        attachment = EmailAttachment(message=message, filename="brief.pdf",
                                     content_type="application/pdf")
        attachment.file.save("brief.pdf", ContentFile(b"%PDF-1.7 brief"), save=False)
        attachment.save()

        self.assertEqual(attachment.size, len(b"%PDF-1.7 brief"))
        self.assertEqual(attachment.checksum_sha256, hashlib.sha256(b"%PDF-1.7 brief").hexdigest())

    def test_raw_size_is_recorded(self):
        message = self.make_message()
        message.raw_file.save("m.eml", ContentFile(b"Subject: x\r\n\r\nbody"), save=False)
        message.save()

        self.assertEqual(message.raw_size, len(b"Subject: x\r\n\r\nbody"))


class ActivityServiceTests(EmailFixtureMixin, TestCase):
    def test_a_project_implies_its_client(self):
        activity = log_activity(ActivityKind.QUOTE_SENT, project=self.website, summary="Quote sent")

        self.assertEqual(activity.client, self.acme)
        self.assertFalse(activity.is_editable)

    def test_client_or_project_is_required(self):
        with self.assertRaises(ValueError):
            log_activity(ActivityKind.NOTE, summary="Orphan")

    def test_project_of_another_client_is_rejected(self):
        with self.assertRaises(ValidationError):
            log_activity(ActivityKind.NOTE, client=self.globex, project=self.website, summary="x")

    def test_entries_outlive_the_record_they_point_at(self):
        other = Project.objects.create(client=self.acme, name="App", project_type="web_app")
        activity = log_activity(ActivityKind.NOTE, client=self.acme, subject=other, summary="Call")

        other.delete()

        activity.refresh_from_db()
        self.assertIsNone(activity.subject)
        self.assertEqual(activity.summary, "Call")

    def test_notes_use_their_first_line_as_summary(self):
        user = User.objects.create_user("gabi")
        note = log_note(self.acme, "Called Anna.\nShe wants the launch in May.", actor=user)

        self.assertEqual(note.kind, ActivityKind.NOTE)
        self.assertEqual(note.summary, "Called Anna.")
        self.assertEqual(note.details, "Called Anna.\nShe wants the launch in May.")
        self.assertEqual(note.created_by, user)
        self.assertTrue(note.is_editable)


@override_settings(CLOUD_STORAGE=BUCKET)
class QuoteWorkflowActivityTests(QuoteFixtureMixin, TestCase):
    """The quote workflow writes to the timeline."""

    def send(self, quote):
        with mock.patch("quotes.services.workflow.QuotePDFRenderer.save"):
            return QuoteService.send(quote)

    def test_sending_accepting_and_rejecting_are_logged(self):
        sent = self.send(self.make_quote())
        QuoteService.accept(sent)
        rejected = self.send(self.make_quote())
        QuoteService.reject(rejected, reason="Too expensive")

        kinds = list(Activity.objects.order_by("pk").values_list("kind", flat=True))
        self.assertEqual(kinds, [
            ActivityKind.QUOTE_SENT, ActivityKind.QUOTE_ACCEPTED,
            ActivityKind.QUOTE_SENT, ActivityKind.QUOTE_REJECTED,
        ])
        first = Activity.objects.order_by("pk").first()
        self.assertEqual((first.client, first.project), (self.client_obj, self.project))
        self.assertEqual(first.subject, sent)
        self.assertIn(sent.number, first.summary)
        self.assertEqual(Activity.objects.order_by("pk").last().details, "Too expensive")

    def test_a_failed_send_logs_nothing(self):
        with mock.patch(
            "quotes.services.workflow.QuotePDFRenderer.render", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                QuoteService.send(self.make_quote())

        self.assertFalse(Activity.objects.exists())


@override_settings(CLOUD_STORAGE=BUCKET)
class DocumentWorkflowActivityTests(WorkflowFixtureMixin, TestCase):
    """The document workflow writes to the timeline."""

    def test_sending_signing_and_terminating_are_logged(self):
        document = self.ready_draft(project=self.project)
        DocumentService.send_for_signature(document)
        self.sign_all(document)
        DocumentService.terminate(document, reason="Project cancelled")

        kinds = list(Activity.objects.order_by("pk").values_list("kind", flat=True))
        self.assertEqual(kinds, [
            ActivityKind.DOCUMENT_SENT, ActivityKind.DOCUMENT_SIGNED,
            ActivityKind.DOCUMENT_TERMINATED,
        ])
        sent = Activity.objects.order_by("pk").first()
        self.assertEqual((sent.client, sent.project), (self.client_record, self.project))
        self.assertEqual(sent.subject, document)
        self.assertIn(document.number, sent.summary)
        self.assertEqual(Activity.objects.order_by("pk").last().details, "Project cancelled")


class AdminTests(EmailFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_superuser("admin", "a@x.hu", "pw")
        self.client.force_login(self.user)

    def test_pages_render(self):
        message = self.make_message(client=self.acme, match_method=MatchMethod.CONTACT,
                                    needs_review=True)
        log_note(self.acme, "Called Anna.")
        urls = [
            reverse("admin:communications_emailmessage_changelist"),
            reverse("admin:communications_emailmessage_changelist") + "?match=review",
            reverse("admin:communications_emailmessage_change", args=[message.pk]),
            reverse("admin:communications_emailthread_changelist"),
            reverse("admin:communications_emailthread_change", args=[self.thread.pk]),
            reverse("admin:communications_activity_changelist"),
            reverse("admin:communications_activity_add"),
            reverse("admin:clients_client_change", args=[self.acme.pk]),
            reverse("admin:projects_project_change", args=[self.website.pk]),
        ]
        for url in urls:
            with self.subTest(url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_emails_cannot_be_added_by_hand(self):
        response = self.client.get(reverse("admin:communications_emailmessage_add"))

        self.assertEqual(response.status_code, 403)

    def test_review_queue_filter(self):
        unmatched = self.make_message()
        guessed = self.make_message(client=self.acme, match_method=MatchMethod.DOMAIN,
                                    needs_review=True)
        confirmed = self.make_message(client=self.acme, match_method=MatchMethod.CONTACT)
        url = reverse("admin:communications_emailmessage_changelist")

        def ids(state):
            response = self.client.get(url, {"match": state})
            return {m.pk for m in response.context["cl"].result_list}

        self.assertEqual(ids("unmatched"), {unmatched.pk})
        self.assertEqual(ids("review"), {guessed.pk})
        self.assertEqual(ids("matched"), {confirmed.pk})

    def test_changing_the_client_in_the_admin_is_a_manual_assignment(self):
        message = self.make_message(client=self.globex, match_method=MatchMethod.DOMAIN,
                                    needs_review=True)

        response = self.client.post(
            reverse("admin:communications_emailmessage_change", args=[message.pk]),
            {
                "client": self.acme.pk,
                "project": self.website.pk,
                "needs_review": "on",
                "attachments-TOTAL_FORMS": "0",
                "attachments-INITIAL_FORMS": "0",
            },
        )

        self.assertEqual(response.status_code, 302)
        message.refresh_from_db()
        self.assertEqual((message.client, message.project), (self.acme, self.website))
        self.assertEqual(message.match_method, MatchMethod.MANUAL)
        self.assertEqual(message.matched_by, self.user)
        self.assertFalse(message.needs_review)
        self.assertEqual(Activity.objects.get().project, self.website)

    def test_a_note_can_be_added_and_a_mirrored_entry_cannot_be_deleted(self):
        response = self.client.post(
            reverse("admin:communications_activity_add"),
            {
                "kind": ActivityKind.NOTE,
                "client": self.acme.pk,
                "happened_at_0": "2026-11-15",
                "happened_at_1": "10:00:00",
                "summary": "Kick-off call",
                "details": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        note = Activity.objects.get()
        self.assertEqual(note.created_by, self.user)

        mirrored = log_activity(ActivityKind.QUOTE_SENT, project=self.website, summary="Q sent")
        response = self.client.get(
            reverse("admin:communications_activity_delete", args=[mirrored.pk])
        )
        self.assertEqual(response.status_code, 403)
