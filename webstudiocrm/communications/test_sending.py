"""Writing and sending email from the CRM (Step 6)."""

import smtplib
import tempfile
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import User
from django.core import mail
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from cloudstorage.testing import temp_bucket
from core.models import StudioProfile
from documents.models import Document, DocumentFile, DocumentSigner, DocumentType
from quotes.models import Quote

from .choices import ActivityKind, Direction, MatchMethod, OutgoingStatus
from .mail_backends import OutboxFolderBackend
from .models import Activity, EmailMessage, OutgoingAttachment, OutgoingEmail
from .services import outgoing
from .services.ingest import ingest
from .services.outgoing import (
    SendError,
    deliver,
    display_address,
    draft_for,
    parse_addresses,
    problems,
    queue_for_sending,
    subject_with_reference,
)
from .test_matching import MatchingFixtureMixin

BUCKET = temp_bucket()


class AddressTests(SimpleTestCase):
    def test_parse_addresses(self):
        self.assertEqual(
            parse_addresses("Anna K <Anna@Acme.com>, boss@acme.com; anna@acme.com"),
            [("Anna K", "anna@acme.com"), ("", "boss@acme.com")],
        )
        self.assertEqual(parse_addresses(""), [])
        self.assertEqual(parse_addresses("a@x.hu,, ;b@y.hu"), [("", "a@x.hu"), ("", "b@y.hu")])
        self.assertEqual(parse_addresses('"Kovács; Anna" <a@x.hu>; b@y.hu'),
                         [("Kovács; Anna", "a@x.hu"), ("", "b@y.hu")])
        with self.assertRaisesMessage(ValidationError, "Not an email address: nope, x@"):
            parse_addresses("ok@acme.com, nope, x@")

    def test_display_address_is_readable_and_round_trips(self):
        self.assertEqual(display_address("Anna Kovács", "a@x.hu"), "Anna Kovács <a@x.hu>")
        self.assertEqual(display_address("", "a@x.hu"), "a@x.hu")
        quoted = display_address("Kovács, Anna", "a@x.hu")
        self.assertEqual(quoted, '"Kovács, Anna" <a@x.hu>')
        self.assertEqual(parse_addresses(quoted), [("Kovács, Anna", "a@x.hu")])


class SendingFixtureMixin(MatchingFixtureMixin):
    def setUp(self):
        super().setUp()
        profile = StudioProfile.get_solo()
        profile.company_name = "WebStudio Kft."
        profile.email_signature = "Gábor\nWebStudio Kft."
        profile.save()
        self.user = User.objects.create_user("gabi")

    def ready(self, **fields):
        email = draft_for(self.user, project=self.website)
        email.subject = fields.pop("subject", "Launch date")
        email.body_text = fields.pop("body_text", "Hi Anna," + email.body_text)
        for key, value in fields.items():
            setattr(email, key, value)
        email.save()
        return email

    def send(self, email):
        with self.captureOnCommitCallbacks(execute=True):
            queue_for_sending(email, self.user)
        email.refresh_from_db()
        return email


class SubjectTests(SendingFixtureMixin, TestCase):
    def test_project_number_is_added_once(self):
        number = self.website.number
        self.assertEqual(subject_with_reference("Launch", self.website), f"[{number}] Launch")
        self.assertEqual(subject_with_reference(f"Re: [{number}] Launch", self.website),
                         f"Re: [{number}] Launch")
        self.assertEqual(subject_with_reference(f"about {number.lower()}", self.website),
                         f"about {number.lower()}")
        self.assertEqual(subject_with_reference("Launch", None), "Launch")


