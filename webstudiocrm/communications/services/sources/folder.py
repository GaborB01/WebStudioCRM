"""A folder of .eml files standing in for a mailbox (development).

    <PATH>/inbox/*.eml       received mail   -> logged as received
    <PATH>/sent/*.eml        sent mail       -> logged as sent
    <PATH>/processed/...     logged (or duplicate) files are moved here
    <PATH>/failed/...        files that are not emails, with a .error.txt

Drop files into inbox/ or sent/ (save an email from any mail program as
.eml, or write one by hand) and run the sync. Moving files out of the
way is how the folder remembers what it has read; copying a processed
file back in is safe, as the message is recognised and not logged twice.
"""

import shutil
from datetime import datetime, timezone as dt_timezone
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from ...choices import Direction
from .base import IncomingMail, MailSource, MailSourceError

FOLDERS = {"inbox": Direction.INBOUND, "sent": Direction.OUTBOUND}


def default_path():
    return Path(settings.BASE_DIR).parent / "mail-inbox"


class FolderMailSource(MailSource):
    name = "folder"

    def __init__(self, config):
        super().__init__(config)
        self.root = Path(config.get("PATH") or default_path())

    @property
    def label(self):
        return f"folder:{self.root}"

    def ensure_layout(self):
        for folder in (*FOLDERS, "processed", "failed"):
            (self.root / folder).mkdir(parents=True, exist_ok=True)

    def fetch(self):
        try:
            self.ensure_layout()
        except OSError as error:
            raise MailSourceError(f"Cannot use {self.root}: {error}")
        # Both folders together, oldest file first, so a sent message is
        # logged before the replies that answer it
        files = [
            (path, direction)
            for folder, direction in FOLDERS.items()
            for path in (self.root / folder).iterdir()
            if path.is_file() and path.suffix.lower() == ".eml"
        ]
        files.sort(key=lambda item: (item[0].stat().st_mtime, item[0].name))
        for path, direction in files:
            received = datetime.fromtimestamp(path.stat().st_mtime, tz=dt_timezone.utc)
            yield IncomingMail(
                key=str(path), raw=path.read_bytes(), direction=direction,
                received_at=received,
            )

    def _move(self, mail, area):
        path = Path(mail.key)
        target_dir = self.root / area / path.parent.name / timezone.localdate().strftime("%Y-%m")
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / path.name
        counter = 1
        while target.exists():  # never overwrite an earlier file of the same name
            target = target_dir / f"{path.stem}-{counter}{path.suffix}"
            counter += 1
        shutil.move(str(path), target)
        return target

    def acknowledge(self, mail):
        self._move(mail, "processed")

    def reject(self, mail, error):
        target = self._move(mail, "failed")
        target.with_name(target.name + ".error.txt").write_text(f"{error}\n", encoding="utf-8")
