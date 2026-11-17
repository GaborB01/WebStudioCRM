"""One mail sync: read a source, log each message, record the run.

    source.fetch() -> ingest() each -> acknowledge / reject -> MailSyncRun

Overlapping runs are refused: if a run started less than LOCK_MINUTES ago
and has not finished, the new one records itself as skipped and stops.
A run that crashed without finishing stops blocking after that time.
"""

import logging
from datetime import timedelta

from django.utils import timezone

from ..models import MailSyncRun
from .ingest import ingest
from .parsing import ParseError
from .sources import MailSourceError, get_source

logger = logging.getLogger(__name__)

LOCK_MINUTES = 15
MAX_ERROR_LINES = 50


def sync(source=None):
    """Run one sync and return its MailSyncRun."""
    source = source or get_source()
    busy = MailSyncRun.objects.filter(
        source=source.label,
        status=MailSyncRun.Status.RUNNING,
        started_at__gte=timezone.now() - timedelta(minutes=LOCK_MINUTES),
    ).exists()
    if busy:
        return MailSyncRun.objects.create(
            source=source.label, status=MailSyncRun.Status.SKIPPED, finished_at=timezone.now()
        )

    run = MailSyncRun.objects.create(source=source.label)
    errors = []
    try:
        for mail in source.fetch():
            run.fetched += 1
            try:
                result = ingest(mail.raw, direction=mail.direction, fallback_time=mail.received_at)
            except ParseError as error:
                run.failed += 1
                errors.append(f"{mail.key}: {error}")
                source.reject(mail, error)
                continue
            except Exception as error:  # noqa: BLE001 - keep going, retry next sync
                logger.exception("Could not log %s", mail.key)
                run.failed += 1
                errors.append(f"{mail.key}: {type(error).__name__}: {error} (will retry)")
                continue
            source.acknowledge(mail)
            if result.created:
                run.logged += 1
                run.matched += int(result.matched)
            else:
                run.duplicates += 1
        run.status = MailSyncRun.Status.DONE
    except MailSourceError as error:
        run.status = MailSyncRun.Status.FAILED
        errors.append(str(error))
    except Exception as error:  # noqa: BLE001 - recorded, then re-raised for the worker log
        run.status = MailSyncRun.Status.FAILED
        errors.append(f"{type(error).__name__}: {error}")
        raise
    finally:
        source.close()
        run.errors = "\n".join(errors[:MAX_ERROR_LINES])
        run.finished_at = timezone.now()
        run.save()
        logger.info("Mail sync %s: %s", source.label, run.summary)
    return run
