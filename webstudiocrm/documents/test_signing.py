"""E-signature integration: the provider-agnostic pipeline, and the
Documenso adapter's translation of its wire format."""

import json
from datetime import timedelta

from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .choices import DocumentStatus, SignatureRequestStatus, SignerStatus
from .models import DocumentFile
from .services.signing import PROVIDERS, ProviderError, get_provider
from .services.signing.base import ProviderState, RecipientState, SigningProvider
from .services.signing.documenso import DocumensoProvider
from .services.signing.reconcile import reconcile
from .services.workflow import DocumentError, DocumentService
from .tests import BUCKET, WorkflowFixtureMixin

DOCUMENSO_SETTINGS = {
    "PROVIDER": "documenso",
    "DOCUMENSO": {
        "API_BASE": "https://example.invalid/api/v2",
        "API_TOKEN": "api_test_token",
        "WEBHOOK_SECRET": "a-long-random-secret",
        "TIMEOUT": 5,
    },
}


class FakeProvider(SigningProvider):
    """Stands in for a real provider so the pipeline can be tested without
    a network or an account."""

    name = "fake"
    sent = []
    state = None
    executed = (b"%PDF-1.7 signed", "signed.pdf")
    certificate = (b"%PDF-1.7 cert", "certificate.pdf")

    def send(self, document, signature_request):
        FakeProvider.sent.append(document.pk)
        return "env-123"

    def fetch_state(self, signature_request):
        return FakeProvider.state

    def download_executed(self, signature_request):
        return FakeProvider.executed

    def download_certificate(self, signature_request):
        return FakeProvider.certificate


def fake_state(document, signed=(), declined=(), completed=False, **kwargs):
    recipients = tuple(
        RecipientState(email=email, signed=True, signed_at=timezone.now())
        for email in signed
    ) + tuple(
        RecipientState(email=email, declined=True, decline_reason="Clause 7")
        for email in declined
    )
    return ProviderState(
        envelope_id="env-123", completed=completed, recipients=recipients, **kwargs
    )


