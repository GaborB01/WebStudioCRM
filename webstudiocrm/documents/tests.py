import hashlib
from datetime import timedelta

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from clients.models import Client, Contact
from cloudstorage.testing import temp_bucket
from core.models import DocumentSequence, StudioProfile, StudioSignatory
from projects.models import Project

from .choices import DocumentStatus, SignatureRequestStatus, SignerStatus
from .models import Document, DocumentFile, DocumentSigner, DocumentType
from .services.workflow import DocumentError, DocumentService, add_months

BUCKET = temp_bucket()
TODAY = timezone.localdate()


class DocumentFixtureMixin:
    def setUp(self):
        self.nda = DocumentType.objects.get(name="Non-Disclosure Agreement")
        self.client_record = Client.objects.create(name="Pékség Kft.")
        self.contact = Contact.objects.create(
            client=self.client_record,
            first_name="Anna",
            last_name="Kovács",
            email="anna@pekseg.hu",
        )
        self.project = Project.objects.create(
            client=self.client_record, name="Webshop", project_type="webshop"
        )

    def make_document(self, **fields):
        fields.setdefault("document_type", self.nda)
        fields.setdefault("client", self.client_record)
        fields.setdefault("title", "Mutual NDA")
        return Document.objects.create(**fields)


class DocumentTypeTests(TestCase):
    def test_the_usual_studio_types_are_seeded(self):
        names = set(DocumentType.objects.values_list("name", flat=True))

        self.assertIn("Non-Disclosure Agreement", names)
        self.assertIn("Service Level Agreement", names)
        self.assertEqual(DocumentType.objects.get(name="Development Agreement").prefix, "DEV")

    def test_prefix_is_normalised(self):
        document_type = DocumentType(name="Partner Agreement", prefix=" par ")
        document_type.full_clean()
        self.assertEqual(document_type.prefix, "PAR")

    def test_a_prefix_with_punctuation_is_rejected(self):
        with self.assertRaises(ValidationError) as caught:
            DocumentType(name="Odd", prefix="N/A").full_clean()
        self.assertIn("prefix", caught.exception.message_dict)


class DocumentLifecycleStateTests(DocumentFixtureMixin, TestCase):
    def test_a_new_document_is_an_editable_draft(self):
        document = self.make_document()

        self.assertEqual(document.status, DocumentStatus.DRAFT)
        self.assertTrue(document.is_editable)
        self.assertFalse(document.is_executed)
        self.assertFalse(document.is_in_force)
        self.assertEqual(document.reference, f"Draft #{document.pk}")

    def test_a_signed_document_in_its_term_is_in_force(self):
        document = self.make_document(
            status=DocumentStatus.ACTIVE,
            signed_date=TODAY - timedelta(days=10),
            effective_date=TODAY - timedelta(days=10),
            expiry_date=TODAY + timedelta(days=355),
        )

        self.assertTrue(document.is_executed)
        self.assertTrue(document.is_in_force)
        self.assertFalse(document.is_editable)

    def test_a_document_is_not_in_force_before_its_start_date(self):
        document = self.make_document(
            status=DocumentStatus.SIGNED,
            signed_date=TODAY,
            effective_date=TODAY + timedelta(days=7),
        )

        self.assertTrue(document.is_executed)
        self.assertFalse(document.is_in_force)

    def test_a_document_past_its_expiry_is_not_in_force(self):
        document = self.make_document(
            status=DocumentStatus.ACTIVE,
            effective_date=TODAY - timedelta(days=400),
            expiry_date=TODAY - timedelta(days=1),
        )

        self.assertFalse(document.is_in_force)
        self.assertEqual(document.days_until_expiry, -1)

    def test_renewal_attention_starts_inside_the_notice_window(self):
        document = self.make_document(
            status=DocumentStatus.ACTIVE,
            effective_date=TODAY - timedelta(days=300),
            expiry_date=TODAY + timedelta(days=20),
            renewal_notice_days=30,
        )

        self.assertTrue(document.needs_renewal_attention)

        document.expiry_date = TODAY + timedelta(days=60)
        self.assertFalse(document.needs_renewal_attention)

    def test_an_open_ended_document_never_needs_renewal(self):
        document = self.make_document(status=DocumentStatus.ACTIVE)

        self.assertIsNone(document.days_until_expiry)
        self.assertFalse(document.needs_renewal_attention)

    def test_a_closed_document_is_not_in_force(self):
        document = self.make_document(
            status=DocumentStatus.TERMINATED,
            effective_date=TODAY - timedelta(days=100),
            terminated_on=TODAY - timedelta(days=1),
        )

        self.assertTrue(document.is_closed)
        self.assertTrue(document.is_executed)
        self.assertFalse(document.is_in_force)


