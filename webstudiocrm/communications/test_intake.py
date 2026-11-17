"""Mail intake: parsing, threading, ingest, sources and the sync."""

import tempfile
from datetime import timedelta
from email.message import EmailMessage as Mime
from io import StringIO
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from cloudstorage.testing import temp_bucket

from .choices import Direction, MatchMethod
from .models import EmailAttachment, EmailMessage, EmailThread, MailboxState, MailSyncRun
from .services.ingest import find_thread, ingest
from .services.parsing import ParseError, html_to_text, parse_email, sanitize_html
from .services.sources import MailSourceError, get_source
from .services.sources.base import IncomingMail, MailSource
from .services.sources.folder import FolderMailSource
from .services.sources.imap import ImapMailSource
from .services.sync import sync
from .test_matching import MatchingFixtureMixin

BUCKET = temp_bucket()


def eml(sender="Anna K <anna@acme.com>", to="hello@studio.hu", subject="Hello",
        body="Hi there", msgid="<1@acme.com>", date="Mon, 09 Nov 2026 10:00:00 +0100",
        extra=""):
    lines = [f"From: {sender}", f"To: {to}", f"Subject: {subject}"]
    if date:
        lines.append(f"Date: {date}")
    if msgid:
        lines.append(f"Message-ID: {msgid}")
    if extra:
        lines.append(extra.strip())
    lines += ["Content-Type: text/plain; charset=utf-8", "", body, ""]
    return "\r\n".join(lines).encode()


def multipart():
    mime = Mime()
    mime["From"] = "Anna <anna@acme.com>"
    mime["To"] = "hello@studio.hu"
    mime["Subject"] = "Banner"
    mime["Message-ID"] = "<mp@acme.com>"
    mime.set_content("Plain version")
    mime.add_alternative(
        '<p onclick="x()">Hi <img src="cid:pic@acme.com"><img src="https://t.example/p.gif">'
        "</p><script>alert(1)</script>",
        subtype="html",
    )
    mime.get_payload()[1].add_related(b"PNGDATA", maintype="image", subtype="png",
                                      cid="<pic@acme.com>")
    mime.add_attachment(b"%PDF brief", maintype="application", subtype="pdf",
                        filename="brief.pdf")
    return mime.as_bytes()


