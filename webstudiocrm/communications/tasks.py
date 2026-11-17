"""Background jobs for the communications app.

Each task takes ids, not objects, and re-reads what it needs, so a job
queued before a transaction commits still sees the final state. Every
task returns the report as a dict; in development, where jobs run inline,
the admin shows it straight away.
"""

import logging

from celery import shared_task

from .models import EmailMessage
from .services import rematch as scopes

logger = logging.getLogger(__name__)


@shared_task
def rematch_messages(message_ids, reason=""):
    """Re-match the given emails, e.g. those a deleted contact explained."""
    report = scopes.rematch(EmailMessage.objects.filter(pk__in=message_ids), reason=reason)
    logger.info("Re-matched %s: %s", reason or "selected emails", report.summary())
    return report.as_dict()


@shared_task
def rematch_for_contact(contact_id):
    """Match waiting emails involving a contact's addresses."""
    from clients.models import Contact

    contact = Contact.objects.filter(pk=contact_id).first()
    if contact is None:
        return scopes.RematchReport().as_dict()
    report = scopes.rematch(scopes.contact_scope(contact), reason=f"Contact {contact} added")
    logger.info("Re-matched for contact %s: %s", contact, report.summary())
    return report.as_dict()


@shared_task
def rematch_for_client(client_id):
    """Match waiting emails involving a client's contacts or domains."""
    from clients.models import Client

    client = Client.objects.filter(pk=client_id).first()
    if client is None:
        return scopes.RematchReport().as_dict()
    report = scopes.rematch(scopes.client_scope(client), reason=f"Client {client} updated")
    logger.info("Re-matched for client %s: %s", client, report.summary())
    return report.as_dict()


def queue(task, *args):
    """Start a job and describe it for the person who clicked.

    With a broker the job runs in a worker and the admin says so; inline
    (development) it has already finished and the report is shown.
    """
    result = task.delay(*args)
    if result.ready() and isinstance(result.result, dict):
        return result.result["summary"]
    return "Matching started in the background; refresh in a moment to see the result."


@shared_task
def sync_mail(source_name=None):
    """Read new mail from the source and log it (scheduled by celery beat)."""
    from .services.sources import get_source
    from .services.sync import sync

    run = sync(get_source(source_name))
    return {"run": run.pk, "status": run.status, "summary": f"Mail sync: {run.summary}."}


@shared_task
def send_outgoing_email(email_id):
    """Send one queued OutgoingEmail and log the sent copy."""
    from .services.outgoing import deliver

    status = deliver(email_id)
    logger.info("Outgoing email %s: %s", email_id, status)
    return {"email": email_id, "status": status}