class DocumentValidationTests(DocumentFixtureMixin, TestCase):
    def test_the_project_must_belong_to_the_client(self):
        other = Project.objects.create(
            client=Client.objects.create(name="Other Kft."),
            name="Elsewhere",
            project_type="website",
        )

        with self.assertRaises(ValidationError) as caught:
            self.make_document(project=other).full_clean()

        self.assertIn("project", caught.exception.message_dict)

    def test_expiry_cannot_precede_the_start_date(self):
        document = self.make_document(
            effective_date=TODAY, expiry_date=TODAY - timedelta(days=1)
        )

        with self.assertRaises(ValidationError) as caught:
            document.full_clean()

        self.assertIn("expiry_date", caught.exception.message_dict)

    def test_a_document_cannot_take_effect_before_it_is_signed(self):
        document = self.make_document(
            signed_date=TODAY, effective_date=TODAY - timedelta(days=5)
        )

        with self.assertRaises(ValidationError) as caught:
            document.full_clean()

        self.assertIn("effective_date", caught.exception.message_dict)

    def test_the_signing_date_cannot_be_in_the_future(self):
        document = self.make_document(signed_date=TODAY + timedelta(days=1))

        with self.assertRaises(ValidationError) as caught:
            document.full_clean()

        self.assertIn("signed_date", caught.exception.message_dict)

    def test_auto_renewal_needs_an_expiry_date(self):
        document = self.make_document(auto_renews=True)

        with self.assertRaises(ValidationError) as caught:
            document.full_clean()

        self.assertIn("auto_renews", caught.exception.message_dict)

    def test_a_valid_document_passes(self):
        document = self.make_document(
            project=self.project,
            signed_date=TODAY - timedelta(days=1),
            effective_date=TODAY,
            expiry_date=TODAY + timedelta(days=365),
            auto_renews=True,
        )

        document.full_clean()  # must not raise


class DocumentSignerTests(DocumentFixtureMixin, TestCase):
    def test_details_are_copied_from_the_contact(self):
        document = self.make_document()

        signer = DocumentSigner.objects.create(document=document, contact=self.contact)

        self.assertEqual(signer.name, "Anna Kovács")
        self.assertEqual(signer.email, "anna@pekseg.hu")
        self.assertEqual(signer.status, SignerStatus.PENDING)

    def test_copied_details_survive_the_contact_being_deleted(self):
        document = self.make_document()
        signer = DocumentSigner.objects.create(document=document, contact=self.contact)

        self.contact.delete()
        signer.refresh_from_db()

        self.assertIsNone(signer.contact_id)
        self.assertEqual(signer.name, "Anna Kovács")
        self.assertEqual(signer.email, "anna@pekseg.hu")

    def test_a_contact_of_another_client_is_rejected(self):
        document = self.make_document()
        stranger = Contact.objects.create(
            client=Client.objects.create(name="Other Kft."),
            first_name="Bob",
            last_name="Smith",
            email="bob@other.com",
        )

        with self.assertRaises(ValidationError) as caught:
            DocumentSigner(document=document, contact=stranger).full_clean()

        self.assertIn("contact", caught.exception.message_dict)

    def test_a_studio_signatory_cannot_be_a_client_contact(self):
        document = self.make_document()

        with self.assertRaises(ValidationError) as caught:
            DocumentSigner(
                document=document, contact=self.contact, is_studio_side=True
            ).full_clean()

        self.assertIn("contact", caught.exception.message_dict)

    def test_a_signer_needs_a_name_and_an_email(self):
        document = self.make_document()

        with self.assertRaises(ValidationError) as caught:
            DocumentSigner(document=document).full_clean()

        self.assertIn("email", caught.exception.message_dict)
        self.assertIn("name", caught.exception.message_dict)

    def test_signing_progress_counts_signatures(self):
        document = self.make_document()
        DocumentSigner.objects.create(document=document, contact=self.contact)
        DocumentSigner.objects.create(
            document=document, name="Studio Owner", email="owner@studio.hu",
            is_studio_side=True, signing_order=2,
        )

        self.assertEqual(document.signing_progress, (0, 2))

        document.signers.filter(is_studio_side=True).update(status=SignerStatus.SIGNED)
        self.assertEqual(document.signing_progress, (1, 2))