class SigningProviderBase(WorkflowFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        FakeProvider.sent = []
        FakeProvider.state = None
        PROVIDERS["fake"] = FakeProvider
        self.addCleanup(PROVIDERS.pop, "fake", None)

    def sent_document(self, provider="fake"):
        document = self.ready_draft()
        DocumentService.send_for_signature(document, provider=provider)
        return document

    @property
    def client_email(self):
        return self.contact.email

    @property
    def studio_email(self):
        return "owner@studio.hu"


@override_settings(CLOUD_STORAGE=BUCKET)
class ProviderRegistryTests(SigningProviderBase):
    def test_the_default_provider_is_manual(self):
        self.assertEqual(get_provider().name, "manual")

    @override_settings(SIGNING=DOCUMENSO_SETTINGS)
    def test_the_settings_choose_the_provider(self):
        self.assertEqual(get_provider().name, "documenso")

    def test_an_unknown_provider_is_refused(self):
        with self.assertRaises(ProviderError):
            get_provider("carrier-pigeon")

    def test_documenso_reports_whether_it_is_configured(self):
        self.assertFalse(DocumensoProvider.is_configured())
        with override_settings(SIGNING=DOCUMENSO_SETTINGS):
            self.assertTrue(DocumensoProvider.is_configured())


@override_settings(CLOUD_STORAGE=BUCKET)
class SendDispatchTests(SigningProviderBase):
    def test_sending_hands_the_document_to_the_provider(self):
        document = self.sent_document()

        request = document.signature_requests.get()
        self.assertEqual(FakeProvider.sent, [document.pk])
        self.assertEqual(request.provider, "fake")
        self.assertEqual(request.provider_envelope_id, "env-123")
        self.assertEqual(request.status, SignatureRequestStatus.SENT)

    def test_the_manual_provider_opens_a_send_out_with_no_envelope(self):
        document = self.ready_draft()

        DocumentService.send_for_signature(document, provider="manual")

        request = document.signature_requests.get()
        self.assertEqual(request.provider, "manual")
        self.assertEqual(request.provider_envelope_id, "")

    def test_a_provider_failure_rolls_the_whole_send_back(self):
        class BrokenProvider(FakeProvider):
            name = "broken"

            def send(self, document, signature_request):
                raise ProviderError("the API is down")

        PROVIDERS["broken"] = BrokenProvider
        self.addCleanup(PROVIDERS.pop, "broken", None)
        document = self.ready_draft()

        with self.assertRaises(DocumentError) as caught:
            DocumentService.send_for_signature(document, provider="broken")

        self.assertIn("the API is down", str(caught.exception))
        document.refresh_from_db()
        self.assertEqual(document.status, DocumentStatus.DRAFT)
        self.assertIsNone(document.number)
        self.assertEqual(document.signature_requests.count(), 0)

    def test_a_failed_send_uses_no_document_number(self):
        from core.models import DocumentSequence

        class BrokenProvider(FakeProvider):
            name = "broken2"

            def send(self, document, signature_request):
                raise ProviderError("nope")

        PROVIDERS["broken2"] = BrokenProvider
        self.addCleanup(PROVIDERS.pop, "broken2", None)

        with self.assertRaises(DocumentError):
            DocumentService.send_for_signature(self.ready_draft(), provider="broken2")

        self.assertEqual(DocumentSequence.objects.count(), 0)


@override_settings(CLOUD_STORAGE=BUCKET)
class ReconcileTests(SigningProviderBase):
    def test_a_signature_from_the_provider_is_recorded(self):
        document = self.sent_document()
        request = document.signature_requests.get()

        changes = reconcile(request, fake_state(document, signed=[self.client_email]))

        document.refresh_from_db()
        self.assertEqual(document.status, DocumentStatus.PARTIALLY_SIGNED)
        self.assertEqual(changes, ["Anna Kovács signed"])

    def test_the_last_signature_completes_and_stores_the_provider_files(self):
        document = self.sent_document()
        request = document.signature_requests.get()

        reconcile(
            request,
            fake_state(
                document,
                signed=[self.client_email, self.studio_email],
                completed=True,
            ),
        )

        document.refresh_from_db()
        self.assertEqual(document.status, DocumentStatus.ACTIVE)
        self.assertIsNotNone(document.signed_date)
        self.assertEqual(document.executed_file.filename, "signed.pdf")
        self.assertTrue(document.files.filter(kind=DocumentFile.Kind.AUDIT_TRAIL).exists())
        request.refresh_from_db()
        self.assertEqual(request.status, SignatureRequestStatus.COMPLETED)

    def test_replaying_the_same_state_changes_nothing(self):
        document = self.sent_document()
        request = document.signature_requests.get()
        state = fake_state(
            document, signed=[self.client_email, self.studio_email], completed=True
        )

        first = reconcile(request, state)
        second = reconcile(request, state)

        self.assertTrue(first)
        self.assertEqual(second, [])  # idempotent: nothing left to do
        self.assertEqual(document.files.filter(kind=DocumentFile.Kind.EXECUTED).count(), 1)

    def test_a_decline_stops_the_document_and_is_not_overridden(self):
        document = self.sent_document()
        request = document.signature_requests.get()

        reconcile(
            request,
            fake_state(
                document, signed=[self.studio_email], declined=[self.client_email]
            ),
        )

        document.refresh_from_db()
        self.assertEqual(document.status, DocumentStatus.DECLINED)
        self.assertEqual(
            document.signers.get(email=self.client_email).declined_reason, "Clause 7"
        )

    def test_an_unknown_email_is_ignored(self):
        document = self.sent_document()
        request = document.signature_requests.get()

        changes = reconcile(request, fake_state(document, signed=["nobody@elsewhere.com"]))

        document.refresh_from_db()
        self.assertEqual(changes, [])
        self.assertEqual(document.status, DocumentStatus.OUT_FOR_SIGNATURE)

    def test_completion_without_signature_events_still_closes_the_document(self):
        """A missed DOCUMENT_SIGNED must not strand a finished envelope."""
        document = self.sent_document()
        request = document.signature_requests.get()

        reconcile(request, ProviderState(envelope_id="env-123", completed=True))

        document.refresh_from_db()
        self.assertEqual(document.status, DocumentStatus.ACTIVE)
        self.assertTrue(all(s.has_signed for s in document.signers.all()))

    def test_events_are_appended_to_the_audit_log(self):
        document = self.sent_document()
        request = document.signature_requests.get()

        reconcile(
            request,
            fake_state(document, signed=[self.client_email]),
            raw_event={"event": "DOCUMENT_SIGNED"},
        )

        request.refresh_from_db()
        self.assertEqual(len(request.event_log), 1)
        self.assertEqual(request.event_log[0]["event"], "DOCUMENT_SIGNED")
        self.assertIn("received_at", request.event_log[0])


@override_settings(CLOUD_STORAGE=BUCKET, SIGNING=DOCUMENSO_SETTINGS)
class DocumensoWebhookTests(SigningProviderBase):
    """The adapter's translation of Documenso's documented payload, and the
    endpoint's authentication."""

    def setUp(self):
        super().setUp()
        self.document = self.ready_draft()
        DocumentService.send_for_signature(self.document, provider="fake")
        request = self.document.signature_requests.get()
        request.provider = "documenso"
        request.save(update_fields=["provider"])
        self.url = reverse("documents:signing-webhook", args=["documenso"])

    def payload(self, event, recipients, status="PENDING", external_id=""):
        return {
            "event": event,
            "createdAt": timezone.now().isoformat(),
            "payload": {
                "id": "env-123",
                "externalId": external_id or self.document.number,
                "title": self.document.title,
                "status": status,
                "completedAt": timezone.now().isoformat(),
                "recipients": recipients,
            },
        }

    def post(self, body, secret="a-long-random-secret"):
        return self.client.post(
            self.url,
            data=json.dumps(body),
            content_type="application/json",
            headers={"X-Documenso-Secret": secret} if secret is not None else {},
        )

    def recipient(self, email, status="SIGNED"):
        return {"id": "r1", "email": email, "signingStatus": status}

    # ---- authentication ------------------------------------------------

    def test_a_request_without_the_secret_is_refused(self):
        response = self.post(self.payload("DOCUMENT_SIGNED", []), secret=None)

        self.assertEqual(response.status_code, 403)
        self.document.refresh_from_db()
        self.assertEqual(self.document.status, DocumentStatus.OUT_FOR_SIGNATURE)

    def test_a_wrong_secret_is_refused(self):
        self.assertEqual(self.post(self.payload("DOCUMENT_SIGNED", []), secret="x").status_code, 403)

    def test_an_unverified_request_cannot_write_to_the_audit_log(self):
        self.post(self.payload("DOCUMENT_SIGNED", []), secret="wrong")

        self.assertEqual(self.document.signature_requests.get().event_log, [])

    @override_settings(
        SIGNING={**DOCUMENSO_SETTINGS, "DOCUMENSO": {**DOCUMENSO_SETTINGS["DOCUMENSO"], "WEBHOOK_SECRET": ""}}
    )
    def test_an_unconfigured_secret_refuses_everything(self):
        self.assertEqual(self.post(self.payload("DOCUMENT_SIGNED", [])).status_code, 403)

    def test_an_unknown_provider_is_refused(self):
        response = self.client.post(
            reverse("documents:signing-webhook", args=["pigeon"]),
            data="{}",
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)

    def test_get_is_not_allowed(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)

    # ---- translation ---------------------------------------------------

    def test_a_signature_event_records_the_signature(self):
        response = self.post(
            self.payload("DOCUMENT_SIGNED", [self.recipient(self.client_email)])
        )

        self.assertEqual(response.status_code, 200)
        self.document.refresh_from_db()
        self.assertEqual(self.document.status, DocumentStatus.PARTIALLY_SIGNED)
        self.assertEqual(
            self.document.signers.get(email=self.client_email).status, SignerStatus.SIGNED
        )

    def test_the_double_event_on_the_last_signature_is_harmless(self):
        """Documenso fires DOCUMENT_SIGNED and DOCUMENT_COMPLETED together."""
        recipients = [
            self.recipient(self.client_email),
            self.recipient(self.studio_email),
        ]

        self.post(self.payload("DOCUMENT_SIGNED", recipients, status="COMPLETED"))
        self.post(self.payload("DOCUMENT_COMPLETED", recipients, status="COMPLETED"))

        self.document.refresh_from_db()
        self.assertEqual(self.document.status, DocumentStatus.ACTIVE)
        self.assertEqual(self.document.signers.filter(status=SignerStatus.SIGNED).count(), 2)

    def test_a_rejection_declines_the_document(self):
        response = self.post(
            self.payload(
                "DOCUMENT_REJECTED",
                [{"id": "r1", "email": self.client_email, "signingStatus": "REJECTED",
                  "rejectionReason": "Needs legal review"}],
                status="REJECTED",
            )
        )

        self.assertEqual(response.status_code, 200)
        self.document.refresh_from_db()
        self.assertEqual(self.document.status, DocumentStatus.DECLINED)
        self.assertEqual(
            self.document.signers.get(email=self.client_email).declined_reason,
            "Needs legal review",
        )

    def test_an_irrelevant_event_is_acknowledged_and_ignored(self):
        response = self.post(self.payload("DOCUMENT_CREATED", []))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ignored")

    def test_an_envelope_we_do_not_know_is_acknowledged(self):
        body = self.payload("DOCUMENT_SIGNED", [self.recipient(self.client_email)])
        body["payload"]["id"] = "someone-elses-envelope"
        body["payload"]["externalId"] = "NDA-1999-0001"

        response = self.post(body)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "unknown envelope")

    def test_a_malformed_body_is_reported_not_retried_forever(self):
        response = self.client.post(
            self.url,
            data="not json",
            content_type="application/json",
            headers={"X-Documenso-Secret": "a-long-random-secret"},
        )
        self.assertEqual(response.status_code, 400)

    def test_an_envelope_is_matched_by_external_id_as_a_fallback(self):
        request = self.document.signature_requests.get()
        request.provider_envelope_id = ""  # envelope id never stored
        request.save(update_fields=["provider_envelope_id"])
        body = self.payload("DOCUMENT_SIGNED", [self.recipient(self.client_email)])
        body["payload"]["id"] = ""

        self.post(body)

        self.document.refresh_from_db()
        self.assertEqual(self.document.status, DocumentStatus.PARTIALLY_SIGNED)


