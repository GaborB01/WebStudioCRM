"""Demo data and sample emails for trying the mail workflow by hand.

    python manage.py mail_demo            # demo clients + sample .eml files
    python manage.py mail_demo --reset    # remove the demo clients, their mail and files

Creates three demo clients (names end in "(demo)") and writes one .eml
per scenario into the folder source's inbox/ and sent/, using the real
project numbers the database hands out. Then run `sync_mail` and compare
the result with the "expect" line printed for each file.

Only touches clients named "... (demo)" and files named "demo-*.eml".
"""

import textwrap
from datetime import timedelta
from email.utils import format_datetime, make_msgid
from pathlib import Path

from django.core.management.base import BaseCommand
from django.utils import timezone

from clients.models import Client, ClientEmailDomain, Contact
from communications.services.sources.folder import FolderMailSource
from core.models import StudioProfile
from projects.models import Project

DEMO_SUFFIX = " (demo)"


def demo_image_bytes(text, size, image_format):
    """A small, valid image (PNG) or one-page document (PDF) with a label.

    Generated with Pillow rather than typed in as bytes: a viewer must be
    able to open what the demo attaches, or a broken demo file looks like
    a broken CRM.
    """
    import io

    from PIL import Image, ImageDraw

    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle([4, 4, size[0] - 5, size[1] - 5], outline=(30, 90, 160), width=3)
    draw.text((20, 20), text, fill=(30, 30, 30))
    buffer = io.BytesIO()
    image.save(buffer, format=image_format, **({"resolution": 72} if image_format == "PDF" else {}))
    return buffer.getvalue()