@override_settings(CLOUD_STORAGE=BUCKET)
class DocumentFileTests(DocumentFixtureMixin, TestCase):
    def test_a_stored_file_is_fingerprinted(self):
        document = self.make_document()

        stored = DocumentFile.objects.create(
            document=document,
            kind=DocumentFile.Kind.EXECUTED,
            file=ContentFile(b"%PDF-1.7 signed", name="nda-signed.pdf"),
        )

        self.assertEqual(stored.size, 15)
        self.assertEqual(
            stored.checksum_sha256, hashlib.sha256(b"%PDF-1.7 signed").hexdigest()
        )
        self.assertEqual(stored.filename, "nda-signed.pdf")

    def test_the_executed_copy_is_found_among_the_files(self):
        document = self.make_document()
        DocumentFile.objects.create(
            document=document,
            kind=DocumentFile.Kind.DRAFT,
            file=ContentFile(b"draft", name="nda-draft.pdf"),
        )
        executed = DocumentFile.objects.create(
            document=document,
            kind=DocumentFile.Kind.EXECUTED,
            file=ContentFile(b"signed", name="nda-executed.pdf"),
        )

        self.assertEqual(document.executed_file, executed)
        self.assertEqual(document.latest_draft_file.kind, DocumentFile.Kind.DRAFT)

    def test_files_go_to_the_versioned_bucket(self):
        from cloudstorage.models import StoredObject

        document = self.make_document()
        stored = DocumentFile.objects.create(
            document=document,
            kind=DocumentFile.Kind.EXECUTED,
            file=ContentFile(b"signed", name="agreement.pdf"),
        )

        self.assertTrue(StoredObject.objects.filter(key=stored.file.name).exists())


@override_settings(CLOUD_STORAGE=BUCKET)
class DocumentAdminTests(DocumentFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(User.objects.create_superuser("admin", "a@a.hu", "pw"))

    def test_a_draft_is_fully_editable(self):
        document = self.make_document()

        response = self.client.get(
            reverse("admin:documents_document_change", args=[document.pk])
        )

        self.assertEqual(response.status_code, 200)
        form = response.context["adminform"].form
        self.assertIn("title", form.fields)
        self.assertIn("document_type", form.fields)

    def test_a_sent_document_locks_its_content_but_not_its_dates(self):
        document = self.make_document(status=DocumentStatus.OUT_FOR_SIGNATURE)

        response = self.client.get(
            reverse("admin:documents_document_change", args=[document.pk])
        )

        form = response.context["adminform"].form
        self.assertNotIn("title", form.fields)
        self.assertNotIn("document_type", form.fields)
        # a wet-ink signature or a termination must still be recordable
        self.assertIn("signed_date", form.fields)
        self.assertIn("terminated_on", form.fields)
        self.assertIn("notes", form.fields)

    def test_a_sent_document_cannot_be_deleted(self):
        document = self.make_document(status=DocumentStatus.SIGNED)

        self.assertFalse(
            self.client.get(
                reverse("admin:documents_document_change", args=[document.pk])
            ).context["has_delete_permission"]
        )

    def test_the_changelist_shows_signing_progress_and_expiry(self):
        document = self.make_document(
            status=DocumentStatus.ACTIVE,
            effective_date=TODAY - timedelta(days=10),
            expiry_date=TODAY + timedelta(days=5),
            renewal_notice_days=30,
        )
        DocumentSigner.objects.create(document=document, contact=self.contact)

        response = self.client.get(reverse("admin:documents_document_changelist"))

        self.assertContains(response, "0/1 signed")
        self.assertContains(response, "in 5 days")

    def test_the_expiry_filter_narrows_the_list(self):
        soon = self.make_document(
            title="Expiring soon", expiry_date=TODAY + timedelta(days=10)
        )
        self.make_document(title="Far off", expiry_date=TODAY + timedelta(days=300))

        response = self.client.get(
            reverse("admin:documents_document_changelist"), {"expiry": "expiring_30"}
        )

        self.assertEqual(response.context["cl"].result_count, 1)
        self.assertEqual(response.context["cl"].result_list.get(), soon)

    def test_documents_show_up_on_the_client_page(self):
        self.make_document(title="Mutual NDA")

        response = self.client.get(
            reverse("admin:clients_client_change", args=[self.client_record.pk])
        )

        self.assertContains(response, "Mutual NDA")

    def test_an_existing_signed_contract_can_be_recorded_by_hand(self):
        """The backfill path: upload the executed copy and fill in the dates."""
        document = self.make_document(
            status=DocumentStatus.ACTIVE,
            signed_date=TODAY - timedelta(days=200),
            effective_date=TODAY - timedelta(days=200),
            expiry_date=TODAY + timedelta(days=165),
        )
        DocumentFile.objects.create(
            document=document,
            kind=DocumentFile.Kind.EXECUTED,
            file=ContentFile(b"%PDF-1.7 old contract", name="legacy-nda.pdf"),
        )

        response = self.client.get(
            reverse("admin:documents_document_change", args=[document.pk])
        )

        self.assertContains(response, "legacy-nda.pdf")
        self.assertTrue(document.is_in_force)


@override_settings(CLOUD_STORAGE=BUCKET)
class SignerLiveDetailsTests(DocumentFixtureMixin, TestCase):
    """Picking a contact on a signer row shows their details immediately: the
    endpoint serves what fill_from_contact() copies on save, and the signer
    autocomplete only offers the document's client's contacts."""

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_superuser("admin", "a@a.hu", "pw")
        self.client.force_login(self.user)
        self.document = self.make_document()
        self.other_client = Client.objects.create(name="Other Kft.")
        self.stranger = Contact.objects.create(
            client=self.other_client, first_name="Bob", last_name="Smith", email="bob@other.com"
        )

    def details(self, contact):
        return self.client.get(
            reverse("admin:documents_document_contact_details", args=[self.document.pk]),
            {"contact": contact.pk},
        )

    def test_returns_what_the_save_would_copy(self):
        response = self.details(self.contact)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(), {"name": "Anna Kovács", "email": "anna@pekseg.hu"}
        )

    def test_another_clients_contact_is_not_served(self):
        self.assertEqual(self.details(self.stranger).status_code, 404)

    def test_requires_a_logged_in_staff_user(self):
        self.client.logout()
        response = self.details(self.contact)
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])

    def test_change_form_loads_the_scripts_and_endpoint(self):
        response = self.client.get(
            reverse("admin:documents_document_change", args=[self.document.pk])
        )

        self.assertContains(response, "documents/js/signer_defaults.js")
        self.assertContains(response, "clients/js/client_filtered_autocomplete.js")
        self.assertContains(
            response,
            reverse("admin:documents_document_contact_details", args=[self.document.pk]),
        )

    def test_signer_autocomplete_is_limited_to_the_documents_client(self):
        response = self.client.get(
            reverse("admin:autocomplete"),
            {
                "app_label": "documents",
                "model_name": "documentsigner",
                "field_name": "contact",
                "term": "",
                "client": self.client_record.pk,
            },
        )

        names = sorted(r["text"] for r in response.json()["results"])
        self.assertEqual(names, ["Anna Kovács (Pékség Kft.)"])


