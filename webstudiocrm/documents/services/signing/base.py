"""The contract every e-signature provider adapter implements.

Providers differ in wire format but not in meaning, so adapters translate
their payloads into the small vocabulary below. Everything downstream - the
webhook endpoint, the polling command, the reconciler - works on these
objects and never touches a provider's JSON.

Adding a provider means writing one adapter and registering it; no change to
the workflow, the models or the admin.
"""

from dataclasses import dataclass, field
from datetime import datetime


class ProviderError(Exception):
    """The provider could not be reached, or refused the request."""


@dataclass(frozen=True)
class RecipientState:
    """What the provider says about one signer."""

    email: str
    signed: bool = False
    declined: bool = False
    signed_at: datetime | None = None
    decline_reason: str = ""
    provider_recipient_id: str = ""


@dataclass(frozen=True)
class ProviderState:
    """A provider's view of one send-out, normalised.

    The same object comes from a webhook and from polling, so both paths
    reconcile through identical code.
    """

    envelope_id: str
    completed: bool = False
    declined: bool = False
    voided: bool = False
    recipients: tuple[RecipientState, ...] = ()
    raw: dict = field(default_factory=dict)

    @property
    def is_finished(self):
        return self.completed or self.declined or self.voided


class SigningProvider:
    """Base class for adapters. Subclasses override what they support."""

    #: Value stored on SignatureRequest.provider
    name = ""
    #: False when the provider needs credentials it has not been given
    requires_configuration = True

    @classmethod
    def is_configured(cls):
        return True

    # ---- outbound -----------------------------------------------------------

    def send(self, document, signature_request):
        """Hand the document to the provider.

        Returns the provider's envelope id (empty string when the provider
        has no concept of one). May set provider_recipient_id on signers.
        """
        raise NotImplementedError

    def void(self, signature_request):
        """Cancel an open send-out. Optional."""
        return None

    # ---- inbound ------------------------------------------------------------

    def fetch_state(self, signature_request):
        """Ask the provider where the send-out stands, or None if it cannot
        be polled (used as the fallback for missed webhooks)."""
        return None

    def parse_webhook(self, request):
        """Turn a verified webhook request into a ProviderState, or None if
        the event carries nothing worth acting on."""
        return None

    def verify_webhook(self, request):
        """Raise ProviderError unless the request really came from the
        provider. Called before the body is parsed or logged."""
        raise ProviderError(f"{self.name} does not accept webhooks.")

    # ---- documents ----------------------------------------------------------

    def download_executed(self, signature_request):
        """(bytes, filename) of the signed PDF, or None."""
        return None

    def download_certificate(self, signature_request):
        """(bytes, filename) of the signing certificate, or None."""
        return None
