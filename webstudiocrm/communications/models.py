"""Logged email and the activity timeline.

Three layers:

* EmailMessage - one email, as parsed from the mailbox or an inbound
  webhook. Carries the result of matching: which client and project it
  belongs to, how that was decided, and whether a person should check.
* EmailThread  - the conversation a message belongs to, found from the
  reply headers and the subject. Only a grouping; the client and project
  live on the messages, so one reply can be reassigned without moving the
  whole conversation.
* Activity     - the timeline shown on a client and a project: every
  matched email, every quote and document event, and notes typed by hand.

Who a message is matched to is a *fact about the message*: the same
person at the same address can be a contact of two clients, and a reply
can quote a different project than the thread started with.

Getting mail in (IMAP, webhooks) and deciding the match are the next two
steps; nothing here talks to a mail server.
"""

import hashlib
import re
import uuid
from pathlib import Path

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey, GenericRelation
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.utils.text import get_valid_filename

from core.emails import normalize_email

from .choices import (
    CONFIDENT_METHODS,
    EMAIL_KINDS,
    ActivityKind,
    Direction,
    MatchMethod,
    OutgoingStatus,
)

# Reply and forward markers in English and in Hungarian mail clients
# (Outlook writes "VÁ:" and "TOV:"), with optional counters like "Re[2]:",
# plus bracketed tags such as "[PRJ-2026-0001]" or "[EXTERNAL]".
_SUBJECT_PREFIX = re.compile(
    r"^\s*(?:(?:re|fw|fwd|aw|wg|vá|va|tov|továbbítás)\s*(?:\[\d+\])?\s*:\s*|\[[^\]]*\]\s*)+",
    re.IGNORECASE,
)


def normalize_subject(subject):
    """The subject with reply/forward prefixes and bracket tags removed.

    "Re: Fwd: [PRJ-2026-0001] Homepage copy" -> "Homepage copy"
    Used to group messages whose reply headers are missing or broken.
    """
    stripped = _SUBJECT_PREFIX.sub("", subject or "")
    return re.sub(r"\s+", " ", stripped).strip()


class EmailThread(models.Model):
    """A conversation: the messages that reply to one another."""

    subject = models.CharField(
        "Subject",
        max_length=998,
        blank=True,
        help_text="The first message's subject without Re:/Fwd: prefixes.",
    )
    first_message_at = models.DateTimeField("Started", null=True, blank=True, editable=False)
    last_message_at = models.DateTimeField("Last Message", null=True, blank=True, editable=False)
    created_at = models.DateTimeField("Created At", auto_now_add=True)

    class Meta:
        verbose_name = "Email Conversation"
        verbose_name_plural = "Email Conversations"
        ordering = [models.F("last_message_at").desc(nulls_last=True), "-pk"]

    def __str__(self):
        return self.subject or f"Thread #{self.pk}"

    @property
    def latest_matched_message(self):
        """The newest message with a client; its client and project stand for
        the conversation when a thread is displayed or inherited from."""
        return self.messages.filter(client__isnull=False).order_by("-sent_at", "-pk").first()

    def refresh_bounds(self):
        """Recompute the first/last timestamps from the messages."""
        bounds = self.messages.aggregate(
            first=models.Min("sent_at"), last=models.Max("sent_at")
        )
        self.first_message_at = bounds["first"]
        self.last_message_at = bounds["last"]
        self.save(update_fields=["first_message_at", "last_message_at"])


