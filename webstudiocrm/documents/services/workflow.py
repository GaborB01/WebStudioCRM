"""Document lifecycle. The only code that changes a document's status, and
the only place where documents create Tasks or touch Projects.

    draft --send_for_signature--> out for signature
                                   |--(each signature)--> partially signed
                                   |--(last signature)--> signed --> active
                                   |--(a signer declines)--> declined

    draft --mark_signed_manually--> signed      (wet ink, and backfilling
                                                 contracts signed long ago)

    signed/active --(term reached)--> expired, or renewed if it auto-renews
    signed/active --terminate------> terminated
    executed/declined --amend------> new draft (version + 1); sending that
                                     draft supersedes the previous version

Every transition is a single transaction: if any step fails, nothing moved -
most importantly the document number is not used up.
"""

import calendar
from datetime import timedelta

from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone

from core.models import DocumentSequence, StudioProfile, StudioSignatory
from tasks.models import Task

from ..choices import (
    EXECUTED_STATUSES,
    IN_FORCE_STATUSES,
    DocumentStatus,
    SignatureProvider,
    SignatureRequestStatus,
    SignerStatus,
)
from .snapshots import client_snapshot, studio_snapshot

#: Fallback term for an auto-renewing document whose type has no default term.
FALLBACK_RENEWAL_MONTHS = 12


class DocumentError(Exception):
    """A workflow step is not allowed; the message is shown to the user."""


def add_months(date, months):
    """Same day-of-month `months` later, clamped to the end of short months."""
    index = date.month - 1 + months
    year = date.year + index // 12
    month = index % 12 + 1
    return date.replace(
        year=year, month=month, day=min(date.day, calendar.monthrange(year, month)[1])
    )


def _models():
    from documents import models

    return models