class WorkflowFixtureMixin(DocumentFixtureMixin):
    def setUp(self):
        super().setUp()
        profile = StudioProfile.get_solo()
        profile.company_name = "WebStudio Kft."
        profile.save()

    def ready_draft(self, document_type=None, **fields):
        """A draft with signers and a file: everything `send` requires."""
        document = self.make_document(document_type=document_type or self.nda, **fields)
        DocumentSigner.objects.create(document=document, contact=self.contact)
        DocumentSigner.objects.create(
            document=document, name="Studio Owner", email="owner@studio.hu",
            is_studio_side=True, signing_order=2,
        )
        DocumentService.attach_file(
            document, DocumentFile.Kind.DRAFT, ContentFile(b"%PDF-1.7 draft"), "nda.pdf"
        )
        return document

    def sign_all(self, document):
        for signer in document.signers.all():
            DocumentService.record_signature(signer)
        document.refresh_from_db()
        return document


@override_settings(CLOUD_STORAGE=BUCKET)
class SendForSignatureTests(WorkflowFixtureMixin, TestCase):
    def test_send_numbers_freezes_and_raises_a_task(self):
        document = self.ready_draft()

        DocumentService.send_for_signature(document)

        year = timezone.localdate().year
        self.assertEqual(document.number, f"NDA-{year}-0001")
        self.assertEqual(document.status, DocumentStatus.OUT_FOR_SIGNATURE)
        self.assertIsNotNone(document.sent_at)
        self.assertEqual(document.client_snapshot["name"], "Pékség Kft.")
        self.assertEqual(document.studio_snapshot["company_name"], "WebStudio Kft.")
        self.assertEqual(len(document.client_snapshot["signers"]), 2)
        self.assertIn("Chase signature", document.signature_task.title)
        self.assertEqual(document.signature_requests.count(), 1)

    def test_send_freezes_a_copy_of_the_file_that_went_out(self):
        document = self.ready_draft()

        DocumentService.send_for_signature(document)

        sent = document.files.get(kind=DocumentFile.Kind.SENT)
        draft = document.files.get(kind=DocumentFile.Kind.DRAFT)
        self.assertEqual(sent.checksum_sha256, draft.checksum_sha256)
        with sent.file.open("rb") as handle:
            self.assertEqual(handle.read(), b"%PDF-1.7 draft")

    def test_each_type_numbers_in_its_own_series(self):
        sla = DocumentType.objects.get(name="Service Level Agreement")
        DocumentService.send_for_signature(self.ready_draft())
        second = self.ready_draft(document_type=sla, title="SLA")

        DocumentService.send_for_signature(second)

        year = timezone.localdate().year
        self.assertEqual(second.number, f"SLA-{year}-0001")

    def test_a_draft_without_signers_or_a_file_cannot_be_sent(self):
        document = self.make_document()

        problems = DocumentService.send_problems(document)

        self.assertIn("Add at least one signer.", problems)
        self.assertIn("Upload the draft document to be signed.", problems)
        with self.assertRaises(DocumentError):
            DocumentService.send_for_signature(document)

    def test_a_countersigned_type_needs_a_studio_signatory(self):
        document = self.make_document()
        DocumentSigner.objects.create(document=document, contact=self.contact)
        DocumentService.attach_file(
            document, DocumentFile.Kind.DRAFT, ContentFile(b"x"), "nda.pdf"
        )

        self.assertIn(
            "countersigned by the studio",
            " ".join(DocumentService.send_problems(document)),
        )

    def test_a_document_needs_a_client_side_signer(self):
        document = self.make_document()
        DocumentSigner.objects.create(
            document=document, name="Owner", email="o@s.hu", is_studio_side=True
        )
        DocumentService.attach_file(
            document, DocumentFile.Kind.DRAFT, ContentFile(b"x"), "nda.pdf"
        )

        self.assertIn(
            "Add at least one signer on the client's side.",
            DocumentService.send_problems(document),
        )

    def test_a_sent_document_cannot_be_sent_again(self):
        document = self.ready_draft()
        DocumentService.send_for_signature(document)

        with self.assertRaises(DocumentError):
            DocumentService.send_for_signature(document)

    def test_a_failed_send_uses_no_number(self):
        document = self.make_document()  # not ready

        with self.assertRaises(DocumentError):
            DocumentService.send_for_signature(document)

        document.refresh_from_db()
        self.assertIsNone(document.number)
        self.assertEqual(DocumentSequence.objects.count(), 0)