class EmailMessage(models.Model):
    """One email, in or out, and what it was matched to.

    Addresses are stored as lists of {"name": ..., "email": ...} so the
    display name survives and lookups stay simple. The HTML body is
    sanitised before it gets here; this model trusts it.
    """

    thread = models.ForeignKey(
        EmailThread,
        on_delete=models.CASCADE,
        related_name="messages",
        verbose_name="Thread",
    )
    message_id = models.CharField(
        "Message-ID",
        max_length=255,
        unique=True,
        help_text="From the email header. The same message is never logged twice.",
    )
    in_reply_to = models.CharField("In-Reply-To", max_length=255, blank=True)
    references = models.JSONField(
        "References", default=list, blank=True, help_text="Message-IDs of the ancestors."
    )

    direction = models.CharField("Direction", max_length=3, choices=Direction)
    from_name = models.CharField("From (name)", max_length=255, blank=True)
    from_address = models.CharField("From", max_length=254, db_index=True)
    to_addresses = models.JSONField("To", default=list, blank=True)
    cc_addresses = models.JSONField("Cc", default=list, blank=True)
    bcc_addresses = models.JSONField(
        "Bcc", default=list, blank=True, help_text="Only known for sent mail."
    )

    subject = models.CharField("Subject", max_length=998, blank=True)
    body_text = models.TextField("Body (text)", blank=True)
    body_html = models.TextField("Body (HTML, sanitised)", blank=True)
    sent_at = models.DateTimeField("Sent At", db_index=True)
    logged_at = models.DateTimeField("Logged At", auto_now_add=True)

    raw_file = models.FileField(
        "Original (.eml)",
        upload_to="email/%Y/%m/",
        blank=True,
        help_text="The message exactly as received, for re-parsing or disputes.",
    )
    raw_size = models.PositiveBigIntegerField("Original Size (bytes)", default=0, editable=False)

    # ---- matching ---------------------------------------------------------
    client = models.ForeignKey(
        "clients.Client",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="emails",
        verbose_name="Client",
    )
    project = models.ForeignKey(
        "projects.Project",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="emails",
        verbose_name="Project",
    )
    contacts = models.ManyToManyField(
        "clients.Contact",
        blank=True,
        related_name="emails",
        verbose_name="Contacts",
        help_text="Contacts recognised among the sender and recipients.",
    )
    match_method = models.CharField(
        "Matched By",
        max_length=20,
        choices=MatchMethod,
        default=MatchMethod.UNMATCHED,
    )
    needs_review = models.BooleanField(
        "Needs Review",
        default=False,
        help_text="The match is a guess or the clues disagreed; confirm or correct it.",
    )
    match_note = models.CharField(
        "Why",
        max_length=500,
        blank=True,
        help_text="What the matching rules found, and why the match needs review.",
    )
    matched_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="Assigned By",
        editable=False,
        help_text="Set when the client or project is assigned by hand.",
    )

    activities = GenericRelation("communications.Activity", related_query_name="email")

    class Meta:
        verbose_name = "Email"
        verbose_name_plural = "Inbox & Sent Mail"
        ordering = ["-sent_at", "-pk"]
        indexes = [
            models.Index(fields=["client", "-sent_at"], name="email_client_sent"),
            models.Index(fields=["project", "-sent_at"], name="email_project_sent"),
            models.Index(fields=["match_method", "needs_review"], name="email_match_review"),
        ]

    def __str__(self):
        return self.subject or "(no subject)"

    # ---- read-only helpers ------------------------------------------------

    @property
    def is_matched(self):
        return self.client_id is not None

    @property
    def is_inbound(self):
        return self.direction == Direction.INBOUND

    @property
    def snippet(self):
        text = re.sub(r"\s+", " ", self.body_text).strip()
        return text if len(text) <= 160 else text[:157].rstrip() + "..."

    @property
    def participants(self):
        """Every address on the message, sender first, lowercased, no repeats."""
        entries = [{"email": self.from_address}]
        entries += self.to_addresses + self.cc_addresses + self.bcc_addresses
        seen = []
        for entry in entries:
            address = (entry.get("email") or "").strip().lower()
            if address and address not in seen:
                seen.append(address)
        return seen

    @property
    def activity_kind(self):
        return ActivityKind.EMAIL_IN if self.is_inbound else ActivityKind.EMAIL_OUT

    # ---- validation and saving -------------------------------------------

    def clean(self):
        super().clean()
        if self.project_id and not self.client_id:
            self.client_id = self.project.client_id
        if self.project_id and self.client_id and self.project.client_id != self.client_id:
            raise ValidationError({"project": "This project belongs to a different client."})
        if self.client_id and self.match_method == MatchMethod.UNMATCHED:
            raise ValidationError(
                {"match_method": "A message with a client must say how it was matched."}
            )
        if not self.client_id and self.match_method != MatchMethod.UNMATCHED:
            raise ValidationError({"client": "Choose the client this message was matched to."})

    def save(self, *args, **kwargs):
        if self.project_id and not self.client_id:
            self.client_id = self.project.client_id
        if self.match_method == MatchMethod.UNMATCHED:
            self.needs_review = False
        if self.raw_file and not self.raw_size:
            self.raw_size = self.raw_file.size
        super().save(*args, **kwargs)
        self.thread.refresh_bounds()
        self.sync_parties()
        self.sync_activity()

    def assign(self, client, project=None, user=None):
        """Tie the message to a client (and project) by hand.

        Manual assignment is final: it clears the review flag and is never
        overridden by automatic matching.
        """
        self.client = client
        self.project = project
        self.match_method = MatchMethod.MANUAL
        self.matched_by = user
        self.needs_review = False
        self.match_note = f"Assigned by {user}" if user else "Assigned by hand"
        self.full_clean()
        self.save()
        return self

    def sync_activity(self):
        """Keep the timeline entry in step with the match.

        A matched message has exactly one activity row, on the client (and
        project) it is matched to; an unmatched one has none. Called from
        save(), so reassigning a message moves its timeline entry. A stale
        match still has a client, so its entry stays, flagged for review.
        """
        if not self.client_id:
            self.activities.all().delete()
            return
        activity = self.activities.first() or Activity(subject=self)
        activity.kind = self.activity_kind
        activity.client_id = self.client_id
        activity.project_id = self.project_id
        activity.summary = (self.subject or "(no subject)")[:255]
        activity.details = self.snippet
        activity.happened_at = self.sent_at
        activity.needs_review = self.needs_review
        activity.note = self.match_note if self.needs_review else ""
        activity.save()

    def sync_parties(self):
        """Mirror the address lists into EmailParty rows for querying.

        The JSON lists are what is displayed; the rows are what "every
        email involving this address" is answered from.
        """
        wanted = {}
        for role, entries in (
            (EmailParty.Role.FROM, [{"name": self.from_name, "email": self.from_address}]),
            (EmailParty.Role.TO, self.to_addresses),
            (EmailParty.Role.CC, self.cc_addresses),
            (EmailParty.Role.BCC, self.bcc_addresses),
        ):
            for entry in entries:
                address = normalize_email(entry.get("email"))
                if address:
                    wanted.setdefault((address, role), entry.get("name") or "")
        existing = {(p.address, p.role): p for p in self.parties.all()}
        for key, party in existing.items():
            if key not in wanted:
                party.delete()
        for (address, role), name in wanted.items():
            party = existing.get((address, role))
            if party is None:
                EmailParty.objects.create(message=self, address=address, role=role, name=name)
            elif party.name != name:
                party.name = name
                party.save(update_fields=["name"])