class DocumensoAdapterUnitTests(TestCase):
    """Pure translation, no database."""

    def state_for(self, event, payload):
        provider = DocumensoProvider()

        class FakeRequest:
            body = json.dumps({"event": event, "payload": payload}).encode()

        return provider.parse_webhook(FakeRequest())

    def test_recipient_statuses_are_translated(self):
        state = self.state_for(
            "DOCUMENT_COMPLETED",
            {
                "id": "e1",
                "status": "COMPLETED",
                "recipients": [
                    {"email": "a@x.com", "signingStatus": "SIGNED"},
                    {"email": "b@x.com", "signingStatus": "NOT_SIGNED"},
                ],
            },
        )

        self.assertTrue(state.completed)
        self.assertEqual(state.envelope_id, "e1")
        self.assertTrue(state.recipients[0].signed)
        self.assertFalse(state.recipients[1].signed)

    def test_a_rejection_is_not_reported_as_completed(self):
        state = self.state_for(
            "DOCUMENT_REJECTED",
            {
                "id": "e1",
                "status": "REJECTED",
                "recipients": [{"email": "a@x.com", "signingStatus": "REJECTED"}],
            },
        )

        self.assertTrue(state.declined)
        self.assertFalse(state.completed)

    def test_unhandled_events_yield_nothing(self):
        self.assertIsNone(self.state_for("DOCUMENT_OPENED_BY_ALIEN", {"id": "e1"}))

    def test_timestamps_are_parsed_as_aware(self):
        state = self.state_for(
            "DOCUMENT_SIGNED",
            {
                "id": "e1",
                "recipients": [
                    {"email": "a@x.com", "signingStatus": "SIGNED",
                     "signedAt": "2026-03-04T08:22:11Z"}
                ],
            },
        )

        signed_at = state.recipients[0].signed_at
        self.assertIsNotNone(signed_at)
        self.assertTrue(timezone.is_aware(signed_at))

    def test_an_unreachable_api_is_a_provider_error(self):
        with override_settings(SIGNING=DOCUMENSO_SETTINGS):
            with self.assertRaises(ProviderError):
                DocumensoProvider()._request("GET", "/envelope/x")