@override_settings(CLOUD_STORAGE=BUCKET)
class DraftTests(SendingFixtureMixin, TestCase):
    def test_from_a_project_client_or_contact(self):
        from_project = draft_for(self.user, project=self.website)
        from_client = draft_for(self.user, client=self.globex)
        from_contact = draft_for(self.user, contact=self.anna)

        self.assertEqual((from_project.client, from_project.project), (self.acme, self.website))
        self.assertEqual(from_project.to, "Anna K <anna@acme.com>")
        self.assertEqual(from_project.body_text, "\n\n-- \nGábor\nWebStudio Kft.")
        self.assertEqual(from_project.created_by, self.user)
        self.assertEqual((from_client.client, from_client.to), (self.globex, "Gus G <gus@globex.com>"))
        self.assertEqual((from_contact.client, from_contact.to), (self.acme, "Anna K <anna@acme.com>"))

    def test_the_projects_own_contact_comes_first(self):
        from clients.models import Contact

        lead = Contact.objects.create(client=self.acme, first_name="Lea", last_name="D",
                                      email="lea@acme.com")
        self.website.primary_contact = lead
        self.website.save()

        self.assertEqual(draft_for(self.user, project=self.website).to, "Lea D <lea@acme.com>")

    def test_reply_to_received_mail(self):
        parent = self.stored(self.inbound(cc=("boss@acme.com", "hello@studio.hu")),
                             project=self.website, match_method=MatchMethod.REFERENCE)
        parent.from_name, parent.body_text = "Anna K", "Can we launch?\n\nThanks"
        parent.save()

        reply = draft_for(self.user, reply_to=parent)

        self.assertEqual(reply.to, "Anna K <anna@acme.com>")
        self.assertEqual(reply.cc, "boss@acme.com")  # the studio is not copied to itself
        self.assertEqual(reply.subject, "Re: Hello")
        self.assertEqual((reply.client, reply.project, reply.reply_to),
                         (self.acme, self.website, parent))
        self.assertIn("wrote:\n> Can we launch?\n>\n> Thanks", reply.body_text)
        self.assertTrue(reply.body_text.startswith("\n\n-- \nGábor"))

    def test_reply_to_sent_mail_goes_to_the_same_people(self):
        parent = self.stored(self.outbound("anna@acme.com", subject="Re: Launch"))

        reply = draft_for(self.user, reply_to=parent)

        self.assertEqual((reply.to, reply.subject), ("anna@acme.com", "Re: Launch"))

    def test_from_a_quote_attaches_a_copy_of_the_pdf(self):
        quote = Quote.objects.create(project=self.website, number="Q-2026-0001", contact=self.anna,
                                     language="hu")
        quote.pdf.save("q.pdf", ContentFile(b"%PDF quote"), save=True)

        email = draft_for(self.user, quote=quote)

        self.assertEqual(email.subject, "Árajánlat Q-2026-0001 - Website")
        self.assertTrue(email.body_text.startswith("Kedves Anna!"))
        attachment = email.attachments.get()
        self.assertEqual(attachment.display_name, "Q-2026-0001.pdf")
        self.assertEqual(attachment.file.open("rb").read(), b"%PDF quote")
        self.assertNotEqual(attachment.file.name, quote.pdf.name)  # a copy, not a link

    def test_english_quote(self):
        quote = Quote.objects.create(project=self.website, number="Q-2026-0002", language="en")

        email = draft_for(self.user, quote=quote)

        self.assertEqual(email.subject, "Quote Q-2026-0002 - Website")
        self.assertEqual(email.to, "Anna K <anna@acme.com>")  # the client's primary-ish contact
        self.assertFalse(email.attachments.exists())  # no PDF yet, nothing attached

    def test_from_a_document_to_its_signers_with_the_latest_file(self):
        nda = DocumentType.objects.get(prefix="NDA")
        document = Document.objects.create(document_type=nda, client=self.acme, title="NDA",
                                           project=self.website)
        DocumentSigner.objects.create(document=document, contact=self.anna)
        DocumentSigner.objects.create(document=document, name="Me", email="me@studio.hu",
                                      is_studio_side=True)
        for kind, content in [(DocumentFile.Kind.DRAFT, b"draft"),
                              (DocumentFile.Kind.EXECUTED, b"signed")]:
            DocumentFile.objects.create(document=document, kind=kind,
                                        file=ContentFile(content, name=f"{kind}.pdf"))

        email = draft_for(self.user, document=document)

        self.assertEqual(email.to, "Anna K <anna@acme.com>")  # the studio signer is left out
        self.assertEqual(email.subject, "Non-Disclosure Agreement: NDA")  # no "Draft #1"
        self.assertEqual(email.attachments.get().file.open("rb").read(), b"signed")