class DocumentService:
    # ---- creating and editing drafts ---------------------------------------

    @staticmethod
    def apply_defaults(document):
        """Fill a new draft from its document type and client."""
        document_type = document.document_type
        if document_type:
            document.renewal_notice_days = document_type.default_renewal_notice_days
            document.title = document.title or f"{document_type.name} - {document.client.name}"
        return document

    @classmethod
    def create_for_client(cls, client, document_type, user=None, **fields):
        Document = _models().Document
        document = Document(
            client=client, document_type=document_type, created_by=user, **fields
        )
        cls.apply_defaults(document)
        document.full_clean()
        document.save()
        cls.ensure_studio_signer(document)
        return document

    @classmethod
    def ensure_studio_signer(cls, document):
        """Add the studio's default signatory to a draft that needs one.

        Client contacts are picked per document, but the studio's own
        signatory is the same person every time - so a countersigned type
        gets them from Core -> Studio Profile instead of retyping. Returns
        the signer added, or None when nothing was missing or no signatory
        is configured.
        """
        if (
            not document.is_editable
            or not document.document_type.requires_countersignature
            or document.signers.filter(is_studio_side=True).exists()
        ):
            return None
        signatory = StudioSignatory.get_default()
        if signatory is None or document.signers.filter(email=signatory.email).exists():
            return None
        last_order = max(
            (s.signing_order for s in document.signers.all()), default=1
        )
        return document.signers.create(
            name=signatory.name,
            email=signatory.email,
            is_studio_side=True,
            # The studio countersigns, so it goes after the client's signers
            signing_order=last_order + 1,
        )

    @classmethod
    def add_signer(cls, document, **fields):
        cls._require_editable(document, "Signers can only be changed on a draft.")
        signer = document.signers.model(document=document, **fields)
        signer.full_clean()
        signer.save()
        return signer

    @classmethod
    def attach_file(cls, document, kind, content, filename, user=None, description=""):
        """Store a file against the document (draft, executed copy, annex...)."""
        DocumentFile = _models().DocumentFile
        stored = DocumentFile(
            document=document, kind=kind, description=description, uploaded_by=user
        )
        stored.file.save(filename, content, save=False)
        stored.save()
        return stored

    # ---- sending for signature ---------------------------------------------

    @classmethod
    def send_problems(cls, document):
        """Reasons the document can't go out yet (empty list = ready)."""
        if not document.is_editable:
            return ["Only draft documents can be sent for signature."]

        problems = []
        if not StudioProfile.get_solo().company_name:
            problems.append("Fill in the company name in Core -> Studio Profile.")

        signers = list(document.signers.all())
        if not signers:
            problems.append("Add at least one signer.")
        elif not any(not s.is_studio_side for s in signers):
            problems.append("Add at least one signer on the client's side.")
        if document.document_type.requires_countersignature and not any(
            s.is_studio_side for s in signers
        ):
            if StudioSignatory.get_default():
                # Configured but missing here (e.g. the row was deleted);
                # saving the document adds it back.
                problems.append(
                    f"A {document.document_type.name} is countersigned by the "
                    "studio: save the document to add your signatory."
                )
            else:
                problems.append(
                    f"A {document.document_type.name} is countersigned by the "
                    "studio: add your signatory in Core -> Studio Profile."
                )
        if not document.latest_draft_file:
            problems.append("Upload the draft document to be signed.")
        return problems

    @classmethod
    @transaction.atomic
    def send_for_signature(cls, document, user=None, provider=None):
        """Number the document, freeze the parties, copy the file that goes
        out, open a signature request and raise a chase-up task."""
        models = _models()
        # Lock the row so a double click can't send the document twice
        models.Document.objects.select_for_update().filter(pk=document.pk).exists()
        document.refresh_from_db()

        problems = cls.send_problems(document)
        if problems:
            raise DocumentError(" ".join(problems))

        today = timezone.localdate()
        if document.revision_of_id and document.revision_of.number:
            base = document.revision_of.number.split("-v")[0]
            document.number = f"{base}-v{document.version}"
        else:
            document.number = DocumentSequence.next_reference(
                document.document_type.prefix, today.year
            )
        document.client_snapshot = client_snapshot(document)
        document.studio_snapshot = studio_snapshot(StudioProfile.get_solo())
        document.status = DocumentStatus.OUT_FOR_SIGNATURE
        document.sent_at = timezone.now()

        # The previous version can no longer be signed
        previous = document.revision_of
        if previous and previous.status not in (
            DocumentStatus.SUPERSEDED,
            DocumentStatus.TERMINATED,
        ):
            previous.status = DocumentStatus.SUPERSEDED
            previous.save(update_fields=["status", "updated_at"])
            cls._close_task(previous.signature_task)

        document.signature_task = Task.objects.create(
            title=f"Chase signature on {document.number}",
            project=document.project,
            client=document.client,
            priority=Task.Priority.HIGH,
            due_date=today + timedelta(days=7),
        )

        # Freeze the exact file the client received, so later draft uploads
        # can never blur what was actually sent.
        draft = document.latest_draft_file
        sent = cls._copy_as_sent(document, draft, user)
        try:
            document.save()
            cls._dispatch_to_provider(document, provider)
        except Exception:
            if sent:
                sent.file.delete(save=False)  # the transaction can't undo the file
            raise
        return document

    @classmethod
    def _dispatch_to_provider(cls, document, provider_name):
        """Open the send-out with the e-signature provider.

        Done last, so a provider outage rolls the whole send back and the
        document number is not used up. A provider failure is reported as a
        DocumentError, which the admin shows as a message.
        """
        from .signing import ProviderError, default_provider_name, get_provider

        provider_name = provider_name or default_provider_name()
        request = _models().SignatureRequest.objects.create(
            document=document, provider=provider_name
        )
        try:
            provider = get_provider(provider_name)
            envelope_id = provider.send(document, request) or ""
        except ProviderError as error:
            raise DocumentError(f"Could not send the document for signature: {error}")
        if envelope_id:
            request.provider_envelope_id = envelope_id
            request.save(update_fields=["provider_envelope_id"])
        return request

    @classmethod
    def _copy_as_sent(cls, document, draft, user):
        if draft is None:
            return None
        with draft.file.open("rb") as handle:
            content = ContentFile(handle.read())
        return cls.attach_file(
            document,
            _models().DocumentFile.Kind.SENT,
            content,
            draft.filename,
            user=user,
            description="Copy of the file sent for signature",
        )

    # ---- collecting signatures ---------------------------------------------

    @classmethod
    @transaction.atomic
    def record_signature(cls, signer, signed_at=None):
        """One signer has signed. Completes the document once all have."""
        document = signer.document
        if document.status not in (
            DocumentStatus.OUT_FOR_SIGNATURE,
            DocumentStatus.PARTIALLY_SIGNED,
        ):
            raise DocumentError("This document is not out for signature.")
        if signer.has_signed:
            raise DocumentError(f"{signer.name} has already signed.")

        signer.status = SignerStatus.SIGNED
        signer.signed_at = signed_at or timezone.now()
        signer.save(update_fields=["status", "signed_at"])

        outstanding = document.signers.exclude(status=SignerStatus.SIGNED).exists()
        if outstanding:
            document.status = DocumentStatus.PARTIALLY_SIGNED
            document.save(update_fields=["status", "updated_at"])
            return document
        return cls._complete_signing(document, signed_on=signer.signed_at.date())

    @classmethod
    @transaction.atomic
    def record_decline(cls, signer, reason=""):
        """A signer refused; the document is dead and must be amended."""
        document = signer.document
        if document.status not in (
            DocumentStatus.OUT_FOR_SIGNATURE,
            DocumentStatus.PARTIALLY_SIGNED,
        ):
            raise DocumentError("This document is not out for signature.")

        signer.status = SignerStatus.DECLINED
        signer.declined_reason = reason
        signer.save(update_fields=["status", "declined_reason"])

        document.status = DocumentStatus.DECLINED
        document.save(update_fields=["status", "updated_at"])
        cls._close_signature_request(document, SignatureRequestStatus.DECLINED)
        cls._close_task(document.signature_task)
        return document

    @classmethod
    @transaction.atomic
    def mark_signed_manually(
        cls, document, executed_file=None, filename="", signed_date=None, user=None
    ):
        """Record a wet-ink signature, or bring in a contract signed long ago.

        Accepts a draft that was never sent, which is how existing paperwork
        is backfilled: it is numbered on the spot so it joins the register.
        """
        allowed = (
            DocumentStatus.DRAFT,
            DocumentStatus.OUT_FOR_SIGNATURE,
            DocumentStatus.PARTIALLY_SIGNED,
        )
        if document.status not in allowed:
            raise DocumentError("Only an unsigned document can be marked as signed.")

        signed_on = signed_date or timezone.localdate()
        if signed_on > timezone.localdate():
            raise DocumentError("The signing date cannot be in the future.")

        if not document.number:
            document.number = DocumentSequence.next_reference(
                document.document_type.prefix, signed_on.year
            )
        if not document.client_snapshot:
            document.client_snapshot = client_snapshot(document)
            document.studio_snapshot = studio_snapshot(StudioProfile.get_solo())

        signed_at = timezone.make_aware(
            timezone.datetime.combine(signed_on, timezone.datetime.min.time())
        )
        document.signers.exclude(status=SignerStatus.SIGNED).update(
            status=SignerStatus.SIGNED, signed_at=signed_at
        )
        if executed_file is not None:
            cls.attach_file(
                document,
                _models().DocumentFile.Kind.EXECUTED,
                executed_file,
                filename or f"{document.number}-signed.pdf",
                user=user,
                description="Signed copy",
            )
        cls._close_signature_request(document, SignatureRequestStatus.COMPLETED)
        return cls._complete_signing(document, signed_on=signed_on)

    @classmethod
    def _complete_signing(cls, document, signed_on):
        """Every signature is in: fix the dates and put the document in force."""
        document.status = DocumentStatus.SIGNED
        document.signed_date = document.signed_date or signed_on
        document.effective_date = document.effective_date or document.signed_date
        if not document.expiry_date:
            months = document.document_type.default_validity_months
            if months:
                document.expiry_date = add_months(document.effective_date, months)
        document.save()

        cls._close_signature_request(document, SignatureRequestStatus.COMPLETED)
        cls._close_task(document.signature_task)

        # A signed agreement moves the project out of contracting
        project = document.project
        if project and project.status == "contracting":
            project.status = "new"
            project.save(update_fields=["status", "updated_at"])

        if document.effective_date <= timezone.localdate():
            cls.activate(document)
        return document

    # ---- the term ------------------------------------------------------------

    @classmethod
    @transaction.atomic
    def activate(cls, document):
        """A signed document whose start date has arrived is now in force."""
        if document.status != DocumentStatus.SIGNED:
            raise DocumentError("Only a signed document can be activated.")
        document.status = DocumentStatus.ACTIVE
        document.save(update_fields=["status", "updated_at"])
        return document

    @classmethod
    @transaction.atomic
    def terminate(cls, document, on_date=None, reason=""):
        """End an agreement before its expiry date."""
        if document.status not in IN_FORCE_STATUSES:
            raise DocumentError("Only a signed or active document can be terminated.")
        on_date = on_date or timezone.localdate()
        if document.effective_date and on_date < document.effective_date:
            raise DocumentError("A document cannot be terminated before it takes effect.")

        document.status = DocumentStatus.TERMINATED
        document.terminated_on = on_date
        document.termination_reason = reason or document.termination_reason
        document.save(update_fields=["status", "terminated_on", "termination_reason", "updated_at"])
        cls._close_task(document.renewal_task)
        return document

    @classmethod
    @transaction.atomic
    def renew(cls, document, today=None):
        """Roll an auto-renewing document forward by one more term."""
        if not document.auto_renews:
            raise DocumentError("This document does not renew automatically.")
        if document.status not in IN_FORCE_STATUSES:
            raise DocumentError("Only a signed or active document can be renewed.")

        months = document.document_type.default_validity_months
        if not months and document.effective_date and document.expiry_date:
            # No default term: reuse the length of the term just finishing
            months = max(
                1,
                round((document.expiry_date - document.effective_date).days / 30),
            )
        document.expiry_date = add_months(
            document.expiry_date or (today or timezone.localdate()),
            months or FALLBACK_RENEWAL_MONTHS,
        )
        document.save(update_fields=["expiry_date", "updated_at"])
        cls._close_task(document.renewal_task)
        document.renewal_task = None
        document.save(update_fields=["renewal_task", "updated_at"])
        return document

    @classmethod
    @transaction.atomic
    def amend(cls, document, user=None):
        """Start the next version as a draft.

        The current version stays in force until the amendment is sent.
        """
        if document.status not in EXECUTED_STATUSES | {DocumentStatus.DECLINED}:
            raise DocumentError(
                "Only a signed, declined or finished document can be amended."
            )
        if document.revisions.filter(status=DocumentStatus.DRAFT).exists():
            raise DocumentError("A draft amendment of this document already exists.")

        Document = _models().Document
        amendment = Document.objects.create(
            document_type=document.document_type,
            client=document.client,
            project=document.project,
            source_quote=document.source_quote,
            created_by=user,
            version=document.version + 1,
            revision_of=document,
            title=document.title,
            language=document.language,
            summary=document.summary,
            auto_renews=document.auto_renews,
            renewal_notice_days=document.renewal_notice_days,
        )
        for signer in document.signers.all():
            amendment.signers.create(
                contact=signer.contact,
                name=signer.name,
                email=signer.email,
                is_studio_side=signer.is_studio_side,
                signing_order=signer.signing_order,
            )
        return amendment

    # ---- the daily job --------------------------------------------------------

    @classmethod
    def activate_due(cls, today=None):
        """Signed documents whose start date has arrived become active."""
        Document = _models().Document
        today = today or timezone.localdate()
        due = Document.objects.filter(
            status=DocumentStatus.SIGNED, effective_date__lte=today
        )
        activated = 0
        for document in due:
            cls.activate(document)
            activated += 1
        return activated

    @classmethod
    def expire_overdue(cls, today=None):
        """Close out documents past their expiry, renewing the ones that roll
        over. Returns (expired, renewed)."""
        Document = _models().Document
        today = today or timezone.localdate()
        overdue = Document.objects.filter(
            status__in=IN_FORCE_STATUSES, expiry_date__lt=today
        ).select_related("document_type")

        expired = renewed = 0
        for document in overdue:
            if document.auto_renews:
                cls.renew(document, today=today)
                renewed += 1
            else:
                document.status = DocumentStatus.EXPIRED
                document.save(update_fields=["status", "updated_at"])
                cls._close_task(document.signature_task)
                expired += 1
        return expired, renewed

    @classmethod
    def create_renewal_reminders(cls, today=None):
        """Raise a task for each document entering its notice window."""
        Document = _models().Document
        today = today or timezone.localdate()
        candidates = Document.objects.filter(
            status__in=IN_FORCE_STATUSES,
            expiry_date__isnull=False,
            expiry_date__gte=today,
            renewal_task__isnull=True,
        ).select_related("client", "project")

        created = 0
        for document in candidates:
            if not document.needs_renewal_attention:
                continue
            action = "renews automatically" if document.auto_renews else "expires"
            document.renewal_task = Task.objects.create(
                title=f"{document.number or document.title} {action} on {document.expiry_date}",
                description=(
                    "Decide whether to renew, renegotiate or let it lapse."
                    if not document.auto_renews
                    else "Cancel before the notice period if it should not roll over."
                ),
                project=document.project,
                client=document.client,
                priority=Task.Priority.HIGH,
                # Never dated in the past: a document already inside its
                # notice window needs attention now, not retrospectively.
                due_date=max(
                    today,
                    document.expiry_date - timedelta(days=document.renewal_notice_days),
                ),
            )
            document.save(update_fields=["renewal_task", "updated_at"])
            created += 1
        return created

    # ---- helpers ---------------------------------------------------------------

    @staticmethod
    def _require_editable(document, message):
        if not document.is_editable:
            raise DocumentError(message)

    @staticmethod
    def _close_task(task):
        if task and task.status != "done":
            task.status = "done"
            task.save(update_fields=["status", "updated_at"])

    @staticmethod
    def _close_signature_request(document, status):
        request = document.signature_requests.filter(
            status=SignatureRequestStatus.SENT
        ).first()
        if request:
            request.status = status
            request.completed_at = timezone.now()
            request.save(update_fields=["status", "completed_at"])
