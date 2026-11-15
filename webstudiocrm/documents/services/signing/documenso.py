"""Documenso Cloud adapter (https://documenso.com) - the hosted service, not
a self-hosted instance.

Everything provider-specific lives in this one file: the routes, the payload
shapes and the webhook check. If Documenso changes its API, or the studio
moves to another provider, nothing outside this module is affected.

Two Documenso facts drive the design:

* Webhooks are NOT signed. Documenso sends the configured secret verbatim in
  the `X-Documenso-Secret` header and expects the receiver to compare it.
  That is weaker than an HMAC, so the endpoint must be HTTPS-only and the
  secret long and random - see verify_webhook().
* The final signature fires DOCUMENT_SIGNED *and* DOCUMENT_COMPLETED, and
  every payload carries the full recipient list. So the adapter reports
  absolute state rather than deltas, and the reconciler is idempotent by
  construction: replaying any event changes nothing.

API v2 is in beta. The routes below follow the published documentation, but
they have not been exercised against a live account from here - smoke-test
`send` once with a sandbox token before relying on it (see README).
"""

import json
import logging
import urllib.error
import urllib.request
from datetime import datetime

from django.conf import settings
from django.utils import timezone

from .base import ProviderError, ProviderState, RecipientState, SigningProvider

logger = logging.getLogger(__name__)

# ---- wire details: the only Documenso-specific constants ---------------------

DEFAULT_API_BASE = "https://app.documenso.com/api/v2"

#: Documenso takes the API token verbatim, with no "Bearer " prefix.
AUTH_HEADER = "Authorization"

SECRET_HEADER = "X-Documenso-Secret"

ENDPOINTS = {
    "create": "/envelope/create",
    "send": "/envelope/{envelope_id}/send",
    "get": "/envelope/{envelope_id}",
    "void": "/envelope/{envelope_id}/cancel",
    "download": "/envelope/{envelope_id}/download",
    "certificate": "/envelope/{envelope_id}/certificate",
}

#: Events we act on. Documenso also emits CREATED / SENT / OPENED, which tell
#: us nothing the CRM does not already know.
COMPLETED_EVENTS = {"DOCUMENT_COMPLETED"}
PROGRESS_EVENTS = {"DOCUMENT_SIGNED", "DOCUMENT_OPENED", "DOCUMENT_SENT"}
DECLINED_EVENTS = {"DOCUMENT_REJECTED"}
VOIDED_EVENTS = {"DOCUMENT_CANCELLED"}
HANDLED_EVENTS = COMPLETED_EVENTS | PROGRESS_EVENTS | DECLINED_EVENTS | VOIDED_EVENTS

SIGNED_STATUSES = {"SIGNED", "COMPLETED"}
DECLINED_STATUSES = {"REJECTED", "DECLINED"}


def _config():
    return {
        "API_BASE": DEFAULT_API_BASE,
        "API_TOKEN": "",
        "WEBHOOK_SECRET": "",
        "TIMEOUT": 30,
        **getattr(settings, "SIGNING", {}).get("DOCUMENSO", {}),
    }


