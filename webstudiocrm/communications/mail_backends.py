"""Django email backends for development.

OutboxFolderBackend writes every outgoing email as an .eml file in a
folder instead of handing it to a mail server - the sending counterpart
of the folder mail source. Open the files in any mail program to see
exactly what a client would receive, headers and attachments included.

    EMAIL_BACKEND = "communications.mail_backends.OutboxFolderBackend"
    MAIL["OUTBOX"]["PATH"]  (default: <project>/mail-outbox)

Bcc recipients are not written into the file (no mail server does that
either); they are listed in an X-Outbox-Bcc header so a developer can
still see who would have received a blind copy.
"""

import re
from pathlib import Path

from django.conf import settings
from django.core.mail.backends.base import BaseEmailBackend
from django.utils import timezone


def outbox_path():
    configured = getattr(settings, "MAIL", {}).get("OUTBOX", {}).get("PATH")
    return Path(configured) if configured else Path(settings.BASE_DIR).parent / "mail-outbox"


class OutboxFolderBackend(BaseEmailBackend):
    def __init__(self, fail_silently=False, path=None, **kwargs):
        super().__init__(fail_silently=fail_silently, **kwargs)
        self.path = Path(path) if path else outbox_path()

    def send_messages(self, email_messages):
        if not email_messages:
            return 0
        self.path.mkdir(parents=True, exist_ok=True)
        sent = 0
        for message in email_messages:
            try:
                mime = message.message()
                if message.bcc:
                    mime["X-Outbox-Bcc"] = ", ".join(message.bcc)
                stamp = timezone.now().strftime("%Y%m%d-%H%M%S-%f")
                slug = re.sub(r"[^A-Za-z0-9]+", "-", message.subject or "no-subject")[:40].strip("-")
                (self.path / f"{stamp}-{slug or 'email'}.eml").write_bytes(mime.as_bytes())
                sent += 1
            except Exception:
                if not self.fail_silently:
                    raise
        return sent
