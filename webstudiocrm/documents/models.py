"""Contracts and other signed paperwork: NDAs, development agreements, SLAs.

A Document is the record of one agreement with a client. It tracks what the
agreement is, who has to sign it, when it was signed, how long it runs, and
every file that belongs to it.

Two rules shape the design, because these are legal records rather than
ordinary uploads:

* nothing is overwritten - each file is a DocumentFile row, and the version
  history stays in the storage bucket
* what the client received is frozen - a document that goes out for signature
  keeps a snapshot of the client and studio details of that moment, the same
  way a sent Quote does

Status changes belong to documents.services.workflow (next step), not here.
"""

import hashlib

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import FileExtensionValidator
from django.db import models
from django.utils import timezone

from core.choices import DocumentLanguage

from .choices import (
    CLOSED_STATUSES,
    EXECUTED_STATUSES,
    IN_FORCE_STATUSES,
    DocumentStatus,
    SignatureProvider,
    SignatureRequestStatus,
    SignerStatus,
)


class DocumentType(models.Model):
    """A kind of document the studio issues - NDA, SLA, development agreement.

    A model rather than a fixed list, so new agreement types can be added
    without a migration, each with its own numbering prefix and defaults.
    """

    name = models.CharField("Name", max_length=100, unique=True)
    prefix = models.CharField(
        "Numbering Prefix",
        max_length=10,
        help_text="Used for the document number, e.g. NDA gives NDA-2026-0001.",
    )
    description = models.TextField("Description", blank=True)

    requires_countersignature = models.BooleanField(
        "Studio Signs Too",
        default=True,
        help_text="The studio must also sign, not just the client.",
    )
    default_validity_months = models.PositiveSmallIntegerField(
        "Default Term (months)",
        null=True,
        blank=True,
        help_text="Used to suggest an expiry date. Empty means open-ended.",
    )
    default_renewal_notice_days = models.PositiveSmallIntegerField(
        "Renewal Reminder (days before expiry)",
        default=30,
        help_text="How far ahead the renewal task is created.",
    )
    template_en = models.FileField(
        "Template (English)", upload_to="documents/templates/", blank=True
    )
    template_hu = models.FileField(
        "Template (Hungarian)", upload_to="documents/templates/", blank=True
    )
    is_default_for_projects = models.BooleanField(
        "Raise For Accepted Quotes",
        default=False,
        help_text="Accepting a quote starts a draft of this type for the project. "
        "Only one type can be marked.",
    )
    is_active = models.BooleanField("Active", default=True)

    class Meta:
        verbose_name = "Document Type"
        verbose_name_plural = "Document Types"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["is_default_for_projects"],
                condition=models.Q(is_default_for_projects=True),
                name="single_default_document_type",
                violation_error_message=(
                    "Another document type is already raised for accepted quotes."
                ),
            )
        ]

    def __str__(self):
        return self.name

    def clean(self):
        super().clean()
        self.prefix = self.prefix.strip().upper()
        errors = {}
        if self.prefix and not self.prefix.replace("-", "").isalnum():
            errors["prefix"] = "Use letters, digits and dashes only, e.g. NDA or DEV-AGR."
        if self.is_default_for_projects and not self.is_active:
            errors["is_default_for_projects"] = "An inactive type cannot be the default."
        if errors:
            raise ValidationError(errors)

    @classmethod
    def for_accepted_quotes(cls):
        """The type raised as a draft when a quote is accepted, if any."""
        return cls.objects.filter(
            is_default_for_projects=True, is_active=True
        ).first()

    def template_in(self, language):
        if language == DocumentLanguage.HUNGARIAN:
            return self.template_hu or None
        return self.template_en or None