@override_settings(CLOUD_STORAGE=BUCKET)
class ProblemTests(SendingFixtureMixin, TestCase):
    def test_a_ready_email_has_none(self):
        self.assertEqual(problems(self.ready()), [])

    def test_each_problem_is_named(self):
        profile = StudioProfile.get_solo()
        profile.email = ""
        profile.save()
        email = OutgoingEmail.objects.create(to="nope", cc="", subject=" ")

        found = problems(email)

        self.assertEqual(len(found), 4)
        self.assertIn("Studio Profile", found[0])
        self.assertIn("To: Not an email address: nope", found[1])
        self.assertIn("at least one recipient", found[2])
        self.assertIn("Write a subject", found[3])

    def test_attachment_size_limit(self):
        email = self.ready()
        OutgoingAttachment(email=email).file.save("big.bin", ContentFile(b"x"), save=True)
        with mock.patch.object(outgoing, "MAX_TOTAL_ATTACHMENT_BYTES", 0):
            self.assertIn("limit", " ".join(problems(email)))

    def test_queueing_refuses_problems_and_repeats(self):
        with self.assertRaisesMessage(SendError, "Write a subject"):
            queue_for_sending(self.ready(subject=""))

        sent = self.send(self.ready())
        with self.assertRaisesMessage(SendError, "sent already"):
            queue_for_sending(sent)


@override_settings(CLOUD_STORAGE=BUCKET)
class SendTests(SendingFixtureMixin, TestCase):
    def test_sends_and_logs_the_copy(self):
        email = self.send(self.ready(cc="Boss <boss@acme.com>", bcc="audit@studio.hu"))

        self.assertEqual(email.status, OutgoingStatus.SENT)
        self.assertEqual((email.attempts, email.sent_by), (1, self.user))
        self.assertIsNotNone(email.sent_at)
        [sent] = mail.outbox
        self.assertEqual(sent.subject, f"[{self.website.number}] Launch date")
        self.assertEqual(sent.from_email, "WebStudio Kft. <hello@studio.hu>")
        self.assertEqual(sent.to, ["Anna K <anna@acme.com>"])
        self.assertEqual(sent.bcc, ["audit@studio.hu"])
        self.assertEqual(sent.extra_headers["Message-ID"], email.message_id)
        self.assertTrue(email.message_id.endswith("@studio.hu>"))

        logged = email.sent_message
        self.assertEqual(logged.message_id, email.message_id)
        self.assertEqual(logged.direction, Direction.OUTBOUND)
        self.assertEqual((logged.client, logged.project), (self.acme, self.website))
        self.assertEqual(logged.match_method, MatchMethod.MANUAL)
        self.assertEqual(logged.match_note, "Sent from the CRM by gabi")
        self.assertEqual(logged.bcc_addresses, [{"name": "", "email": "audit@studio.hu"}])
        self.assertEqual(logged.cc_addresses, [{"name": "Boss", "email": "boss@acme.com"}])
        self.assertTrue(logged.raw_size > 0)
        activity = Activity.objects.get(kind=ActivityKind.EMAIL_OUT)
        self.assertEqual((activity.client, activity.project), (self.acme, self.website))

    def test_a_reply_carries_the_thread_headers_and_joins_the_thread(self):
        parent = self.stored(self.inbound(references=("<root@acme.com>",)),
                             message_id="<parent@acme.com>", client=self.acme,
                             match_method=MatchMethod.CONTACT)
        draft = draft_for(self.user, reply_to=parent)

        email = self.send(draft)

        headers = mail.outbox[0].extra_headers
        self.assertEqual(headers["In-Reply-To"], "<parent@acme.com>")
        self.assertEqual(headers["References"], "<root@acme.com> <parent@acme.com>")
        self.assertEqual(email.sent_message.thread, parent.thread)

    def test_attachments_are_sent_and_logged(self):
        email = self.ready()
        attachment = OutgoingAttachment(email=email, filename="brief.pdf")
        attachment.file.save("brief.pdf", ContentFile(b"%PDF brief"), save=True)

        email = self.send(email)

        self.assertEqual(mail.outbox[0].attachments[0][:2], ("brief.pdf", b"%PDF brief"))
        logged = email.sent_message.attachments.get()
        self.assertEqual((logged.filename, logged.file.open("rb").read()),
                         ("brief.pdf", b"%PDF brief"))

    def test_without_a_client_the_copy_is_matched_normally(self):
        email = OutgoingEmail.objects.create(to="gus@globex.com", subject="Hi", body_text="x")

        email = self.send(email)

        self.assertEqual(email.sent_message.client, self.globex)
        self.assertEqual(email.sent_message.match_method, MatchMethod.CONTACT)

    def test_the_same_mail_read_back_from_a_sent_folder_is_a_duplicate(self):
        email = self.send(self.ready())

        again = ingest(email.sent_message.raw_file.open("rb").read())

        self.assertFalse(again.created)
        self.assertEqual(EmailMessage.objects.filter(direction="out").count(), 1)