@override_settings(CLOUD_STORAGE=BUCKET)
class SyncSignaturesCommandTests(SigningProviderBase):
    def run_command(self, **options):
        import io

        from django.core.management import call_command

        output = io.StringIO()
        call_command("sync_signatures", stdout=output, stderr=output, **options)
        return output.getvalue()

    def test_polling_applies_what_a_missed_webhook_would_have(self):
        document = self.sent_document()
        FakeProvider.state = fake_state(
            document, signed=[self.client_email, self.studio_email], completed=True
        )

        output = self.run_command()

        document.refresh_from_db()
        self.assertEqual(document.status, DocumentStatus.ACTIVE)
        self.assertIn("signed", output)

    def test_manual_send_outs_are_not_polled(self):
        self.ready_draft()
        document = self.ready_draft()
        DocumentService.send_for_signature(document, provider="manual")

        output = self.run_command()

        self.assertIn("Checked 0 open send-out(s)", output)

    def test_an_unreachable_provider_is_reported_not_fatal(self):
        class DownProvider(FakeProvider):
            name = "down"

            def fetch_state(self, signature_request):
                raise ProviderError("timed out")

        PROVIDERS["down"] = DownProvider
        self.addCleanup(PROVIDERS.pop, "down", None)
        DocumentService.send_for_signature(self.ready_draft(), provider="down")

        output = self.run_command()

        self.assertIn("timed out", output)
        self.assertIn("1 unreachable", output)

    def test_a_finished_send_out_is_no_longer_polled(self):
        document = self.sent_document()
        FakeProvider.state = fake_state(
            document, signed=[self.client_email, self.studio_email], completed=True
        )
        self.run_command()

        FakeProvider.state = None
        self.assertIn("Checked 0 open send-out(s)", self.run_command())


class WebhookRoutingTests(TestCase):
    def test_the_health_route_is_not_shadowed_by_the_provider_route(self):
        response = self.client.get(reverse("documents:signing-health"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"ok")
