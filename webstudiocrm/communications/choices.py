"""Classification values for logged email and the activity timeline.

Kept out of models.py so services can import them without the models.
"""

from django.db import models


class Direction(models.TextChoices):
    INBOUND = "in", "Received"
    OUTBOUND = "out", "Sent"


class MatchMethod(models.TextChoices):
    """How a message was tied to a client, strongest first.

    The order is the order the matching rules run in; the first rule with
    an answer wins. REFERENCE and THREAD name a project directly; CONTACT
    and DOMAIN only name a client, and the project is filled in when the
    client has exactly one open project.

    STALE is what a matched message becomes when the evidence for its
    match is gone (the contact was deleted) and nothing else matches: it
    keeps its client so the history stays, and waits for a person.
    """

    REFERENCE = "reference", "Reference number in the email"
    THREAD = "thread", "Reply to a matched email"
    CONTACT = "contact", "Known contact address"
    DOMAIN = "domain", "Client's email domain"
    MANUAL = "manual", "Assigned by hand"
    STALE = "stale", "Earlier match no longer holds"
    UNMATCHED = "unmatched", "Not matched"


#: Methods that identify the client with certainty. The others are
#: inferences, and the message is flagged for a human to confirm.
CONFIDENT_METHODS = frozenset(
    {MatchMethod.REFERENCE, MatchMethod.THREAD, MatchMethod.CONTACT, MatchMethod.MANUAL}
)


class ActivityKind(models.TextChoices):
    EMAIL_IN = "email_in", "Email received"
    EMAIL_OUT = "email_out", "Email sent"
    QUOTE_SENT = "quote_sent", "Quote sent"
    QUOTE_ACCEPTED = "quote_accepted", "Quote accepted"
    QUOTE_REJECTED = "quote_rejected", "Quote rejected"
    DOCUMENT_SENT = "document_sent", "Document sent for signature"
    DOCUMENT_SIGNED = "document_signed", "Document signed"
    DOCUMENT_TERMINATED = "document_terminated", "Document terminated"
    NOTE = "note", "Note"


EMAIL_KINDS = frozenset({ActivityKind.EMAIL_IN, ActivityKind.EMAIL_OUT})


class OutgoingStatus(models.TextChoices):
    """Where an email written in the CRM is.

        draft --send--> queued --worker--> sending --> sent
                                                   --> failed --send again--> queued

    Only drafts can be edited. SENDING is held while the mail server is
    being talked to; a job that finds a message already SENDING was
    interrupted and marks it FAILED rather than risk a second copy.
    """

    DRAFT = "draft", "Draft"
    QUEUED = "queued", "Queued"
    SENDING = "sending", "Sending"
    SENT = "sent", "Sent"
    FAILED = "failed", "Failed"