class Document(models.Model):
    """One agreement with a client."""

    Status = DocumentStatus

    document_type = models.ForeignKey(
        DocumentType,
        on_delete=models.PROTECT,
        related_name="documents",
        verbose_name="Document Type",
    )
    client = models.ForeignKey(
        "clients.Client",
        on_delete=models.PROTECT,
        related_name="documents",
        verbose_name="Client",
    )
    project = models.ForeignKey(
        "projects.Project",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="documents",
        verbose_name="Project",
        help_text="Leave empty for agreements that cover the whole relationship.",
    )
    source_quote = models.ForeignKey(
        "quotes.Quote",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="documents",
        verbose_name="From Quote",
        help_text="The accepted quote this agreement was raised from.",
    )

    number = models.CharField(
        "Document Number",
        max_length=30,
        unique=True,
        null=True,
        blank=True,
        editable=False,
        help_text="Assigned when the document is sent for signature.",
    )
    version = models.PositiveSmallIntegerField("Version", default=1, editable=False)
    revision_of = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="revisions",
        verbose_name="Revision Of",
        editable=False,
    )
    status = models.CharField(
        "Status",
        max_length=20,
        choices=DocumentStatus,
        default=DocumentStatus.DRAFT,
        editable=False,
    )

    title = models.CharField("Title", max_length=255)
    language = models.CharField(
        "Language",
        max_length=2,
        choices=DocumentLanguage,
        default=DocumentLanguage.HUNGARIAN,
    )
    summary = models.TextField(
        "Summary",
        blank=True,
        help_text="What this agreement covers, in a sentence or two.",
    )

    # ---- dates -------------------------------------------------------------
    sent_at = models.DateTimeField("Sent For Signature", null=True, blank=True, editable=False)
    signed_date = models.DateField(
        "Signed On",
        null=True,
        blank=True,
        help_text="The date the last required signature was given.",
    )
    effective_date = models.DateField(
        "In Force From",
        null=True,
        blank=True,
        help_text="When the agreement starts to apply. Defaults to the signing date.",
    )
    expiry_date = models.DateField(
        "Expires On", null=True, blank=True, help_text="Empty means open-ended."
    )
    terminated_on = models.DateField("Terminated On", null=True, blank=True)
    termination_reason = models.TextField("Termination Reason", blank=True)

    auto_renews = models.BooleanField(
        "Renews Automatically",
        default=False,
        help_text="Rolls over unless cancelled before the notice period.",
    )
    renewal_notice_days = models.PositiveSmallIntegerField(
        "Renewal Reminder (days before expiry)", default=30
    )

    # ---- frozen at send time -----------------------------------------------
    client_snapshot = models.JSONField("Client Details", default=dict, editable=False)
    studio_snapshot = models.JSONField("Studio Details", default=dict, editable=False)

    renewal_task = models.ForeignKey(
        "tasks.Task",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        editable=False,
    )
    signature_task = models.ForeignKey(
        "tasks.Task",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        editable=False,
    )

    notes = models.TextField("Internal Notes", blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="Created By",
        editable=False,
    )
    created_at = models.DateTimeField("Created At", auto_now_add=True)
    updated_at = models.DateTimeField("Updated At", auto_now=True)

    class Meta:
        verbose_name = "Document"
        verbose_name_plural = "Documents"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "expiry_date"], name="document_status_expiry"),
            models.Index(fields=["client", "status"], name="document_client_status"),
        ]

    def __str__(self):
        return f"{self.reference} - {self.title}"

    # ---- read-only helpers --------------------------------------------------

    @property
    def reference(self):
        if self.number:
            return self.number
        return f"Draft #{self.pk}" if self.pk else "Draft"

    @property
    def is_editable(self):
        """Only drafts can be changed; anything sent out is a record."""
        return self.status == DocumentStatus.DRAFT

    @property
    def is_executed(self):
        """Every required signature was collected at some point."""
        return self.status in EXECUTED_STATUSES

    @property
    def is_in_force(self):
        """Executed and still governing today."""
        if self.status not in IN_FORCE_STATUSES:
            return False
        today = timezone.localdate()
        if self.effective_date and self.effective_date > today:
            return False
        return not (self.expiry_date and self.expiry_date < today)

    @property
    def is_closed(self):
        return self.status in CLOSED_STATUSES

    @property
    def days_until_expiry(self):
        if not self.expiry_date:
            return None
        return (self.expiry_date - timezone.localdate()).days

    @property
    def needs_renewal_attention(self):
        """Inside the notice window and not dealt with yet."""
        remaining = self.days_until_expiry
        if remaining is None or self.status not in IN_FORCE_STATUSES:
            return False
        return remaining <= self.renewal_notice_days

    @property
    def executed_file(self):
        """The signed copy, if one has been stored."""
        return self.files.filter(kind=DocumentFile.Kind.EXECUTED).order_by("-uploaded_at").first()

    @property
    def latest_draft_file(self):
        return self.files.filter(kind=DocumentFile.Kind.DRAFT).order_by("-uploaded_at").first()

    @property
    def signing_progress(self):
        """(signed, required) counts for the signature line in the admin."""
        signers = list(self.signers.all())
        return sum(1 for s in signers if s.status == SignerStatus.SIGNED), len(signers)

    # ---- validation ---------------------------------------------------------

    def clean(self):
        super().clean()
        errors = {}

        if self.project_id and self.client_id and self.project.client_id != self.client_id:
            errors["project"] = "This project belongs to a different client."
        if self.source_quote_id and self.client_id:
            if self.source_quote.project.client_id != self.client_id:
                errors["source_quote"] = "This quote belongs to a different client."

        if self.effective_date and self.expiry_date and self.expiry_date < self.effective_date:
            errors["expiry_date"] = "The expiry date cannot be before the start date."
        if self.signed_date and self.effective_date and self.effective_date < self.signed_date:
            errors["effective_date"] = "A document cannot take effect before it is signed."
        if self.signed_date and self.signed_date > timezone.localdate():
            errors["signed_date"] = "The signing date cannot be in the future."
        if self.terminated_on and self.effective_date and self.terminated_on < self.effective_date:
            errors["terminated_on"] = "A document cannot be terminated before it takes effect."
        if self.auto_renews and not self.expiry_date:
            errors["auto_renews"] = "An open-ended document has nothing to renew; set an expiry date."

        if errors:
            raise ValidationError(errors)

    def apply_type_defaults(self):
        """Fill the renewal window from the document type (new documents only)."""
        if self.document_type_id:
            self.renewal_notice_days = self.document_type.default_renewal_notice_days
        return self


