"""Keep matches honest when the evidence behind them changes.

Deleting a contact (or one of its extra addresses) removes the reason
some emails were matched. Those emails are re-matched in a background
job once the deletion has committed: ones with another explanation move
there, the rest stay where they were, marked stale, for a person to
decide. Nothing is deleted with the contact.
"""

from django.db import transaction
from django.db.models.signals import pre_delete
from django.dispatch import receiver

from clients.models import Contact, ContactEmail

from .choices import MatchMethod
from .models import EmailMessage
from .services.rematch import involving


def _queue_rematch(addresses, reason):
    ids = list(
        involving(addresses)
        .exclude(match_method=MatchMethod.MANUAL)
        .values_list("pk", flat=True)
    )
    if not ids:
        return

    def enqueue():
        from .tasks import rematch_messages

        rematch_messages.delay(ids, reason)

    # After commit: if the delete is rolled back there is nothing to redo,
    # and a worker must not see the contact still in place.
    transaction.on_commit(enqueue)


@receiver(pre_delete, sender=Contact, dispatch_uid="communications.contact_deleted")
def contact_deleted(sender, instance, **kwargs):
    if EmailMessage.objects.exists():
        _queue_rematch(instance.all_emails, f"Contact {instance} was deleted")


@receiver(pre_delete, sender=ContactEmail, dispatch_uid="communications.contact_email_deleted")
def contact_email_deleted(sender, instance, **kwargs):
    # Skip when the whole contact is going: contact_deleted covers it
    if Contact.objects.filter(pk=instance.contact_id).exists():
        _queue_rematch(
            [instance.email], f"Address {instance.email} was removed from {instance.contact}"
        )