class EmailParty(models.Model):
    """One address on one message - the queryable form of the headers."""

    class Role(models.TextChoices):
        FROM = "from", "From"
        TO = "to", "To"
        CC = "cc", "Cc"
        BCC = "bcc", "Bcc"

    message = models.ForeignKey(
        EmailMessage, on_delete=models.CASCADE, related_name="parties", verbose_name="Email"
    )
    address = models.CharField("Address", max_length=254, db_index=True)
    name = models.CharField("Name", max_length=255, blank=True)
    role = models.CharField("Role", max_length=4, choices=Role)

    class Meta:
        verbose_name = "Email Party"
        verbose_name_plural = "Email Parties"
        constraints = [
            models.UniqueConstraint(
                fields=["message", "address", "role"], name="unique_party_per_message_role"
            )
        ]

    def __str__(self):
        return f"{self.get_role_display()}: {self.address}"


class EmailAttachment(models.Model):
    """A file that came with a message, kept in the bucket.

    Inline images (referenced from the HTML as cid:...) are attachments
    too, flagged so lists can leave them out.
    """

    message = models.ForeignKey(
        EmailMessage,
        on_delete=models.CASCADE,
        related_name="attachments",
        verbose_name="Email",
    )
    file = models.FileField("File", upload_to="email/attachments/%Y/%m/")
    filename = models.CharField("File Name", max_length=255)
    content_type = models.CharField("Content Type", max_length=100, blank=True)
    size = models.PositiveBigIntegerField("Size (bytes)", default=0, editable=False)
    checksum_sha256 = models.CharField("SHA-256", max_length=64, blank=True, editable=False)
    content_id = models.CharField(
        "Content-ID", max_length=255, blank=True, help_text="Set for inline images."
    )
    is_inline = models.BooleanField("Inline", default=False)

    class Meta:
        verbose_name = "Attachment"
        verbose_name_plural = "Attachments"
        ordering = ["is_inline", "filename"]

    def __str__(self):
        return self.filename

    def compute_checksum(self):
        """SHA-256 and size of the stored bytes, read through the storage API."""
        digest = hashlib.sha256()
        size = 0
        with self.file.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
                size += len(chunk)
        self.checksum_sha256 = digest.hexdigest()
        self.size = size
        return self.checksum_sha256

    def save(self, *args, **kwargs):
        if self.file and not self.checksum_sha256:
            self.compute_checksum()
        super().save(*args, **kwargs)


