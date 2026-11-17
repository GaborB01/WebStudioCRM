"""Writing to the timeline from other modules.

The quote and document workflows call log_activity() at the moments a
client would notice: a quote went out, a contract was signed. Emails do
not come through here - EmailMessage keeps its own activity row in step
with its match (see EmailMessage.sync_activity).
"""

from ..choices import ActivityKind
from ..models import Activity


def log_activity(kind, *, client=None, project=None, subject=None, summary, details="",
                 actor=None, happened_at=None):
    """Record one timeline entry.

    Either client or project is required; a project implies its client.
    `subject` is the record the entry is about (a Quote, a Document) and
    is only a pointer: the entry stays if the record is deleted later.
    """
    if project is not None and client is None:
        client = project.client
    if client is None:
        raise ValueError("An activity needs a client or a project.")

    activity = Activity(
        kind=kind,
        client=client,
        project=project,
        subject=subject,
        summary=summary[:255],
        details=details,
        created_by=actor,
    )
    if happened_at is not None:
        activity.happened_at = happened_at
    activity.full_clean()
    activity.save()
    return activity


def log_note(client, text, *, project=None, actor=None, happened_at=None):
    """A note typed by a person: a phone call, a meeting, a decision."""
    first_line = text.strip().splitlines()[0] if text.strip() else "Note"
    return log_activity(
        ActivityKind.NOTE,
        client=client,
        project=project,
        summary=first_line,
        details=text,
        actor=actor,
        happened_at=happened_at,
    )