class DocumentFile(models.Model):
    """One file belonging to a document.

    Files are added, never replaced: the draft that went out, the executed
    copy that came back and the provider's audit certificate are separate
    rows, so the paper trail stays intact.
    """

    class Kind(models.TextChoices):
        DRAFT = "draft", "Draft"
        SENT = "sent", "Sent For Signature"
        EXECUTED = "executed", "Executed (signed)"
        AUDIT_TRAIL = "audit_trail", "Signature Certificate"
        ATTACHMENT = "attachment", "Attachment"

    document = models.ForeignKey(
        Document, on_delete=models.CASCADE, related_name="files", verbose_name="Document"
    )
    kind = models.CharField("Kind", max_length=20, choices=Kind, default=Kind.DRAFT)
    file = models.FileField(
        "File",
        upload_to="documents/%Y/",
        validators=[FileExtensionValidator(["pdf", "doc", "docx", "odt", "png", "jpg", "jpeg"])],
    )
    description = models.CharField("Description", max_length=255, blank=True)

    size = models.PositiveBigIntegerField("Size (bytes)", default=0, editable=False)
    checksum_sha256 = models.CharField(
        "SHA-256",
        max_length=64,
        blank=True,
        editable=False,
        help_text="Proves the stored file is the one that was signed.",
    )

    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="Uploaded By",
        editable=False,
    )
    uploaded_at = models.DateTimeField("Uploaded At", auto_now_add=True)

    class Meta:
        verbose_name = "File"
        verbose_name_plural = "Files"
        ordering = ["-uploaded_at"]

    def __str__(self):
        return f"{self.get_kind_display()}: {self.filename}"

    @property
    def filename(self):
        return self.file.name.rsplit("/", 1)[-1] if self.file else ""

    def compute_checksum(self):
        """SHA-256 and size of the stored bytes, read through the storage API."""
        digest = hashlib.sha256()
        size = 0
        with self.file.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
                size += len(chunk)
        return digest.hexdigest(), size

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # After super(): the file is in storage, so it can be read back and
        # fingerprinted. Only done once - the bytes behind a row never change.
        if self.file and not self.checksum_sha256:
            self.checksum_sha256, self.size = self.compute_checksum()
            super().save(update_fields=["checksum_sha256", "size"])