def _parse_timestamp(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if timezone.is_aware(parsed) else timezone.make_aware(parsed)


class DocumensoProvider(SigningProvider):
    name = "documenso"

    @classmethod
    def is_configured(cls):
        return bool(_config()["API_TOKEN"])

    # ---- HTTP ---------------------------------------------------------------

    def _request(self, method, path, payload=None, expect_json=True, **route):
        config = _config()
        if not config["API_TOKEN"]:
            raise ProviderError(
                "No Documenso API token configured (SIGNING['DOCUMENSO']['API_TOKEN'])."
            )
        url = config["API_BASE"].rstrip("/") + path.format(**route)
        body = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(url, data=body, method=method)
        request.add_header(AUTH_HEADER, config["API_TOKEN"])
        if body:
            request.add_header("Content-Type", "application/json")

        try:
            with urllib.request.urlopen(request, timeout=config["TIMEOUT"]) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")[:500]
            raise ProviderError(f"Documenso {method} {path} failed ({error.code}): {detail}")
        except urllib.error.URLError as error:
            raise ProviderError(f"Documenso is unreachable: {error.reason}")

        if not expect_json:
            return raw
        try:
            return json.loads(raw or b"{}")
        except json.JSONDecodeError:
            raise ProviderError(f"Documenso returned a non-JSON body for {path}.")

    def _upload(self, url, content):
        request = urllib.request.Request(
            url, data=content, method="PUT", headers={"Content-Type": "application/pdf"}
        )
        try:
            urllib.request.urlopen(request, timeout=_config()["TIMEOUT"]).close()
        except (urllib.error.HTTPError, urllib.error.URLError) as error:
            raise ProviderError(f"Uploading the document to Documenso failed: {error}")

    # ---- outbound -----------------------------------------------------------

    def send(self, document, signature_request):
        """Create the envelope, upload the file and start the signing round."""
        source = document.files.filter(kind=document.files.model.Kind.SENT).first()
        if source is None:
            raise ProviderError("The document has no file to send.")
        with source.file.open("rb") as handle:
            content = handle.read()

        created = self._request("POST", ENDPOINTS["create"], payload={
            "title": document.title,
            # Lets a webhook be traced back to this CRM record even if the
            # envelope id is ever lost.
            "externalId": document.number or f"document-{document.pk}",
            "recipients": [
                {
                    "email": signer.email,
                    "name": signer.name,
                    "role": "SIGNER",
                    "signingOrder": signer.signing_order,
                }
                for signer in document.signers.all()
            ],
        })

        envelope_id = str(
            created.get("envelopeId") or created.get("id") or ""
        )
        if not envelope_id:
            raise ProviderError(f"Documenso did not return an envelope id: {created}")

        upload_url = created.get("uploadUrl") or created.get("url")
        if upload_url:
            self._upload(upload_url, content)

        self._record_recipient_ids(document, created.get("recipients") or [])
        self._request("POST", ENDPOINTS["send"], payload={}, envelope_id=envelope_id)
        return envelope_id

    @staticmethod
    def _record_recipient_ids(document, recipients):
        by_email = {
            str(r.get("email", "")).lower(): r.get("id")
            for r in recipients
            if r.get("email")
        }
        for signer in document.signers.all():
            recipient_id = by_email.get(signer.email.lower())
            if recipient_id and str(recipient_id) != signer.provider_recipient_id:
                signer.provider_recipient_id = str(recipient_id)
                signer.save(update_fields=["provider_recipient_id"])

    def void(self, signature_request):
        if not signature_request.provider_envelope_id:
            return None
        self._request(
            "POST",
            ENDPOINTS["void"],
            payload={},
            envelope_id=signature_request.provider_envelope_id,
        )
        return None

    # ---- inbound ------------------------------------------------------------

    def fetch_state(self, signature_request):
        if not signature_request.provider_envelope_id:
            return None
        envelope = self._request(
            "GET", ENDPOINTS["get"], envelope_id=signature_request.provider_envelope_id
        )
        return self._to_state(envelope, event=None)

    def verify_webhook(self, request):
        """Constant-time check of the shared secret Documenso echoes back.

        Documenso does not sign payloads, so this is the whole of the
        authentication: keep the secret long, random and off the network by
        serving the endpoint over HTTPS only.
        """
        import hmac

        expected = _config()["WEBHOOK_SECRET"]
        if not expected:
            raise ProviderError(
                "No Documenso webhook secret configured; refusing to trust the request."
            )
        presented = request.headers.get(SECRET_HEADER, "")
        if not hmac.compare_digest(presented, expected):
            raise ProviderError("The webhook secret did not match.")

    def parse_webhook(self, request):
        try:
            body = json.loads(request.body or b"{}")
        except json.JSONDecodeError:
            raise ProviderError("The webhook body was not valid JSON.")
        event = body.get("event")
        if event not in HANDLED_EVENTS:
            return None
        return self._to_state(body.get("payload") or {}, event=event)

    def _to_state(self, envelope, event):
        """Normalise an envelope payload.

        Absolute state, never deltas: Documenso always sends the full
        recipient list, so replaying an event is a no-op.
        """
        status = str(envelope.get("status", "")).upper()
        recipients = tuple(
            RecipientState(
                email=str(recipient.get("email", "")),
                signed=str(recipient.get("signingStatus", "")).upper() in SIGNED_STATUSES,
                declined=str(recipient.get("signingStatus", "")).upper() in DECLINED_STATUSES,
                signed_at=_parse_timestamp(
                    recipient.get("signedAt") or envelope.get("completedAt")
                ),
                decline_reason=str(recipient.get("rejectionReason") or ""),
                provider_recipient_id=str(recipient.get("id") or ""),
            )
            for recipient in envelope.get("recipients") or []
            if recipient.get("email")
        )
        declined = event in DECLINED_EVENTS or status == "REJECTED" or any(
            r.declined for r in recipients
        )
        return ProviderState(
            envelope_id=str(envelope.get("id") or ""),
            completed=(event in COMPLETED_EVENTS or status == "COMPLETED") and not declined,
            declined=declined,
            voided=event in VOIDED_EVENTS or status == "CANCELLED",
            recipients=recipients,
            raw={
                "event": event,
                "status": status,
                # Set to the document number when sending, so a webhook can
                # still be matched if the envelope id was never stored.
                "external_id": str(envelope.get("externalId") or ""),
            },
        )

    # ---- documents ----------------------------------------------------------

    def download_executed(self, signature_request):
        return self._download(signature_request, "download", "signed.pdf")

    def download_certificate(self, signature_request):
        return self._download(signature_request, "certificate", "certificate.pdf")

    def _download(self, signature_request, endpoint, suffix):
        envelope_id = signature_request.provider_envelope_id
        if not envelope_id:
            return None
        try:
            raw = self._request(
                "GET", ENDPOINTS[endpoint], expect_json=False, envelope_id=envelope_id
            )
        except ProviderError as error:
            # A missing certificate must not block recording the signature
            logger.warning("Could not download %s from Documenso: %s", endpoint, error)
            return None
        if raw[:4] != b"%PDF":
            # Some deployments answer with {"downloadUrl": ...} instead
            try:
                url = json.loads(raw).get("downloadUrl")
            except (json.JSONDecodeError, AttributeError):
                url = None
            if not url:
                logger.warning("Documenso %s did not return a PDF.", endpoint)
                return None
            with urllib.request.urlopen(url, timeout=_config()["TIMEOUT"]) as response:
                raw = response.read()
        reference = signature_request.document.number or signature_request.document.pk
        return raw, f"{reference}-{suffix}"
