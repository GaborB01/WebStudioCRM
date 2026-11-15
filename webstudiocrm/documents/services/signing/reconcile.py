"""Bring a document in line with what the provider reports.

The single place where provider state turns into workflow transitions, used
by both the webhook endpoint and the polling command - so a missed webhook
is repaired by the next poll with no difference in outcome.

Idempotent by construction: providers report absolute state (who has signed),
not deltas, and each signer is only acted on when the CRM disagrees. Webhooks
may be delivered twice, out of order, or alongside a poll; replaying them
changes nothing.
"""

import logging

from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone

from ...choices import DocumentStatus, SignatureRequestStatus
from ..workflow import DocumentError, DocumentService
from .base import ProviderError

logger = logging.getLogger(__name__)

#: Keep the audit trail from growing without bound on a chatty provider.
MAX_LOGGED_EVENTS = 200


@transaction.atomic
def reconcile(signature_request, state, provider=None, raw_event=None):
    """Apply a ProviderState to its document. Returns a list of what changed."""
    from . import get_provider

    document = signature_request.document
    provider = provider or get_provider(signature_request.provider)
    changes = []

    if raw_event is not None:
        _log_event(signature_request, raw_event)

    if state.envelope_id and not signature_request.provider_envelope_id:
        signature_request.provider_envelope_id = state.envelope_id
        signature_request.save(update_fields=["provider_envelope_id"])

    signers = {s.email.lower(): s for s in document.signers.all()}

    # Declines first: one refusal ends the round, so recording signatures
    # afterwards would be misleading.
    for recipient in state.recipients:
        signer = signers.get(recipient.email.lower())
        if signer is None or not recipient.declined or signer.status == "declined":
            continue
        try:
            DocumentService.record_decline(signer, reason=recipient.decline_reason)
            changes.append(f"{signer.name} declined")
        except DocumentError as error:
            logger.info("Ignoring decline for %s: %s", signer.email, error)
        return _finish(signature_request, state, provider, changes)

    for recipient in sorted(state.recipients, key=lambda r: r.signed_at or timezone.now()):
        signer = signers.get(recipient.email.lower())
        if signer is None or not recipient.signed or signer.has_signed:
            continue
        try:
            DocumentService.record_signature(signer, signed_at=recipient.signed_at)
            changes.append(f"{signer.name} signed")
        except DocumentError as error:
            # Already signed, or the document moved on - nothing to repair
            logger.info("Ignoring signature for %s: %s", signer.email, error)

    return _finish(signature_request, state, provider, changes)


def _finish(signature_request, state, provider, changes):
    document = signature_request.document
    document.refresh_from_db()

    if state.voided and signature_request.is_open:
        signature_request.status = SignatureRequestStatus.VOIDED
        signature_request.completed_at = timezone.now()
        signature_request.save(update_fields=["status", "completed_at"])
        changes.append("send-out voided")

    if state.completed:
        # A provider can report completion before we have seen every
        # signature event; close the document out either way.
        if document.status not in (
            DocumentStatus.SIGNED,
            DocumentStatus.ACTIVE,
        ) and document.status != DocumentStatus.DECLINED:
            try:
                DocumentService.mark_signed_manually(document)
                changes.append("document completed")
            except DocumentError as error:
                logger.info("Could not complete %s: %s", document.reference, error)
        changes += _store_provider_files(signature_request, provider)

    return changes


def _store_provider_files(signature_request, provider):
    """Fetch the signed PDF and the signing certificate, once."""
    from ...models import DocumentFile

    document = signature_request.document
    stored = []
    wanted = (
        (DocumentFile.Kind.EXECUTED, provider.download_executed, "Signed copy"),
        (
            DocumentFile.Kind.AUDIT_TRAIL,
            provider.download_certificate,
            "Signature certificate",
        ),
    )
    for kind, download, description in wanted:
        if document.files.filter(kind=kind).exists():
            continue
        try:
            result = download(signature_request)
        except ProviderError as error:
            logger.warning("Could not download %s for %s: %s", kind, document.reference, error)
            continue
        if not result:
            continue
        content, filename = result
        DocumentService.attach_file(
            document, kind, ContentFile(content), filename, description=description
        )
        stored.append(f"stored {kind}")
    return stored


def _log_event(signature_request, raw_event):
    log = list(signature_request.event_log or [])
    log.append({"received_at": timezone.now().isoformat(), **raw_event})
    signature_request.event_log = log[-MAX_LOGGED_EVENTS:]
    signature_request.save(update_fields=["event_log"])