class DocumentSigner(models.Model):
    """Someone who has to sign, and whether they have.

    The name and email are copied from the contact so the record still reads
    correctly if the contact is edited or removed later.
    """

    document = models.ForeignKey(
        Document, on_delete=models.CASCADE, related_name="signers", verbose_name="Document"
    )
    contact = models.ForeignKey(
        "clients.Contact",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="document_signatures",
        verbose_name="Contact",
        help_text="Client-side signer. Leave empty for someone outside the CRM.",
    )
    name = models.CharField("Name", max_length=255, blank=True)
    email = models.EmailField("Email Address", blank=True)
    is_studio_side = models.BooleanField(
        "Signs For The Studio",
        default=False,
        help_text="Tick for the studio's own signatory rather than the client's.",
    )
    signing_order = models.PositiveSmallIntegerField(
        "Order", default=1, help_text="Signers with a lower number are asked first."
    )

    status = models.CharField(
        "Status",
        max_length=20,
        choices=SignerStatus,
        default=SignerStatus.PENDING,
        editable=False,
    )
    signed_at = models.DateTimeField("Signed At", null=True, blank=True, editable=False)
    declined_reason = models.TextField("Declined Reason", blank=True)
    provider_recipient_id = models.CharField(
        "Provider Recipient ID", max_length=100, blank=True, editable=False
    )

    class Meta:
        verbose_name = "Signer"
        verbose_name_plural = "Signers"
        ordering = ["signing_order", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["document", "email"],
                name="unique_signer_email_per_document",
                violation_error_message="This email is already a signer on this document.",
            )
        ]

    def __str__(self):
        return f"{self.name or self.email} ({self.get_status_display()})"

    @property
    def has_signed(self):
        return self.status == SignerStatus.SIGNED

    def fill_from_contact(self):
        """Copy the contact's details onto the signer if they are empty."""
        if self.contact_id:
            contact = self.contact
            self.name = self.name or f"{contact.first_name} {contact.last_name}"
            self.email = self.email or contact.email
        return self

    def clean(self):
        super().clean()
        self.fill_from_contact()
        errors = {}
        if self.is_studio_side and self.contact_id:
            errors["contact"] = "A studio signatory is not one of the client's contacts."
        if (
            self.contact_id
            and self.document_id
            and self.contact.client_id != self.document.client_id
        ):
            errors["contact"] = "This contact belongs to a different client."
        if not self.email:
            errors["email"] = "Enter an email address, or pick a contact to take it from."
        if not self.name:
            errors["name"] = "Enter a name, or pick a contact to take it from."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.fill_from_contact()
        super().save(*args, **kwargs)


class SignatureRequest(models.Model):
    """One send-out of a document for signature.

    All provider-specific state lives here, so the Document itself stays the
    same whether signatures are collected electronically or on paper.
    """

    document = models.ForeignKey(
        Document,
        on_delete=models.CASCADE,
        related_name="signature_requests",
        verbose_name="Document",
    )
    provider = models.CharField(
        "Provider", max_length=20, choices=SignatureProvider, default=SignatureProvider.MANUAL
    )
    provider_envelope_id = models.CharField(
        "Provider Reference", max_length=100, blank=True, editable=False
    )
    status = models.CharField(
        "Status",
        max_length=20,
        choices=SignatureRequestStatus,
        default=SignatureRequestStatus.SENT,
        editable=False,
    )
    error_message = models.TextField("Error", blank=True, editable=False)

    sent_at = models.DateTimeField("Sent At", auto_now_add=True)
    completed_at = models.DateTimeField("Completed At", null=True, blank=True, editable=False)
    event_log = models.JSONField(
        "Provider Events",
        default=list,
        editable=False,
        help_text="Raw provider callbacks, newest last - kept for the audit trail.",
    )

    class Meta:
        verbose_name = "Signature Request"
        verbose_name_plural = "Signature Requests"
        ordering = ["-sent_at"]

    def __str__(self):
        return f"{self.get_provider_display()} - {self.get_status_display()}"

    @property
    def is_open(self):
        return self.status == SignatureRequestStatus.SENT
