"""Status and classification values for documents.

Kept out of models.py so the workflow service can import them without
pulling in the models (and the circular import that would come with it).
"""

from django.db import models


class DocumentStatus(models.TextChoices):
    """Where a document is in its life.

        draft -> out for signature -> partially signed -> signed -> active
                                   -> declined
        active -> expired / terminated
        any signed state -> superseded (replaced by a later version)

    DRAFT is the only editable state. SIGNED means fully executed but not yet
    in force; ACTIVE means executed and in force today.
    """

    DRAFT = "draft", "Draft"
    OUT_FOR_SIGNATURE = "out_for_signature", "Out for Signature"
    PARTIALLY_SIGNED = "partially_signed", "Partially Signed"
    SIGNED = "signed", "Signed"
    ACTIVE = "active", "Active"
    DECLINED = "declined", "Declined"
    EXPIRED = "expired", "Expired"
    TERMINATED = "terminated", "Terminated"
    SUPERSEDED = "superseded", "Superseded"


#: Statuses that mean every required signature is in place. A document that
#: reached any of these was executed, whatever happened to it afterwards.
EXECUTED_STATUSES = frozenset(
    {
        DocumentStatus.SIGNED,
        DocumentStatus.ACTIVE,
        DocumentStatus.EXPIRED,
        DocumentStatus.TERMINATED,
        DocumentStatus.SUPERSEDED,
    }
)

#: Statuses where the document is executed and still governs the relationship.
IN_FORCE_STATUSES = frozenset({DocumentStatus.SIGNED, DocumentStatus.ACTIVE})

#: Statuses where the document is finished and nothing more will happen to it.
CLOSED_STATUSES = frozenset(
    {
        DocumentStatus.DECLINED,
        DocumentStatus.EXPIRED,
        DocumentStatus.TERMINATED,
        DocumentStatus.SUPERSEDED,
    }
)


class SignerStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    VIEWED = "viewed", "Viewed"
    SIGNED = "signed", "Signed"
    DECLINED = "declined", "Declined"


class SignatureProvider(models.TextChoices):
    """How signatures are collected.

    MANUAL covers wet ink and scanned copies, and is also how pre-existing
    contracts are brought into the CRM.
    """

    MANUAL = "manual", "Manual / wet ink"
    DOCUMENSO = "documenso", "Documenso"
    DROPBOX_SIGN = "dropbox_sign", "Dropbox Sign"


class SignatureRequestStatus(models.TextChoices):
    SENT = "sent", "Sent"
    COMPLETED = "completed", "Completed"
    DECLINED = "declined", "Declined"
    VOIDED = "voided", "Voided"
    ERROR = "error", "Error"