class ParserTests(SimpleTestCase):
    def test_headers_and_addresses(self):
        parsed = parse_email(eml(
            to="Studio <Hello@Studio.hu>, Boss <boss@acme.com>",
            extra="Cc: x@acme.com, not-an-address\r\nIn-Reply-To: <0@studio.hu>\r\n"
                  "References: <a@x> <0@studio.hu>",
        ))

        self.assertEqual((parsed.from_name, parsed.from_address), ("Anna K", "anna@acme.com"))
        self.assertEqual(parsed.to, [{"name": "Studio", "email": "hello@studio.hu"},
                                     {"name": "Boss", "email": "boss@acme.com"}])
        self.assertEqual(parsed.cc, [{"name": "", "email": "x@acme.com"}])
        self.assertEqual(parsed.message_id, "<1@acme.com>")
        self.assertEqual(parsed.in_reply_to, "<0@studio.hu>")
        self.assertEqual(parsed.references, ["<a@x>", "<0@studio.hu>"])
        self.assertEqual(parsed.sent_at.isoformat(), "2026-11-09T10:00:00+01:00")
        self.assertFalse(parsed.synthetic_id)

    def test_encoded_hungarian_headers_and_body(self):
        raw = (
            "From: =?utf-8?q?G=C3=A1bor?= <gabor@acme.com>\r\n"
            "Subject: =?utf-8?b?w4FyYWrDoW5sYXQga8OpcmTDqXM=?=\r\n"
            "Message-ID: <hu@acme.com>\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            "Content-Transfer-Encoding: quoted-printable\r\n\r\n"
            "Sz=C3=ADvesen.\r\n"
        ).encode()

        parsed = parse_email(raw)

        self.assertEqual(parsed.from_name, "Gábor")
        self.assertEqual(parsed.subject, "Árajánlat kérdés")
        self.assertEqual(parsed.body_text, "Szívesen.")

    def test_missing_message_id_gets_a_stable_stand_in(self):
        raw = eml(msgid=None)

        first, second = parse_email(raw), parse_email(raw)

        self.assertTrue(first.synthetic_id)
        self.assertEqual(first.message_id, second.message_id)
        self.assertTrue(first.message_id.endswith("@webstudiocrm.invalid>"))
        self.assertNotEqual(first.message_id, parse_email(eml(msgid=None, body="x")).message_id)

    def test_missing_or_broken_date_is_none(self):
        self.assertIsNone(parse_email(eml(date=None)).sent_at)
        self.assertIsNone(parse_email(eml(date="yesterday-ish")).sent_at)

    def test_not_an_email_is_refused(self):
        for raw in [b"", b"   ", b"Just some text\n", b"Subject: no sender\r\n\r\nbody"]:
            with self.subTest(raw), self.assertRaises(ParseError):
                parse_email(raw)

    def test_multipart_bodies_and_attachments(self):
        parsed = parse_email(multipart())

        self.assertEqual(parsed.body_text, "Plain version")
        self.assertNotIn("script", parsed.body_html)
        self.assertNotIn("onclick", parsed.body_html)
        self.assertNotIn("t.example", parsed.body_html)  # remote image: no tracking
        self.assertIn('src="cid:pic@acme.com"', parsed.body_html)
        by_name = {a.filename: a for a in parsed.attachments}
        self.assertEqual(set(by_name), {"brief.pdf", "inline-1.png"})
        self.assertEqual(by_name["brief.pdf"].content, b"%PDF brief")
        self.assertFalse(by_name["brief.pdf"].is_inline)
        self.assertTrue(by_name["inline-1.png"].is_inline)
        self.assertEqual(by_name["inline-1.png"].content_id, "pic@acme.com")

    def test_html_only_mail_gets_a_text_body(self):
        raw = (
            "From: a@acme.com\r\nMessage-ID: <h@acme.com>\r\nContent-Type: text/html\r\n\r\n"
            "<html><head><style>p{}</style></head><body><p>Hello&nbsp;there</p>"
            "<p>Line &amp; two</p><script>x()</script></body></html>"
        ).encode()

        parsed = parse_email(raw)

        self.assertEqual(parsed.body_text, "Hello\xa0there\n\nLine & two")

    def test_sanitizer_and_text_helpers(self):
        self.assertEqual(sanitize_html('<a href="javascript:x()">y</a>'),
                         '<a rel="noopener noreferrer nofollow">y</a>')
        self.assertEqual(html_to_text("<div>a</div><div>b</div>"), "a\n\nb")