class Activity(models.Model):
    """One line on a client's (and project's) timeline.

    Rows point at the thing that happened - an email, a quote, a document -
    through a generic link, so the timeline can list every kind together.
    The link is informational: an activity outlives the quote or document
    it describes ("quote Q-2026-0003 sent" stays true after the quote is
    deleted). Email activities are the exception: they are owned by the
    message and go with it.
    """

    kind = models.CharField("Kind", max_length=30, choices=ActivityKind)
    client = models.ForeignKey(
        "clients.Client",
        on_delete=models.CASCADE,
        related_name="activities",
        verbose_name="Client",
    )
    project = models.ForeignKey(
        "projects.Project",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="activities",
        verbose_name="Project",
    )

    content_type = models.ForeignKey(
        ContentType, on_delete=models.SET_NULL, null=True, blank=True, editable=False
    )
    object_id = models.PositiveBigIntegerField(null=True, blank=True, editable=False)
    subject = GenericForeignKey("content_type", "object_id")

    summary = models.CharField("Summary", max_length=255)
    details = models.TextField("Details", blank=True)
    happened_at = models.DateTimeField("When", default=timezone.now, db_index=True)
    needs_review = models.BooleanField(
        "Needs Review",
        default=False,
        help_text="Mirrors the email it stands for: its place on this timeline is in doubt.",
    )
    note = models.CharField("Note", max_length=500, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="Recorded By",
        editable=False,
    )
    created_at = models.DateTimeField("Recorded At", auto_now_add=True)

    class Meta:
        verbose_name = "Timeline Entry"
        verbose_name_plural = "Timeline"
        ordering = ["-happened_at", "-pk"]
        indexes = [
            models.Index(fields=["client", "-happened_at"], name="activity_client_when"),
            models.Index(fields=["project", "-happened_at"], name="activity_project_when"),
            models.Index(fields=["content_type", "object_id"], name="activity_subject"),
        ]

    def __str__(self):
        return f"{self.get_kind_display()}: {self.summary}"

    @property
    def is_email(self):
        return self.kind in EMAIL_KINDS

    @property
    def is_editable(self):
        """Only notes are written by people; everything else mirrors a record."""
        return self.kind == ActivityKind.NOTE

    def clean(self):
        super().clean()
        if self.project_id and not self.client_id:
            self.client_id = self.project.client_id
        if self.project_id and self.client_id and self.project.client_id != self.client_id:
            raise ValidationError({"project": "This project belongs to a different client."})

    def save(self, *args, **kwargs):
        if self.project_id and not self.client_id:
            self.client_id = self.project.client_id
        super().save(*args, **kwargs)


class OutgoingEmail(models.Model):
    """An email written in the CRM, from draft until it is sent.

    Addresses are typed as in a mail program ("Anna <anna@acme.com>,
    boss@acme.com"). When it has been sent, the exact message is logged
    as an EmailMessage like any other mail (sent_message), so it threads,
    shows on the timeline and is recognised if the Sent folder is ever
    read too.
    """

    client = models.ForeignKey(
        "clients.Client", on_delete=models.CASCADE, null=True, blank=True,
        related_name="outgoing_emails", verbose_name="Client",
    )
    project = models.ForeignKey(
        "projects.Project", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="outgoing_emails", verbose_name="Project",
        help_text="Its number is added to the subject, so replies are matched to it.",
    )
    reply_to = models.ForeignKey(
        EmailMessage, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="replies", verbose_name="Reply To",
    )

    to = models.TextField("To", blank=True, help_text="Separate addresses with commas.")
    cc = models.TextField("Cc", blank=True)
    bcc = models.TextField("Bcc", blank=True)
    subject = models.CharField("Subject", max_length=998, blank=True)
    body_text = models.TextField("Message", blank=True)

    status = models.CharField(
        "Status", max_length=10, choices=OutgoingStatus, default=OutgoingStatus.DRAFT
    )
    message_id = models.CharField(
        "Message-ID", max_length=255, blank=True, editable=False,
        help_text="Fixed when the email is queued, so a retry is still the same email.",
    )
    attempts = models.PositiveSmallIntegerField("Attempts", default=0, editable=False)
    last_error = models.TextField("Last Error", blank=True, editable=False)
    queued_at = models.DateTimeField("Queued At", null=True, blank=True, editable=False)
    sent_at = models.DateTimeField("Sent At", null=True, blank=True, editable=False)
    sent_message = models.OneToOneField(
        EmailMessage, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="outgoing", verbose_name="Logged Copy", editable=False,
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name="Written By", editable=False,
    )
    sent_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name="Sent By", editable=False,
    )
    created_at = models.DateTimeField("Created At", auto_now_add=True)
    updated_at = models.DateTimeField("Updated At", auto_now=True)

    class Meta:
        verbose_name = "Email to Send"
        verbose_name_plural = "Write Email"
        ordering = ["-created_at", "-pk"]

    def __str__(self):
        return self.subject or "(no subject)"

    @property
    def is_editable(self):
        return self.status == OutgoingStatus.DRAFT

    def clean(self):
        super().clean()
        if self.project_id and not self.client_id:
            self.client_id = self.project.client_id
        if self.project_id and self.client_id and self.project.client_id != self.client_id:
            raise ValidationError({"project": "This project belongs to a different client."})

    def save(self, *args, **kwargs):
        if self.project_id and not self.client_id:
            self.client_id = self.project.client_id
        super().save(*args, **kwargs)