@override_settings(CLOUD_STORAGE=BUCKET)
class SigningTests(WorkflowFixtureMixin, TestCase):
    def test_one_signature_of_two_is_partial(self):
        document = self.ready_draft()
        DocumentService.send_for_signature(document)

        DocumentService.record_signature(document.signers.first())

        document.refresh_from_db()
        self.assertEqual(document.status, DocumentStatus.PARTIALLY_SIGNED)
        self.assertEqual(document.signing_progress, (1, 2))
        self.assertIsNone(document.signed_date)

    def test_the_last_signature_completes_and_activates_the_document(self):
        document = self.ready_draft()
        DocumentService.send_for_signature(document)

        self.sign_all(document)

        today = timezone.localdate()
        self.assertEqual(document.status, DocumentStatus.ACTIVE)
        self.assertEqual(document.signed_date, today)
        self.assertEqual(document.effective_date, today)
        # NDA type carries a 24 month default term
        self.assertEqual(document.expiry_date, add_months(today, 24))
        self.assertTrue(document.is_in_force)
        self.assertEqual(document.signature_task.status, "done")
        self.assertEqual(
            document.signature_requests.get().status, SignatureRequestStatus.COMPLETED
        )

    def test_a_future_start_date_keeps_the_document_signed_not_active(self):
        start = timezone.localdate() + timedelta(days=30)
        document = self.ready_draft(effective_date=start)
        DocumentService.send_for_signature(document)

        self.sign_all(document)

        self.assertEqual(document.status, DocumentStatus.SIGNED)
        self.assertFalse(document.is_in_force)

    def test_signing_twice_is_refused(self):
        document = self.ready_draft()
        DocumentService.send_for_signature(document)
        signer = document.signers.first()
        DocumentService.record_signature(signer)

        with self.assertRaises(DocumentError):
            DocumentService.record_signature(signer)

    def test_a_decline_stops_the_document(self):
        document = self.ready_draft()
        DocumentService.send_for_signature(document)

        DocumentService.record_decline(document.signers.first(), reason="Clause 7")

        document.refresh_from_db()
        self.assertEqual(document.status, DocumentStatus.DECLINED)
        self.assertEqual(document.signers.first().declined_reason, "Clause 7")
        self.assertEqual(
            document.signature_requests.get().status, SignatureRequestStatus.DECLINED
        )
        self.assertEqual(document.signature_task.status, "done")

    def test_signatures_cannot_be_recorded_on_a_draft(self):
        document = self.ready_draft()

        with self.assertRaises(DocumentError):
            DocumentService.record_signature(document.signers.first())


@override_settings(CLOUD_STORAGE=BUCKET)
class ManualSigningTests(WorkflowFixtureMixin, TestCase):
    def test_wet_ink_after_sending_completes_the_document(self):
        document = self.ready_draft()
        DocumentService.send_for_signature(document)

        DocumentService.mark_signed_manually(
            document, executed_file=ContentFile(b"%PDF scan"), filename="scan.pdf"
        )

        document.refresh_from_db()
        self.assertEqual(document.status, DocumentStatus.ACTIVE)
        self.assertTrue(all(s.has_signed for s in document.signers.all()))
        self.assertEqual(document.executed_file.filename, "scan.pdf")

    def test_an_old_contract_can_be_backfilled_straight_from_draft(self):
        signed_on = timezone.localdate() - timedelta(days=200)
        document = self.ready_draft()

        DocumentService.mark_signed_manually(
            document,
            executed_file=ContentFile(b"%PDF legacy"),
            filename="legacy.pdf",
            signed_date=signed_on,
        )

        document.refresh_from_db()
        self.assertEqual(document.number, f"NDA-{signed_on.year}-0001")
        self.assertEqual(document.signed_date, signed_on)
        self.assertEqual(document.effective_date, signed_on)
        self.assertEqual(document.expiry_date, add_months(signed_on, 24))
        self.assertEqual(document.status, DocumentStatus.ACTIVE)
        self.assertTrue(document.client_snapshot)

    def test_a_future_signing_date_is_refused(self):
        document = self.ready_draft()

        with self.assertRaises(DocumentError):
            DocumentService.mark_signed_manually(
                document, signed_date=timezone.localdate() + timedelta(days=1)
            )


