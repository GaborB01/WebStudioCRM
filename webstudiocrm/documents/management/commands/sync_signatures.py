"""Polling fallback for missed signature webhooks.

Webhooks are the fast path; this is the safety net. A provider outage, a
deploy during a callback or a firewall hiccup would otherwise leave a
document stuck at "out for signature" forever while the client has already
signed.

    python manage.py sync_signatures --settings=webstudiocrm.settings.prod

Run a few times a day (hourly is plenty). Reconciling is idempotent, so this
is safe to run alongside live webhooks.
"""

from django.core.management.base import BaseCommand

from documents.choices import SignatureRequestStatus
from documents.models import SignatureRequest
from documents.services.signing import ProviderError, get_provider
from documents.services.signing.reconcile import reconcile


class Command(BaseCommand):
    help = "Ask each signing provider about open send-outs and apply what changed."

    def add_arguments(self, parser):
        parser.add_argument(
            "--provider",
            help="Only poll send-outs made with this provider.",
        )
        parser.add_argument(
            "--quiet-if-idle",
            action="store_true",
            help="Print nothing when nothing changed (tidier in cron mail).",
        )

    def handle(self, *args, **options):
        open_requests = SignatureRequest.objects.filter(
            status=SignatureRequestStatus.SENT
        ).select_related("document")
        if options["provider"]:
            open_requests = open_requests.filter(provider=options["provider"])

        checked = updated = failed = 0
        for signature_request in open_requests:
            try:
                provider = get_provider(signature_request.provider)
                state = provider.fetch_state(signature_request)
            except ProviderError as error:
                self.stderr.write(
                    self.style.WARNING(
                        f"{signature_request.document.reference}: {error}"
                    )
                )
                failed += 1
                continue
            if state is None:
                continue  # manual send-outs have nothing to poll
            checked += 1

            changes = reconcile(signature_request, state, provider=provider)
            if changes:
                updated += 1
                self.stdout.write(
                    self.style.SUCCESS(
                        f"{signature_request.document.reference}: {', '.join(changes)}"
                    )
                )

        if options["quiet_if_idle"] and not (updated or failed):
            return
        self.stdout.write(
            f"Checked {checked} open send-out(s); {updated} updated, {failed} unreachable."
        )