def outgoing_upload_to(instance, filename):
    """email/outgoing/<YYYY>/<MM>/<random>/<file name>.

    A folder per file: the bucket is versioned, so two drafts attaching a
    "brief.pdf" in the same month must not become two versions of one key.
    """
    name = Path(filename.replace("\\", "/")).name.lstrip(".") or "attachment"
    return f"email/outgoing/{timezone.now():%Y/%m}/{uuid.uuid4().hex[:12]}/{get_valid_filename(name)}"


class OutgoingAttachment(models.Model):
    """A file sent with an OutgoingEmail: uploaded, or copied from a quote or
    document when the email was started from there."""

    email = models.ForeignKey(
        OutgoingEmail, on_delete=models.CASCADE, related_name="attachments", verbose_name="Email"
    )
    file = models.FileField("File", upload_to=outgoing_upload_to)
    filename = models.CharField("File Name", max_length=255, blank=True,
                                help_text="Defaults to the uploaded file's name.")

    class Meta:
        verbose_name = "Attachment"
        verbose_name_plural = "Attachments"
        ordering = ["pk"]

    def __str__(self):
        return self.display_name

    @property
    def display_name(self):
        return self.filename or (self.file.name.rsplit("/", 1)[-1] if self.file else "")


class MailSyncRun(models.Model):
    """One run of the mail sync: what it read, what it logged, what failed.

    Also the lock: a run that started recently and has not finished means
    another sync is busy, and a second one stops instead of reading the
    same mail twice in parallel.
    """

    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        DONE = "done", "Done"
        FAILED = "failed", "Failed"
        SKIPPED = "skipped", "Skipped (another sync was running)"

    source = models.CharField("Source", max_length=100)
    status = models.CharField("Status", max_length=10, choices=Status, default=Status.RUNNING)
    started_at = models.DateTimeField("Started", auto_now_add=True, db_index=True)
    finished_at = models.DateTimeField("Finished", null=True, blank=True)
    fetched = models.PositiveIntegerField("Read", default=0)
    logged = models.PositiveIntegerField("Logged", default=0)
    duplicates = models.PositiveIntegerField("Already Logged", default=0)
    matched = models.PositiveIntegerField("Matched", default=0)
    failed = models.PositiveIntegerField("Failed", default=0)
    errors = models.TextField("Errors", blank=True)

    class Meta:
        verbose_name = "Mail Sync"
        verbose_name_plural = "Mail Sync Log"
        ordering = ["-started_at", "-pk"]

    def __str__(self):
        return f"{self.source} {self.started_at:%Y-%m-%d %H:%M} ({self.get_status_display()})"

    @property
    def summary(self):
        text = (f"{self.fetched} read, {self.logged} logged ({self.matched} matched), "
                f"{self.duplicates} already logged, {self.failed} failed")
        return text if self.status != self.Status.SKIPPED else self.get_status_display()


class MailboxState(models.Model):
    """How far a mailbox folder has been read (IMAP): the last UID seen.

    UIDVALIDITY changes when the server renumbers the folder; the sync
    then starts over, and Message-ID de-duplication keeps that harmless.
    """

    source = models.CharField("Source", max_length=100)
    folder = models.CharField("Folder", max_length=255)
    uidvalidity = models.PositiveBigIntegerField("UIDVALIDITY", default=0)
    last_uid = models.PositiveBigIntegerField("Last UID", default=0)
    updated_at = models.DateTimeField("Updated", auto_now=True)

    class Meta:
        verbose_name = "Mailbox Position"
        verbose_name_plural = "Mailbox Positions"
        constraints = [
            models.UniqueConstraint(fields=["source", "folder"], name="unique_mailbox_folder")
        ]

    def __str__(self):
        return f"{self.source}:{self.folder} @ {self.last_uid}"


__all__ = [
    "OutgoingAttachment",
    "OutgoingEmail",
    "MailSyncRun",
    "MailboxState",
    "Activity",
    "EmailParty",
    "CONFIDENT_METHODS",
    "EmailAttachment",
    "EmailMessage",
    "EmailThread",
    "normalize_subject",
]