class Command(BaseCommand):
    help = "Create demo clients and sample emails for the folder mail source."

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true",
                            help="Delete the demo clients, their emails and the demo files.")
        parser.add_argument("--path", help="Folder to write to (default: MAIL['FOLDER']['PATH']).")

    def handle(self, *args, **options):
        from django.conf import settings

        path = options["path"] or settings.MAIL.get("FOLDER", {}).get("PATH")
        self.source = FolderMailSource({"PATH": path} if path else {})
        if options["reset"]:
            return self.reset()
        self.source.ensure_layout()
        self.setup_data()
        self.write_mail()

    # ---- data ---------------------------------------------------------------

    def setup_data(self):
        profile = StudioProfile.get_solo()
        if not profile.email:
            profile.email = "hello@studio.example"
            profile.save()
            self.stdout.write("Studio Profile email was empty: set to hello@studio.example")
        self.studio = profile.email.lower()

        self.acme, _ = Client.objects.get_or_create(name=f"Acme{DEMO_SUFFIX}")
        self.globex, _ = Client.objects.get_or_create(name=f"Globex{DEMO_SUFFIX}")
        ClientEmailDomain.objects.get_or_create(client=self.acme, domain="acme.example")

        self.anna = self._contact(self.acme, "Anna", "Kovács", "anna@acme.example", primary=True)
        self.gus = self._contact(self.globex, "Gus", "Grant", "gus@globex.example", primary=True)
        self._contact(self.acme, "Freddie", "Free", "freddie@freelance.example")
        self._contact(self.globex, "Freddie", "Free", "freddie@freelance.example")

        self.website = self._project(self.acme, "Acme Website", "website")
        self.webshop = self._project(self.globex, "Globex Webshop", "webshop")
        self._project(self.globex, "Globex App", "web_app")

        self.stdout.write(self.style.SUCCESS(
            f"Demo data: {self.acme} ({self.website.number}), "
            f"{self.globex} ({self.webshop.number} + one more open project)"
        ))

    def _contact(self, client, first, last, email, primary=False):
        contact, _ = Contact.objects.get_or_create(
            client=client, email=email,
            defaults={"first_name": first, "last_name": last, "is_primary": primary},
        )
        return contact

    def _project(self, client, name, kind):
        project, _ = Project.objects.get_or_create(
            client=client, name=name, defaults={"project_type": kind}
        )
        return project

    # ---- mail ---------------------------------------------------------------

    def write_mail(self):
        now = timezone.now()
        first_id = make_msgid(domain="acme.example")
        studio_reply_id = make_msgid(domain="studio.example")
        hungarian_subject = "Árajánlat kérdés"

        def when(hours_ago):
            return format_datetime(now - timedelta(hours=hours_ago))

        def headers(sender, to, subject, hours_ago, msgid=None, extra=""):
            lines = [
                f"From: {sender}",
                f"To: {to}",
                f"Subject: {subject}",
                f"Date: {when(hours_ago)}",
            ]
            if msgid is not False:
                lines.append(f"Message-ID: {msgid or make_msgid(domain='demo.example')}")
            lines.append("MIME-Version: 1.0")
            return "\n".join(lines) + ("\n" + extra.strip() if extra.strip() else "")

        def plain(head, body):
            return (f"{head}\nContent-Type: text/plain; charset=utf-8\n"
                    f"Content-Transfer-Encoding: 8bit\n\n{textwrap.dedent(body).strip()}\n")

        anna = "Anna Kovács <anna@acme.example>"
        studio = f"WebStudio <{self.studio}>"
        files = []

        def add(folder, name, content, expect):
            files.append((folder, name, content, expect))

        add("inbox", "demo-01-contact.eml", plain(
            headers(anna, studio, "Homepage copy", 30, msgid=first_id),
            "Hi, attached thoughts on the homepage copy.\n\nAnna"),
            "Acme / Acme Website - Known contact; project filled (only open project)")
        add("inbox", "demo-02-reference.eml", plain(
            headers("Nina New <nina.new@gmail.com>", studio,
                    f"[{self.webshop.number}] Logo files", 29),
            "Hello, I am new at Globex. Logo files for the shop are coming."),
            f"Globex / Globex Webshop - Reference {self.webshop.number}; not flagged")
        add("sent", "demo-03-sent-reply.eml", plain(
            headers(studio, anna, "Re: Homepage copy", 28, msgid=studio_reply_id,
                    extra=f"In-Reply-To: {first_id}\nReferences: {first_id}"),
            "Thanks Anna, we will update it this week."),
            "SENT, Acme / Acme Website - Reply to a matched email; same thread as 01")
        add("inbox", "demo-04-thread-reply.eml", plain(
            headers("Anna (home) <anna.private@gmail.com>", studio, "Re: Homepage copy", 27,
                    extra=f"In-Reply-To: {studio_reply_id}\n"
                          f"References: {first_id} {studio_reply_id}"),
            "Writing from home: looks great!"),
            "Acme / Acme Website - Reply to a matched email (unknown sender); thread of 01")
        add("inbox", "demo-05-domain.eml", plain(
            headers("Boss <boss@acme.example>", studio, "Invoice address", 26),
            "Please send invoices to our new address."),
            "Acme - Client's email domain; NEEDS REVIEW")
        add("inbox", "demo-06-unknown.eml", plain(
            headers("Bill Lumbergh <bill@initech.example>", studio, "New website?", 25),
            "We would like a quote for a new website."),
            "UNMATCHED - nobody knows initech.example (fixed later by adding the client)")
        add("inbox", "demo-07-conflict.eml", plain(
            headers("Gus Grant <gus@globex.example>", studio,
                    f"Question about {self.website.number}", 24),
            "Is this the Acme project? We saw it in our portfolio list."),
            f"Acme / Acme Website - Reference, but sender is Globex's: NEEDS REVIEW")
        add("inbox", "demo-08-newsletter.eml", plain(
            headers("Design Weekly <noreply@newsletter.example>", studio, "This week in design", 23),
            "Ten trends you can't miss."),
            "UNMATCHED - noreply@ senders are ignored")

        html = f"""\
            <html><body>
            <p>Hi! The <b>new banner</b> is below, the brief is attached.</p>
            <img src="cid:banner@acme.example" alt="banner">
            <img src="https://tracker.example/pixel.gif" alt="tracking pixel">
            <p><a href="https://acme.example" onclick="steal()">Our site</a></p>
            <script>alert('this must not survive')</script>
            </body></html>"""
        add("inbox", "demo-09-html-attachments.eml", self._multipart(
            headers(anna, studio, "Banner and brief", 22), textwrap.dedent(html)),
            "Acme / Acme Website - HTML shown sanitised (no script, no tracker), "
            "1 PDF + 1 inline image")
        add("inbox", "demo-10-duplicate.eml", plain(
            headers(anna, studio, "Homepage copy", 30, msgid=first_id),
            "Hi, attached thoughts on the homepage copy.\n\nAnna"),
            "SKIPPED as already logged (same Message-ID as 01); file still moves to processed/")
        add("inbox", "demo-11-broken.eml", "This is not an email at all.\nJust some text.\n",
            "FAILED - moved to failed/ with a .error.txt next to it")
        add("inbox", "demo-12-no-message-id.eml", plain(
            headers("Gus Grant <gus@globex.example>", studio, "Quick question", 21, msgid=False),
            "No Message-ID on this one."),
            "Globex (no project: two open) - Known contact; gets a made-up Message-ID")
        encoded = "=?utf-8?b?" + __import__("base64").b64encode(
            hungarian_subject.encode()).decode() + "?="
        add("inbox", "demo-13-hungarian.eml",
            headers("=?utf-8?q?Gus_Gr=C3=A1nt?= <gus@globex.example>", studio, encoded, 20)
            + "\nContent-Type: text/plain; charset=utf-8\nContent-Transfer-Encoding: "
              "quoted-printable\n\nSz=C3=ADvesen k=C3=A9rn=C3=A9nk egy =C3=A1raj=C3=A1nlatot.\n",
            f"Globex - subject reads '{hungarian_subject}', body has accents")
        add("inbox", "demo-14-shared-contact.eml", plain(
            headers("Freddie Free <freddie@freelance.example>", studio, "Availability", 19),
            "I can help on both projects next month."),
            "Acme or Globex - Known contact of both clients: NEEDS REVIEW")
        add("inbox", "demo-15-subject-thread.eml", plain(
            headers("Gus Grant <gus@globex.example>", studio, f"Re: {hungarian_subject}", 18),
            "Following up (my mail program drops reply headers)."),
            "Globex - joins the thread of 13 by subject (no reply headers)")
        add("inbox", "demo-16-quoted-reference.eml", plain(
            headers("Someone <someone@gmail.com>", studio, "Re: hello", 17),
            f"""\
            Thanks!

            > On Monday you wrote:
            > about {self.website.number}"""),
            "UNMATCHED - the number only appears in a quoted line")

        for folder, name, content, expect in files:
            target = self.source.root / folder / name
            target.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)
            self.stdout.write(f"  {folder}/{name:<32} expect: {expect}")
        self.stdout.write(self.style.SUCCESS(
            f"Wrote {len(files)} files to {self.source.root}. Now run: python manage.py sync_mail"
        ))

    def _multipart(self, head, html):
        from email.message import EmailMessage as Mime
        from email.parser import BytesParser
        from email import policy

        message = BytesParser(policy=policy.default).parsebytes((head + "\n\n").encode())
        mime = Mime()
        for key, value in message.items():
            if key.lower() != "mime-version":
                mime[key] = value
        mime.set_content("Hi! The new banner is attached, the brief too.")
        mime.add_alternative(html, subtype="html")
        mime.get_payload()[1].add_related(
            demo_image_bytes("Acme banner (demo)", (360, 90), "PNG"),
            maintype="image", subtype="png", cid="<banner@acme.example>", filename=None,
        )
        brief = "Website brief (demo)\n\nGoals: new homepage, faster checkout.\nDeadline: March."
        mime.add_attachment(demo_image_bytes(brief, (595, 842), "PDF"),  # A4 at 72 dpi
                            maintype="application", subtype="pdf", filename="brief.pdf")
        return mime.as_bytes()

    # ---- reset ----------------------------------------------------------------

    def reset(self):
        clients = Client.objects.filter(name__endswith=DEMO_SUFFIX)
        names = list(clients.values_list("name", flat=True))
        # Their emails and timeline go with them (client deletion cascades)
        for client in clients:
            client.documents.all().delete()
            client.delete()
        removed = 0
        if self.source.root.exists():
            for path in Path(self.source.root).rglob("demo-*"):
                if path.is_file():
                    path.unlink()
                    removed += 1
        # Unmatched demo emails have no client to cascade from
        from communications.models import EmailMessage

        leftovers = EmailMessage.objects.filter(
            client__isnull=True, parties__address__in=[
                "bill@initech.example", "noreply@newsletter.example", "someone@gmail.com",
            ],
        ).distinct()
        unmatched = leftovers.count()
        for message in leftovers:
            message.delete()
        self.stdout.write(self.style.SUCCESS(
            f"Removed {len(names)} demo client(s), {unmatched} unmatched demo email(s), "
            f"{removed} demo file(s)."
        ))
