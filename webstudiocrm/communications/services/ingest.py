"""Logging one raw email: parse, de-duplicate, thread, store, match.

    raw bytes -> parse_email() -> already logged? -> find_thread()
              -> EmailMessage + .eml + attachments in the bucket
              -> match_message()

Everything for one message happens in one transaction. Files in the
bucket are not covered by the transaction, so on failure the ones written
so far are deleted by hand, the same way the quote PDF is.

Idempotent: the Message-ID (or, when missing, a hash of the bytes) is
unique, so feeding the same mail twice logs it once. That is what makes
re-running a sync, or re-copying a file into the folder, safe.
"""

import hashlib
from pathlib import Path
from dataclasses import dataclass
from datetime import timedelta

from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.text import get_valid_filename

from core.emails import normalize_email
from core.models import StudioProfile, StudioSignatory

from ..choices import Direction
from ..models import EmailAttachment, EmailMessage, EmailThread, normalize_subject
from .matching import match_message
from .parsing import parse_email

#: How far apart two messages with the same subject can be and still be
#: grouped when the reply headers are missing.
SUBJECT_THREAD_WINDOW = timedelta(days=30)


@dataclass
class IngestResult:
    message: EmailMessage
    created: bool

    @property
    def matched(self):
        return self.created and self.message.is_matched


def studio_addresses():
    profile_email = normalize_email(StudioProfile.get_solo().email)
    addresses = {normalize_email(e) for e in StudioSignatory.objects.values_list("email", flat=True)}
    if profile_email:
        addresses.add(profile_email)
    return addresses


def guess_direction(parsed):
    """Sent if the studio wrote it, received otherwise."""
    return Direction.OUTBOUND if parsed.from_address in studio_addresses() else Direction.INBOUND


def find_thread(parsed, sent_at):
    """The conversation the message belongs to, or a new one.

    1. A logged message it replies to (In-Reply-To, then References).
    2. A logged message that replies to it - mail can arrive out of order.
    3. Same subject, a shared outside party, within SUBJECT_THREAD_WINDOW:
       for clients whose mail program drops the reply headers.
    """
    ancestors = [parsed.in_reply_to, *reversed(parsed.references)]
    ancestors = [a for a in dict.fromkeys(ancestors) if a]
    if ancestors:
        by_id = {
            m.message_id: m.thread
            for m in EmailMessage.objects.filter(message_id__in=ancestors).select_related("thread")
        }
        for message_id in ancestors:
            if message_id in by_id:
                return by_id[message_id]

    child = EmailMessage.objects.filter(in_reply_to=parsed.message_id).select_related("thread").first()
    if child:
        return child.thread

    subject = normalize_subject(parsed.subject)
    outside = set(parsed.addresses) - studio_addresses()
    if subject and outside:
        candidate = (
            EmailThread.objects.filter(
                subject__iexact=subject,
                last_message_at__gte=sent_at - SUBJECT_THREAD_WINDOW,
                last_message_at__lte=sent_at + SUBJECT_THREAD_WINDOW,
                messages__parties__address__in=outside,
            )
            .order_by("-last_message_at")
            .first()
        )
        if candidate:
            return candidate

    return EmailThread.objects.create(subject=subject[:998])


def _store_message(parsed, direction, sent_at):
    thread = find_thread(parsed, sent_at)
    message = EmailMessage(
        thread=thread,
        message_id=parsed.message_id,
        in_reply_to=parsed.in_reply_to,
        references=parsed.references,
        direction=direction,
        from_name=parsed.from_name,
        from_address=parsed.from_address,
        to_addresses=parsed.to,
        cc_addresses=parsed.cc,
        bcc_addresses=parsed.bcc,
        subject=parsed.subject,
        body_text=parsed.body_text,
        body_html=parsed.body_html,
        sent_at=sent_at,
    )
    digest = hashlib.sha256(parsed.raw).hexdigest()[:16]
    message.raw_file.save(f"{digest}.eml", ContentFile(parsed.raw), save=False)
    return message


def attachment_key(raw, position, filename):
    """The bucket name for one attachment: <message hash>/<n>/<file name>.

    The bucket is versioned, so two attachments saved under the same key
    become two versions of one object, and the older email would serve
    the newer file. Clients reuse names constantly (image001.png,
    brief.pdf), so every attachment gets its own folder: the message's
    hash, then its position in the message. The last part stays the real
    file name, which is what a download is saved as.

    The name comes from the sender and is not trusted: path parts and
    characters storage keys cannot hold are removed.
    """
    digest = hashlib.sha256(raw).hexdigest()[:16]
    name = Path(filename.replace("\\", "/")).name.lstrip(".")  # no "..", no hidden files
    name = get_valid_filename(name) if name.strip() else "attachment"
    return f"{digest}/{position}/{name}"


def _already_logged(message_id):
    return EmailMessage.objects.filter(message_id=message_id).first()


def ingest(raw, direction=None, fallback_time=None):
    """Log one email from raw bytes; returns IngestResult.

    `direction` comes from the source (the folder or mailbox it was in);
    without it, mail from the studio's own addresses counts as sent.
    `fallback_time` is used when the Date header is missing or broken.
    ParseError propagates: the source decides what to do with bad input.
    """
    parsed = parse_email(raw)
    existing = _already_logged(parsed.message_id)
    if existing:
        return IngestResult(existing, created=False)

    direction = direction or guess_direction(parsed)
    sent_at = parsed.sent_at or fallback_time or timezone.now()
    written = []
    try:
        with transaction.atomic():
            message = _store_message(parsed, direction, sent_at)
            written.append(message.raw_file)
            message.save()
            for position, item in enumerate(parsed.attachments, start=1):
                attachment = EmailAttachment(
                    message=message,
                    filename=item.filename,
                    content_type=item.content_type[:100],
                    content_id=item.content_id[:255],
                    is_inline=item.is_inline,
                )
                attachment.file.save(
                    attachment_key(parsed.raw, position, item.filename),
                    ContentFile(item.content),
                    save=False,
                )
                written.append(attachment.file)
                attachment.save()
            match_message(message)
    except Exception as error:
        for stored in written:
            stored.delete(save=False)  # the transaction can't undo the bucket
        if isinstance(error, IntegrityError):
            # Logged by another process between the check and the insert
            existing = _already_logged(parsed.message_id)
            if existing:
                return IngestResult(existing, created=False)
        raise
    return IngestResult(message, created=True)