@override_settings(CLOUD_STORAGE=BUCKET)
class FailureTests(SendingFixtureMixin, TestCase):
    def send_with(self, side_effects):
        connection = mock.Mock()
        connection.send_messages.side_effect = side_effects
        with mock.patch.object(outgoing, "get_connection", return_value=connection):
            email = self.send(self.ready())
        return email, connection

    def test_a_connection_problem_is_retried_and_can_succeed(self):
        email, connection = self.send_with([ConnectionRefusedError("down"), 1])

        self.assertEqual((email.status, email.attempts), (OutgoingStatus.SENT, 2))
        self.assertEqual(connection.send_messages.call_count, 2)
        self.assertEqual(email.last_error, "")

    def test_three_connection_problems_and_it_gives_up(self):
        email, connection = self.send_with([smtplib.SMTPServerDisconnected("bye")] * 3)

        self.assertEqual((email.status, email.attempts), (OutgoingStatus.FAILED, 3))
        self.assertIn("Gave up after 3 attempts", email.last_error)
        self.assertIsNone(email.sent_message)

    def test_a_refused_recipient_is_not_retried(self):
        refused = smtplib.SMTPRecipientsRefused({"anna@acme.com": (550, b"No such user")})
        email, connection = self.send_with([refused])

        self.assertEqual((email.status, email.attempts), (OutgoingStatus.FAILED, 1))
        self.assertIn("SMTPRecipientsRefused", email.last_error)

    def test_a_server_that_accepts_nothing_is_a_failure(self):
        email, _ = self.send_with([0])

        self.assertEqual(email.status, OutgoingStatus.FAILED)
        self.assertIn("did not accept", email.last_error)

    def test_an_interrupted_send_is_never_repeated(self):
        email = self.ready()
        OutgoingEmail.objects.filter(pk=email.pk).update(status=OutgoingStatus.SENDING)

        deliver(email.pk)

        email.refresh_from_db()
        self.assertEqual(email.status, OutgoingStatus.FAILED)
        self.assertIn("unknown whether the mail server took the email", email.last_error)
        self.assertEqual(mail.outbox, [])

    def test_send_again_keeps_the_message_id(self):
        email, _ = self.send_with([smtplib.SMTPRecipientsRefused({})])
        first_id = email.message_id

        email = self.send(email)

        self.assertEqual((email.status, email.message_id), (OutgoingStatus.SENT, first_id))

    def test_sent_but_not_logged_still_counts_as_sent(self):
        with mock.patch.object(outgoing, "_log_copy", side_effect=RuntimeError("disk full")):
            email = self.send(self.ready())

        self.assertEqual(email.status, OutgoingStatus.SENT)
        self.assertIn("could not be logged: disk full", email.last_error)
        self.assertEqual(len(mail.outbox), 1)

    def test_a_draft_or_finished_email_is_left_alone_by_the_worker(self):
        draft = self.ready()
        self.assertEqual(deliver(draft.pk), OutgoingStatus.DRAFT)
        self.assertIsNone(deliver(999))
        self.assertEqual(mail.outbox, [])