@override_settings(CLOUD_STORAGE=BUCKET)
class TermTests(WorkflowFixtureMixin, TestCase):
    def active_document(self, **fields):
        document = self.ready_draft(**fields)
        DocumentService.send_for_signature(document)
        return self.sign_all(document)

    def test_a_signed_document_activates_when_its_start_date_arrives(self):
        document = self.active_document(
            effective_date=timezone.localdate() + timedelta(days=5)
        )
        self.assertEqual(document.status, DocumentStatus.SIGNED)

        activated = DocumentService.activate_due(
            today=timezone.localdate() + timedelta(days=5)
        )

        document.refresh_from_db()
        self.assertEqual(activated, 1)
        self.assertEqual(document.status, DocumentStatus.ACTIVE)

    def test_an_overdue_document_expires(self):
        document = self.active_document()
        document.expiry_date = timezone.localdate() - timedelta(days=1)
        document.save(update_fields=["expiry_date"])

        expired, renewed = DocumentService.expire_overdue()

        document.refresh_from_db()
        self.assertEqual((expired, renewed), (1, 0))
        self.assertEqual(document.status, DocumentStatus.EXPIRED)
        self.assertFalse(document.is_in_force)

    def test_an_auto_renewing_document_rolls_over_instead_of_expiring(self):
        document = self.active_document()
        old_expiry = timezone.localdate() - timedelta(days=1)
        document.expiry_date = old_expiry
        document.auto_renews = True
        document.save(update_fields=["expiry_date", "auto_renews"])

        expired, renewed = DocumentService.expire_overdue()

        document.refresh_from_db()
        self.assertEqual((expired, renewed), (0, 1))
        self.assertEqual(document.status, DocumentStatus.ACTIVE)
        self.assertEqual(document.expiry_date, add_months(old_expiry, 24))

    def test_a_renewal_reminder_is_raised_inside_the_notice_window(self):
        document = self.active_document()
        document.expiry_date = timezone.localdate() + timedelta(days=10)
        document.renewal_notice_days = 30
        document.save(update_fields=["expiry_date", "renewal_notice_days"])

        created = DocumentService.create_renewal_reminders()

        document.refresh_from_db()
        self.assertEqual(created, 1)
        self.assertIn("expires on", document.renewal_task.title)
        self.assertEqual(document.renewal_task.client, self.client_record)

    def test_a_reminder_is_not_raised_twice(self):
        document = self.active_document()
        document.expiry_date = timezone.localdate() + timedelta(days=10)
        document.save(update_fields=["expiry_date"])

        DocumentService.create_renewal_reminders()
        self.assertEqual(DocumentService.create_renewal_reminders(), 0)

    def test_nothing_is_reminded_outside_the_notice_window(self):
        document = self.active_document()
        document.expiry_date = timezone.localdate() + timedelta(days=300)
        document.save(update_fields=["expiry_date"])

        self.assertEqual(DocumentService.create_renewal_reminders(), 0)

    def test_terminating_closes_the_agreement_and_its_renewal_task(self):
        document = self.active_document()
        document.expiry_date = timezone.localdate() + timedelta(days=10)
        document.save(update_fields=["expiry_date"])
        DocumentService.create_renewal_reminders()
        document.refresh_from_db()

        DocumentService.terminate(document, reason="Client closed down")

        document.refresh_from_db()
        self.assertEqual(document.status, DocumentStatus.TERMINATED)
        self.assertEqual(document.terminated_on, timezone.localdate())
        self.assertEqual(document.termination_reason, "Client closed down")
        self.assertEqual(document.renewal_task.status, "done")

    def test_a_draft_cannot_be_terminated(self):
        with self.assertRaises(DocumentError):
            DocumentService.terminate(self.ready_draft())


