"""Inbound signature webhooks.

Public endpoint: authenticated by the provider's own scheme, never by a
session. The order below matters - the request is verified before its body
is parsed or logged, so an unauthenticated caller cannot write to the audit
trail.
"""

import json
import logging

from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .choices import SignatureRequestStatus
from .models import SignatureRequest
from .services.signing import ProviderError, get_provider
from .services.signing.reconcile import reconcile

logger = logging.getLogger(__name__)


@csrf_exempt
@require_POST
def signing_webhook(request, provider_name):
    """Receive a provider callback and bring the document up to date.

    Always answers 200 once the caller is authenticated, including when the
    event is irrelevant or already applied: providers retry on anything else,
    and a retry storm over a known event helps nobody.
    """
    try:
        provider = get_provider(provider_name)
    except ProviderError:
        return HttpResponseForbidden("Unknown signing provider.")

    try:
        provider.verify_webhook(request)
    except ProviderError as error:
        logger.warning("Rejected %s webhook: %s", provider_name, error)
        return HttpResponseForbidden("Webhook verification failed.")

    try:
        state = provider.parse_webhook(request)
    except ProviderError as error:
        # Authenticated but malformed: tell the provider, it is their bug
        logger.error("Malformed %s webhook: %s", provider_name, error)
        return JsonResponse({"error": str(error)}, status=400)

    if state is None:
        return JsonResponse({"status": "ignored"})

    signature_request = _find_request(provider_name, state)
    if signature_request is None:
        # Sent from another environment sharing this provider account
        logger.info("No signature request for %s envelope %s", provider_name, state.envelope_id)
        return JsonResponse({"status": "unknown envelope"})

    try:
        raw_event = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        raw_event = {}

    changes = reconcile(signature_request, state, provider=provider, raw_event=raw_event)
    return JsonResponse({"status": "applied", "changes": changes})


def _find_request(provider_name, state):
    """Match the envelope to a send-out, preferring the provider's own id."""
    if state.envelope_id:
        match = SignatureRequest.objects.filter(
            provider=provider_name, provider_envelope_id=state.envelope_id
        ).first()
        if match:
            return match
    # Fall back to the newest open send-out for the referenced document
    reference = (state.raw or {}).get("external_id")
    if reference:
        return (
            SignatureRequest.objects.filter(
                provider=provider_name,
                status=SignatureRequestStatus.SENT,
                document__number=reference,
            )
            .order_by("-sent_at")
            .first()
        )
    return None


def health(request):
    """Confirms the webhook route is reachable, without revealing anything."""
    return HttpResponse("ok", content_type="text/plain")