class OutboxBackendTests(SimpleTestCase):
    def test_writes_each_email_as_an_eml_file(self):
        from django.core.mail import EmailMessage as MailMessage

        folder = Path(tempfile.mkdtemp(prefix="outbox-"))
        backend = OutboxFolderBackend(path=folder)
        message = MailMessage("Launch: soon!", "Body", "a@studio.hu", ["Anna <b@acme.com>"],
                              bcc=["audit@studio.hu"])

        self.assertEqual(backend.send_messages([message]), 1)

        [file] = folder.iterdir()
        self.assertTrue(file.name.endswith("-Launch-soon.eml"))
        content = file.read_text()
        self.assertIn("Subject: Launch: soon!", content)
        self.assertIn("X-Outbox-Bcc: audit@studio.hu", content)
        self.assertEqual(backend.send_messages([]), 0)


@override_settings(CLOUD_STORAGE=BUCKET)
class SendingAdminTests(SendingFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_superuser("admin", "a@x.hu", "pw")
        self.client.force_login(self.admin)

    def change_url(self, email):
        return reverse("admin:communications_outgoingemail_change", args=[email.pk])

    def form(self, email, **fields):
        data = {
            "client": email.client_id or "", "project": email.project_id or "",
            "to": email.to, "cc": email.cc, "bcc": email.bcc, "subject": email.subject,
            "body_text": email.body_text,
            "attachments-TOTAL_FORMS": "0", "attachments-INITIAL_FORMS": "0",
        }
        data.update(fields)
        return data

    def test_compose_buttons_appear_on_the_pages(self):
        message = self.stored(self.inbound())
        pages = {
            reverse("admin:clients_client_change", args=[self.acme.pk]): "Send email",
            reverse("admin:clients_contact_change", args=[self.anna.pk]): "Send email",
            reverse("admin:projects_project_change", args=[self.website.pk]): "Send email",
            reverse("admin:communications_emailmessage_change", args=[message.pk]): "Reply",
        }
        for url, label in pages.items():
            with self.subTest(url):
                self.assertContains(self.client.get(url), f">{label}</button>")

    def test_compose_starts_a_draft_and_opens_it(self):
        url = reverse("admin:communications_outgoingemail_compose")

        self.assertEqual(self.client.get(url).status_code, 405)
        response = self.client.post(url, {"project": self.website.pk})

        email = OutgoingEmail.objects.get()
        self.assertRedirects(response, self.change_url(email))
        self.assertEqual((email.project, email.created_by), (self.website, self.admin))
        self.assertEqual(self.client.post(url, {"project": 999}).status_code, 403)

    def test_save_and_send(self):
        email = draft_for(self.user, project=self.website)
        page = self.client.get(self.change_url(email))
        self.assertContains(page, "Save and send")
        self.assertContains(page, "Write a subject")  # problems shown before sending

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(self.change_url(email),
                                        self.form(email, subject="Hello", _send="1"), follow=True)

        self.assertContains(response, "Handed over for sending")
        email.refresh_from_db()
        self.assertEqual(email.status, OutgoingStatus.SENT)
        self.assertEqual(len(mail.outbox), 1)
        # Now a record: no edit form, no Save and send
        page = self.client.get(self.change_url(email))
        self.assertNotContains(page, "Save and send")
        self.assertNotContains(page, 'name="subject"')
        self.assertEqual(self.client.get(reverse(
            "admin:communications_outgoingemail_delete", args=[email.pk])).status_code, 403)

    def test_problems_stop_the_send(self):
        email = draft_for(self.user, project=self.website)

        response = self.client.post(self.change_url(email), self.form(email, _send="1"),
                                    follow=True)

        self.assertContains(response, "Not sent: Write a subject.")
        email.refresh_from_db()
        self.assertEqual(email.status, OutgoingStatus.DRAFT)

    def test_send_again_button_for_failed_email(self):
        email = self.ready()
        OutgoingEmail.objects.filter(pk=email.pk).update(status=OutgoingStatus.FAILED,
                                                         last_error="Refused")
        page = self.client.get(self.change_url(email))
        self.assertContains(page, "Send again")
        self.assertContains(page, "Refused")

        url = reverse("admin:communications_outgoingemail_send_again", args=[email.pk])
        self.assertEqual(self.client.get(url).status_code, 405)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(url)

        email.refresh_from_db()
        self.assertEqual(email.status, OutgoingStatus.SENT)

    def test_list_shows_status(self):
        self.ready()
        response = self.client.get(reverse("admin:communications_outgoingemail_changelist"))
        self.assertContains(response, ">Draft</span>")