@override_settings(CLOUD_STORAGE=BUCKET)
class AmendmentTests(WorkflowFixtureMixin, TestCase):
    def signed_document(self):
        document = self.ready_draft()
        DocumentService.send_for_signature(document)
        return self.sign_all(document)

    def test_an_amendment_starts_as_the_next_draft_version(self):
        original = self.signed_document()

        amendment = DocumentService.amend(original)

        self.assertEqual(amendment.version, 2)
        self.assertEqual(amendment.revision_of, original)
        self.assertEqual(amendment.status, DocumentStatus.DRAFT)
        self.assertIsNone(amendment.number)
        self.assertEqual(amendment.signers.count(), 2)
        self.assertTrue(all(not s.has_signed for s in amendment.signers.all()))

    def test_sending_the_amendment_supersedes_the_original(self):
        original = self.signed_document()
        amendment = DocumentService.amend(original)
        DocumentService.attach_file(
            amendment, DocumentFile.Kind.DRAFT, ContentFile(b"v2"), "nda-v2.pdf"
        )

        DocumentService.send_for_signature(amendment)

        original.refresh_from_db()
        self.assertEqual(original.status, DocumentStatus.SUPERSEDED)
        self.assertEqual(amendment.number, f"{original.number}-v2")

    def test_only_one_draft_amendment_at_a_time(self):
        original = self.signed_document()
        DocumentService.amend(original)

        with self.assertRaises(DocumentError):
            DocumentService.amend(original)

    def test_a_draft_cannot_be_amended(self):
        with self.assertRaises(DocumentError):
            DocumentService.amend(self.ready_draft())

    def test_a_declined_document_can_be_amended(self):
        document = self.ready_draft()
        DocumentService.send_for_signature(document)
        DocumentService.record_decline(document.signers.first())
        document.refresh_from_db()

        self.assertEqual(DocumentService.amend(document).version, 2)


@override_settings(CLOUD_STORAGE=BUCKET)
class ProjectAndQuoteIntegrationTests(WorkflowFixtureMixin, TestCase):
    def test_signing_moves_the_project_out_of_contracting(self):
        self.project.status = "contracting"
        self.project.save(update_fields=["status"])
        document = self.ready_draft(project=self.project)
        DocumentService.send_for_signature(document)

        self.sign_all(document)

        self.project.refresh_from_db()
        self.assertEqual(self.project.status, "new")

    def test_accepting_a_quote_opens_a_draft_agreement(self):
        from quotes.services.workflow import QuoteService
        from quotes.tests import QuoteFixtureMixin

        fixture = QuoteFixtureMixin()
        fixture.setUp()
        quote = QuoteService.send(fixture.make_quote())

        QuoteService.accept(quote)

        document = Document.objects.get(source_quote=quote)
        self.assertEqual(document.status, DocumentStatus.DRAFT)
        self.assertEqual(document.document_type.name, "Development Agreement")
        self.assertEqual(document.project, quote.project)
        self.assertIn(quote.number, document.summary)

    def test_the_hook_is_off_when_no_type_is_marked(self):
        from quotes.services.workflow import QuoteService
        from quotes.tests import QuoteFixtureMixin

        DocumentType.objects.update(is_default_for_projects=False)
        fixture = QuoteFixtureMixin()
        fixture.setUp()
        quote = QuoteService.send(fixture.make_quote())

        QuoteService.accept(quote)

        self.assertFalse(Document.objects.filter(source_quote=quote).exists())


@override_settings(CLOUD_STORAGE=BUCKET)
class ReviewDocumentsCommandTests(WorkflowFixtureMixin, TestCase):
    def test_the_daily_job_reports_each_pass(self):
        import io

        from django.core.management import call_command

        document = self.ready_draft()
        DocumentService.send_for_signature(document)
        self.sign_all(document)
        document.expiry_date = timezone.localdate() - timedelta(days=1)
        document.save(update_fields=["expiry_date"])

        output = io.StringIO()
        call_command("review_documents", stdout=output)

        self.assertIn("1 document(s) marked as expired", output.getvalue())
        document.refresh_from_db()
        self.assertEqual(document.status, DocumentStatus.EXPIRED)


@override_settings(CLOUD_STORAGE=BUCKET)
class RenewalReminderDueDateTests(WorkflowFixtureMixin, TestCase):
    """A reminder is only raised once the document is already inside its
    notice window, so `expiry - notice_days` is always today or earlier. The
    task is therefore due the day it is raised, never backdated."""

    def active_with_expiry(self, days, notice=30):
        document = self.ready_draft()
        DocumentService.send_for_signature(document)
        self.sign_all(document)
        document.expiry_date = timezone.localdate() + timedelta(days=days)
        document.renewal_notice_days = notice
        document.save(update_fields=["expiry_date", "renewal_notice_days"])
        return document

    def test_a_reminder_is_due_the_day_it_is_raised(self):
        document = self.active_with_expiry(days=30, notice=30)  # window opens today

        DocumentService.create_renewal_reminders()

        document.refresh_from_db()
        self.assertEqual(document.renewal_task.due_date, timezone.localdate())

    def test_a_document_deep_inside_its_window_is_not_backdated(self):
        document = self.active_with_expiry(days=5, notice=30)  # window opened 25 days ago

        DocumentService.create_renewal_reminders()

        document.refresh_from_db()
        self.assertEqual(document.renewal_task.due_date, timezone.localdate())

    def test_a_document_outside_its_window_gets_nothing_yet(self):
        self.active_with_expiry(days=60, notice=30)

        self.assertEqual(DocumentService.create_renewal_reminders(), 0)