class ThreadTests(MatchingFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.now = timezone.now()

    def test_reply_headers_find_the_thread_in_either_direction(self):
        parent = ingest(eml(msgid="<p@acme.com>")).message
        reply = ingest(eml(msgid="<r@studio.hu>", subject="Re: Hello",
                           extra="In-Reply-To: <p@acme.com>")).message
        self.assertEqual(reply.thread, parent.thread)

        # A reply that arrived before its parent pulls the parent in
        early_reply = ingest(eml(msgid="<r2@acme.com>", subject="Re: Other",
                                 extra="In-Reply-To: <late@acme.com>")).message
        late_parent = ingest(eml(msgid="<late@acme.com>", subject="Other")).message
        self.assertEqual(late_parent.thread, early_reply.thread)

    def test_subject_fallback_needs_a_shared_party_and_the_time_window(self):
        first = ingest(eml(msgid="<s1@acme.com>", subject="Homepage")).message
        same = ingest(eml(msgid="<s2@acme.com>", subject="RE: Homepage")).message
        stranger = ingest(eml(sender="x@other.com", msgid="<s3@other.com>",
                              subject="Re: Homepage")).message
        much_later = ingest(eml(msgid="<s4@acme.com>", subject="Re: Homepage",
                                date="Mon, 01 Mar 2027 10:00:00 +0100")).message

        self.assertEqual(same.thread, first.thread)
        self.assertNotEqual(stranger.thread, first.thread)
        self.assertNotEqual(much_later.thread, first.thread)
        self.assertEqual(first.thread.subject, "Homepage")

    def test_studio_addresses_do_not_count_as_a_shared_party(self):
        ingest(eml(sender="a@one.com", msgid="<o1@one.com>", subject="Hello"))
        other = ingest(eml(sender="b@two.com", msgid="<o2@two.com>", subject="Hello")).message

        self.assertEqual(EmailThread.objects.count(), 2)
        self.assertEqual(other.thread.messages.count(), 1)

    def test_empty_subject_starts_a_new_thread(self):
        ingest(eml(msgid="<e1@acme.com>", subject=""))
        ingest(eml(msgid="<e2@acme.com>", subject="Re:"))

        self.assertEqual(EmailThread.objects.count(), 2)


@override_settings(CLOUD_STORAGE=BUCKET)
class IngestTests(MatchingFixtureMixin, TestCase):
    def test_logs_stores_and_matches(self):
        raw = multipart()
        result = ingest(raw)

        message = result.message
        self.assertTrue(result.created)
        self.assertTrue(result.matched)
        self.assertEqual((message.client, message.match_method), (self.acme, MatchMethod.CONTACT))
        self.assertEqual(message.direction, Direction.INBOUND)
        self.assertEqual(message.raw_file.open("rb").read(), raw)
        self.assertEqual(message.raw_size, len(raw))
        self.assertEqual(message.attachments.count(), 2)
        pdf = message.attachments.get(filename="brief.pdf")
        self.assertEqual(pdf.file.open("rb").read(), b"%PDF brief")
        self.assertEqual(len(pdf.checksum_sha256), 64)
        self.assertEqual(sorted(message.parties.values_list("address", flat=True)),
                         ["anna@acme.com", "hello@studio.hu"])

    def test_the_same_mail_is_logged_once(self):
        first = ingest(eml())
        again = ingest(eml())

        self.assertFalse(again.created)
        self.assertEqual(again.message, first.message)
        self.assertEqual(EmailMessage.objects.count(), 1)

    def test_direction_from_the_source_or_the_studio_address(self):
        sent = ingest(eml(sender="hello@studio.hu", to="anna@acme.com", msgid="<s@s>")).message
        forced = ingest(eml(msgid="<f@f>"), direction=Direction.OUTBOUND).message

        self.assertEqual(sent.direction, Direction.OUTBOUND)
        self.assertEqual(sent.client, self.acme)  # matched by its recipient
        self.assertEqual(forced.direction, Direction.OUTBOUND)

    def test_fallback_time_when_the_date_is_missing(self):
        when = timezone.now() - timedelta(days=3)

        message = ingest(eml(date=None), fallback_time=when).message

        self.assertEqual(message.sent_at, when)

    def test_a_failure_leaves_no_rows_and_no_files(self):
        with mock.patch("communications.services.ingest.match_message",
                        side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                ingest(multipart())

        from django.core.files.storage import default_storage

        from cloudstorage.models import StoredObject

        self.assertFalse(EmailMessage.objects.exists())
        self.assertFalse(EmailAttachment.objects.exists())
        # Every file written during the failed ingest reads as missing again
        keys = list(StoredObject.objects.filter(key__startswith="email/")
                    .values_list("key", flat=True))
        self.assertEqual(len(keys), 3)  # the .eml, the PDF and the inline image
        self.assertFalse([k for k in keys if default_storage.exists(k)])

    def test_a_concurrent_duplicate_resolves_to_the_existing_message(self):
        existing = ingest(eml()).message
        # The pre-check misses, as if another process had not committed yet;
        # the insert then hits the unique Message-ID and finds the winner
        with mock.patch("communications.services.ingest._already_logged",
                        side_effect=[None, existing]):
            result = ingest(eml())

        self.assertFalse(result.created)
        self.assertEqual(result.message, existing)


class FakeSource(MailSource):
    name = "fake"

    def __init__(self, mails, explode=None):
        super().__init__({})
        self.mails, self.explode = mails, explode
        self.acknowledged, self.rejected, self.closed = [], [], False

    @property
    def label(self):
        return "fake"

    def fetch(self):
        yield from self.mails
        if self.explode:
            raise self.explode

    def acknowledge(self, mail):
        self.acknowledged.append(mail.key)

    def reject(self, mail, error):
        self.rejected.append(mail.key)

    def close(self):
        self.closed = True


@override_settings(CLOUD_STORAGE=BUCKET)
class SyncTests(MatchingFixtureMixin, TestCase):
    def mail(self, key, raw):
        return IncomingMail(key=key, raw=raw, direction=Direction.INBOUND)

    def test_counts_acknowledges_and_rejects(self):
        source = FakeSource([
            self.mail("a", eml(msgid="<a@acme.com>")),
            self.mail("b", eml(sender="x@nowhere.com", msgid="<b@x>")),
            self.mail("dup", eml(msgid="<a@acme.com>")),
            self.mail("bad", b"not an email"),
        ])

        run = sync(source)

        self.assertEqual(run.status, MailSyncRun.Status.DONE)
        self.assertEqual((run.fetched, run.logged, run.matched, run.duplicates, run.failed),
                         (4, 2, 1, 1, 1))
        self.assertEqual(source.acknowledged, ["a", "b", "dup"])
        self.assertEqual(source.rejected, ["bad"])
        self.assertIn("bad: No sender address", run.errors)
        self.assertTrue(source.closed)
        self.assertIsNotNone(run.finished_at)

    def test_an_unexpected_error_is_left_for_the_next_sync(self):
        source = FakeSource([self.mail("a", eml())])
        with mock.patch("communications.services.sync.ingest", side_effect=RuntimeError("db")):
            run = sync(source)

        self.assertEqual((run.failed, source.acknowledged, source.rejected), (1, [], []))
        self.assertIn("(will retry)", run.errors)

    def test_a_source_error_fails_the_run(self):
        run = sync(FakeSource([], explode=MailSourceError("server down")))

        self.assertEqual(run.status, MailSyncRun.Status.FAILED)
        self.assertEqual(run.errors, "server down")

    def test_overlapping_runs_are_skipped_but_stale_locks_expire(self):
        MailSyncRun.objects.create(source="fake")  # still running

        skipped = sync(FakeSource([self.mail("a", eml())]))
        self.assertEqual(skipped.status, MailSyncRun.Status.SKIPPED)
        self.assertFalse(EmailMessage.objects.exists())

        MailSyncRun.objects.filter(status="running").update(
            started_at=timezone.now() - timedelta(hours=1)
        )
        self.assertEqual(sync(FakeSource([self.mail("a", eml())])).logged, 1)


@override_settings(CLOUD_STORAGE=BUCKET)
class FolderSourceTests(MatchingFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.root = Path(tempfile.mkdtemp(prefix="mail-"))
        self.source = FolderMailSource({"PATH": str(self.root)})
        self.source.ensure_layout()

    def drop(self, folder, name, raw):
        path = self.root / folder / name
        path.write_bytes(raw)
        return path

    def test_reads_both_folders_moves_files_and_sets_direction(self):
        self.drop("inbox", "a.eml", eml(msgid="<in@acme.com>"))
        self.drop("sent", "b.eml", eml(sender="hello@studio.hu", to="anna@acme.com",
                                       msgid="<out@studio.hu>"))
        self.drop("inbox", "notes.txt", b"ignored: not .eml")
        self.drop("inbox", "broken.eml", b"rubbish")

        run = sync(self.source)

        self.assertEqual((run.logged, run.failed), (2, 1))
        directions = dict(EmailMessage.objects.values_list("message_id", "direction"))
        self.assertEqual(directions, {"<in@acme.com>": "in", "<out@studio.hu>": "out"})
        month = timezone.localdate().strftime("%Y-%m")
        self.assertTrue((self.root / "processed" / "inbox" / month / "a.eml").exists())
        self.assertTrue((self.root / "processed" / "sent" / month / "b.eml").exists())
        failed = self.root / "failed" / "inbox" / month
        self.assertTrue((failed / "broken.eml").exists())
        self.assertIn("No sender", (failed / "broken.eml.error.txt").read_text())
        self.assertEqual(sorted(p.name for p in (self.root / "inbox").iterdir()), ["notes.txt"])

    def test_older_files_first_so_a_parent_precedes_its_reply(self):
        import os

        reply = self.drop("inbox", "a-reply.eml", eml(msgid="<r@acme.com>", subject="Re: x",
                                                     extra="In-Reply-To: <p@studio.hu>"))
        parent = self.drop("sent", "z-parent.eml", eml(sender="hello@studio.hu",
                                                       to="anna@acme.com", msgid="<p@studio.hu>",
                                                       subject="x"))
        os.utime(parent, (1_700_000_000, 1_700_000_000))
        os.utime(reply, (1_700_000_100, 1_700_000_100))

        sync(self.source)

        self.assertEqual(EmailMessage.objects.get(message_id="<r@acme.com>").match_method,
                         MatchMethod.THREAD)

    def test_copying_a_processed_file_back_is_harmless(self):
        self.drop("inbox", "a.eml", eml(msgid=None))
        sync(self.source)
        self.drop("inbox", "a.eml", eml(msgid=None))

        run = sync(self.source)

        self.assertEqual((run.logged, run.duplicates), (0, 1))
        month = timezone.localdate().strftime("%Y-%m")
        self.assertTrue((self.root / "processed" / "inbox" / month / "a-1.eml").exists())

    def test_the_file_time_stands_in_for_a_missing_date(self):
        import os

        path = self.drop("inbox", "a.eml", eml(date=None))
        os.utime(path, (1_700_000_000, 1_700_000_000))

        sync(self.source)

        self.assertEqual(int(EmailMessage.objects.get().sent_at.timestamp()), 1_700_000_000)


class FakeImap:
    """Just enough of imaplib.IMAP4 for the source: two folders of messages."""

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port
        self.selected = None
        self.fetched = []
        self.logged_out = False

    def login(self, user, password):
        if password != "secret":
            import imaplib
            raise imaplib.IMAP4.error("AUTHENTICATIONFAILED")

    def select(self, folder, readonly=False):
        assert readonly, "the source must never open folders read-write"
        name = folder.strip('"')
        if name not in self.mailboxes:
            return "NO", [b"no such folder"]
        self.selected = name
        return "OK", [str(len(self.mailboxes[name])).encode()]

    def response(self, code):
        return code, [str(self.uidvalidity[self.selected]).encode()]

    def uid(self, command, *args):
        box = self.mailboxes[self.selected]
        if command == "SEARCH":
            low = int(args[1].split()[1].split(":")[0])
            uids = [u for u in box if u >= low] or [max(box)]  # "n:*" quirk
            return "OK", [" ".join(map(str, uids)).encode()]
        uid = int(args[0])
        assert args[1] == "(BODY.PEEK[])", "fetching must not mark mail as read"
        self.fetched.append((self.selected, uid))
        return "OK", [(f"1 (UID {uid} BODY[] {{1}}".encode(), box[uid]), b")"]

    def logout(self):
        self.logged_out = True


@override_settings(CLOUD_STORAGE=BUCKET)
class ImapSourceTests(MatchingFixtureMixin, TestCase):
    config = {"HOST": "imap.test", "USER": "hello@studio.hu", "PASSWORD": "secret",
              "FOLDERS": {"INBOX": "in", "Sent Items": "out"}}

    def setUp(self):
        super().setUp()
        FakeImap.mailboxes = {
            "INBOX": {5: eml(msgid="<i5@acme.com>"), 6: b"rubbish", 7: eml(msgid="<i7@acme.com>")},
            "Sent Items": {3: eml(sender="hello@studio.hu", to="anna@acme.com",
                                  msgid="<s3@studio.hu>")},
        }
        FakeImap.uidvalidity = {"INBOX": 100, "Sent Items": 200}
        patcher = mock.patch.object(ImapMailSource, "connection_class", FakeImap)
        patcher.start()
        self.addCleanup(patcher.stop)

    def source(self, **config):
        return ImapMailSource({**self.config, **config})

    def test_reads_folders_and_remembers_the_position(self):
        source = self.source()
        run = sync(source)

        self.assertEqual((run.logged, run.failed), (3, 1))
        self.assertEqual(
            dict(EmailMessage.objects.values_list("message_id", "direction")),
            {"<i5@acme.com>": "in", "<i7@acme.com>": "in", "<s3@studio.hu>": "out"},
        )
        positions = dict(MailboxState.objects.values_list("folder", "last_uid"))
        self.assertEqual(positions, {"INBOX": 7, "Sent Items": 3})
        self.assertTrue(source.connection is None)  # logged out and released

        # Nothing new: the "n:*" quirk returns the last message, which is skipped
        second = self.source()
        run = sync(second)
        self.assertEqual(run.fetched, 0)

    def test_new_mail_only_and_batches(self):
        sync(self.source())
        FakeImap.mailboxes["INBOX"][8] = eml(msgid="<i8@acme.com>")
        FakeImap.mailboxes["INBOX"][9] = eml(msgid="<i9@acme.com>")

        run = sync(self.source(BATCH=1))

        self.assertEqual(run.logged, 1)
        self.assertEqual(MailboxState.objects.get(folder="INBOX").last_uid, 8)

    def test_a_renumbered_folder_is_read_again_without_duplicates(self):
        sync(self.source())
        FakeImap.uidvalidity["INBOX"] = 101

        run = sync(self.source())

        self.assertEqual((run.logged, run.duplicates), (0, 2))

    def test_bad_credentials_or_folder_fail_the_run(self):
        login = sync(self.source(PASSWORD="wrong"))
        folder = sync(self.source(FOLDERS={"Nope": "in"}))
        unset = sync(self.source(HOST=""))

        self.assertIn("Cannot log in", login.errors)
        self.assertIn("No folder 'Nope'", folder.errors)
        self.assertIn("Set IMAP_HOST", unset.errors)
        self.assertTrue(all(r.status == MailSyncRun.Status.FAILED for r in (login, folder, unset)))


@override_settings(CLOUD_STORAGE=BUCKET)
class CommandAndAdminTests(MatchingFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.root = Path(tempfile.mkdtemp(prefix="mail-"))

    def test_get_source_reads_the_settings(self):
        with override_settings(MAIL={"SOURCE": "folder", "FOLDER": {"PATH": str(self.root)}}):
            self.assertEqual(get_source().root, self.root)
            self.assertIsInstance(get_source("imap"), ImapMailSource)
            with self.assertRaises(MailSourceError):
                get_source("pigeon")

    def test_sync_mail_command(self):
        (self.root / "inbox").mkdir(parents=True)
        (self.root / "inbox" / "a.eml").write_bytes(eml())
        out = StringIO()

        call_command("sync_mail", "--path", str(self.root), stdout=out)

        self.assertIn("1 read, 1 logged (1 matched)", out.getvalue())

    def test_mail_demo_writes_files_and_reset_cleans_up(self):
        out = StringIO()
        call_command("mail_demo", "--path", str(self.root), stdout=out)
        files = sorted(p.name for p in self.root.rglob("demo-*.eml"))
        self.assertEqual(len(files), 16)

        run = sync(FolderMailSource({"PATH": str(self.root)}))
        self.assertEqual((run.fetched, run.logged, run.duplicates, run.failed), (16, 14, 1, 1))

        call_command("mail_demo", "--reset", "--path", str(self.root), stdout=StringIO())
        self.assertFalse(list(self.root.rglob("demo-*")))
        self.assertFalse(EmailMessage.objects.filter(subject="This week in design").exists())
        self.assertTrue(EmailMessage.objects.count() == 0)

    def test_sync_button_and_pages(self):
        user = User.objects.create_superuser("admin", "a@x.hu", "pw")
        self.client.force_login(user)
        changelist = reverse("admin:communications_emailmessage_changelist")
        with override_settings(MAIL={"SOURCE": "folder", "FOLDER": {"PATH": str(self.root)}}):
            (self.root / "inbox").mkdir(parents=True)
            (self.root / "inbox" / "a.eml").write_bytes(multipart())

            self.assertContains(self.client.get(changelist), "Sync mail now")
            self.assertEqual(self.client.get(reverse("admin:communications_emailmessage_sync"))
                             .status_code, 405)
            response = self.client.post(reverse("admin:communications_emailmessage_sync"),
                                        follow=True)

        self.assertContains(response, "Mail sync: 1 read, 1 logged (1 matched)")
        message = EmailMessage.objects.get()
        page = self.client.get(reverse("admin:communications_emailmessage_change",
                                       args=[message.pk]))
        self.assertContains(page, "<iframe sandbox")
        self.assertNotContains(page, "<script>alert")
        self.assertEqual(
            self.client.get(reverse("admin:communications_mailsyncrun_changelist")).status_code,
            200,
        )
        # IMAP bookkeeping is not an admin page (any more)
        from django.urls import NoReverseMatch
        with self.assertRaises(NoReverseMatch):
            reverse("admin:communications_mailboxstate_changelist")


def with_attachment(msgid, filename, content):
    mime = Mime()
    mime["From"] = "Anna <anna@acme.com>"
    mime["To"] = "hello@studio.hu"
    mime["Subject"] = "Files"
    mime["Message-ID"] = msgid
    mime.set_content("See attached.")
    mime.add_attachment(content, maintype="application", subtype="pdf", filename=filename)
    return mime.as_bytes()


@override_settings(CLOUD_STORAGE=BUCKET)
class AttachmentStorageTests(MatchingFixtureMixin, TestCase):
    def test_same_file_name_in_two_emails_keeps_both_files(self):
        first = ingest(with_attachment("<a1@acme.com>", "brief.pdf", b"%PDF first")).message
        second = ingest(with_attachment("<a2@acme.com>", "brief.pdf", b"%PDF second")).message

        first_file = first.attachments.get()
        second_file = second.attachments.get()
        self.assertNotEqual(first_file.file.name, second_file.file.name)
        self.assertEqual(first_file.file.open("rb").read(), b"%PDF first")
        self.assertEqual(second_file.file.open("rb").read(), b"%PDF second")
        # Each download still carries the real file name
        self.assertTrue(first_file.file.name.endswith("/1/brief.pdf"))

    def test_same_file_name_twice_in_one_email(self):
        mime = Mime()
        mime["From"], mime["Message-ID"] = "anna@acme.com", "<twice@acme.com>"
        mime.set_content("Two scans")
        mime.add_attachment(b"one", maintype="image", subtype="png", filename="scan.png")
        mime.add_attachment(b"two", maintype="image", subtype="png", filename="scan.png")

        message = ingest(mime.as_bytes()).message

        contents = sorted(a.file.open("rb").read() for a in message.attachments.all())
        self.assertEqual(contents, [b"one", b"two"])

    def test_a_hostile_file_name_cannot_leave_the_message_folder(self):
        message = ingest(with_attachment("<evil@x>", "../../../settings.py", b"x")).message

        attachment = message.attachments.get()
        self.assertEqual(attachment.filename, "../../../settings.py")  # shown as sent
        self.assertRegex(attachment.file.name, r"^email/attachments/\d{4}/\d{2}/[0-9a-f]{16}/1/settings.py$")

    def test_the_download_url_serves_the_stored_bytes(self):
        message = ingest(with_attachment("<dl@acme.com>", "brief.pdf", b"%PDF-1.4 real")).message
        attachment = message.attachments.get()

        response = self.client.get(attachment.file.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertEqual(b"".join(response.streaming_content), b"%PDF-1.4 real")


@override_settings(CLOUD_STORAGE=BUCKET)
class DemoFileTests(SimpleTestCase):
    def test_demo_attachments_open_in_a_viewer(self):
        import io

        from PIL import Image

        from .management.commands.mail_demo import Command

        command = Command()
        raw = command._multipart("From: a@x.com\nTo: b@y.com\nSubject: s\nMessage-ID: <d@x>",
                                 "<p>x</p>")
        files = {a.filename: a.content for a in parse_email(raw).attachments}

        Image.open(io.BytesIO(files["inline-1.png"])).load()  # raises if broken
        pdf = files["brief.pdf"]
        self.assertTrue(pdf.startswith(b"%PDF-"))
        for part in (b"/Type /Page", b"xref", b"trailer", b"%%EOF"):
            self.assertIn(part, pdf)


class ReviewBadgeTests(MatchingFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(User.objects.create_superuser("admin", "a@x.hu", "pw"))

    def test_email_list_warns_only_where_attention_is_needed(self):
        from .services.matching import match_message

        flagged = self.stored(self.inbound(sender="freddie@x.com"), client=self.acme,
                              match_method=MatchMethod.DOMAIN, needs_review=True,
                              match_note="Unknown sender at Acme's domain")
        self.stored(self.inbound())  # unmatched, nothing to review
        match_message(self.stored(self.inbound(subject=self.website.number)))  # confirmed

        response = self.client.get(reverse("admin:communications_emailmessage_changelist"))

        self.assertContains(response, "Needs review", count=1 + 1)  # badge + filter option
        self.assertContains(response, 'title="Unknown sender at Acme&#x27;s domain"')
        self.assertNotContains(response, 'alt="False"')  # no red crosses
        self.assertNotContains(response, 'alt="True"')
        self.assertTrue(flagged.needs_review)

    def test_activity_list_and_timeline_use_the_badge(self):
        from .services.activities import log_activity

        message = self.stored(self.inbound(), client=self.acme, match_method=MatchMethod.STALE,
                              needs_review=True, match_note="Contact was deleted")
        log_activity("note", client=self.acme, summary="Call")

        activities = self.client.get(reverse("admin:communications_activity_changelist"))
        client_page = self.client.get(reverse("admin:clients_client_change", args=[self.acme.pk]))

        self.assertNotContains(activities, 'alt="False"')
        self.assertContains(activities, "&#9888;&#xFE0E; Needs review", count=1)
        # One in the summary line ("1 to review"), one on the timeline entry
        self.assertContains(client_page, "&#9888;&#xFE0E; Needs review", count=2)
        self.assertContains(client_page, "Contact was deleted")
        self.assertTrue(message.activities.get().needs_review)