@override_settings(CLOUD_STORAGE=BUCKET)
class StudioSignatoryTests(WorkflowFixtureMixin, TestCase):
    """The studio's own signer comes from the studio profile, not from the
    client's contact list, and is added to countersigned drafts automatically."""

    def configure_signatory(self, **fields):
        return StudioSignatory.objects.create(
            profile=StudioProfile.get_solo(),
            name=fields.pop("name", "Gábor Boros"),
            email=fields.pop("email", "gabor@webstudio.hu"),
            role=fields.pop("role", "Managing Director"),
            **fields,
        )

    def test_the_marked_default_wins_over_other_signatories(self):
        self.configure_signatory(name="Partner", email="p@s.hu")
        marked = self.configure_signatory(is_default=True)

        self.assertEqual(StudioSignatory.get_default(), marked)

    def test_a_sole_signatory_is_the_default_without_a_mark(self):
        only = self.configure_signatory()
        self.assertEqual(StudioSignatory.get_default(), only)

    def test_two_unmarked_signatories_mean_no_default(self):
        self.configure_signatory()
        self.configure_signatory(name="Partner", email="p@s.hu")

        self.assertIsNone(StudioSignatory.get_default())

    def test_a_new_countersigned_document_gets_the_studio_signer(self):
        self.configure_signatory()

        document = DocumentService.create_for_client(
            self.client_record, self.nda, title="NDA"
        )

        signer = document.signers.get()
        self.assertTrue(signer.is_studio_side)
        self.assertEqual(signer.name, "Gábor Boros")
        self.assertEqual(signer.email, "gabor@webstudio.hu")

    def test_no_signer_is_added_when_the_type_is_not_countersigned(self):
        self.configure_signatory()
        one_sided = DocumentType.objects.create(
            name="Client Declaration", prefix="DECL", requires_countersignature=False
        )

        document = DocumentService.create_for_client(
            self.client_record, one_sided, title="Declaration"
        )

        self.assertEqual(document.signers.count(), 0)

    def test_the_signer_is_not_duplicated(self):
        self.configure_signatory()
        document = DocumentService.create_for_client(
            self.client_record, self.nda, title="NDA"
        )

        self.assertIsNone(DocumentService.ensure_studio_signer(document))
        self.assertEqual(document.signers.filter(is_studio_side=True).count(), 1)

    def test_the_studio_signs_after_the_clients_signers(self):
        document = self.make_document()
        DocumentSigner.objects.create(
            document=document, contact=self.contact, signing_order=3
        )
        self.configure_signatory()

        signer = DocumentService.ensure_studio_signer(document)

        self.assertEqual(signer.signing_order, 4)

    def test_send_problems_direct_to_the_studio_profile_when_unconfigured(self):
        document = self.make_document()

        problems = " ".join(DocumentService.send_problems(document))

        self.assertIn("add your signatory in Core -> Studio Profile", problems)

    def test_send_problems_suggest_saving_when_configured(self):
        self.configure_signatory()
        document = self.make_document()

        problems = " ".join(DocumentService.send_problems(document))

        self.assertIn("save the document to add your signatory", problems)

    def test_saving_a_draft_in_the_admin_repairs_a_missing_studio_signer(self):
        from django.contrib.auth.models import User

        self.configure_signatory()
        document = self.make_document()  # created before the signatory existed
        self.client.force_login(User.objects.create_superuser("admin", "a@a.hu", "pw"))

        response = self.client.post(
            reverse("admin:documents_document_change", args=[document.pk]),
            {
                "document_type": self.nda.pk, "client": self.client_record.pk,
                "project": "", "source_quote": "", "title": "Mutual NDA",
                "language": "hu", "summary": "", "effective_date": "",
                "expiry_date": "", "auto_renews": "", "renewal_notice_days": "30",
                "terminated_on": "", "termination_reason": "", "notes": "",
                "signers-TOTAL_FORMS": "0", "signers-INITIAL_FORMS": "0",
                "files-TOTAL_FORMS": "0", "files-INITIAL_FORMS": "0",
            },
            follow=True,
        )

        self.assertContains(response, "added as the studio&#x27;s signatory")
        signer = document.signers.get()
        self.assertTrue(signer.is_studio_side)
        self.assertEqual(signer.name, "Gábor Boros")

    def test_the_quote_hook_document_also_gets_the_studio_signer(self):
        from quotes.services.workflow import QuoteService
        from quotes.tests import QuoteFixtureMixin

        self.configure_signatory()
        fixture = QuoteFixtureMixin()
        fixture.setUp()
        quote = QuoteService.send(fixture.make_quote())

        QuoteService.accept(quote)

        document = Document.objects.get(source_quote=quote)
        self.assertTrue(document.signers.filter(is_studio_side=True).exists())
