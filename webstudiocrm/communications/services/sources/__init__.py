"""Where mail comes from.

`MAIL["SOURCE"]` in the settings picks the default; sync_mail can name
another. Every source hands over raw bytes plus a direction, so parsing,
threading and matching never depend on where the mail came from.
"""

from django.conf import settings

from .base import IncomingMail, MailSource, MailSourceError
from .folder import FolderMailSource
from .imap import ImapMailSource

SOURCES = {
    FolderMailSource.name: FolderMailSource,
    ImapMailSource.name: ImapMailSource,
}

__all__ = ["IncomingMail", "MailSource", "MailSourceError", "SOURCES", "get_source"]


def get_source(name=None, **overrides):
    """The configured source `name` (default: MAIL["SOURCE"])."""
    config = getattr(settings, "MAIL", {})
    name = name or config.get("SOURCE", FolderMailSource.name)
    try:
        source_class = SOURCES[name]
    except KeyError:
        raise MailSourceError(f"Unknown mail source {name!r}; choose from {', '.join(SOURCES)}.")
    return source_class({**config.get(name.upper(), {}), **overrides})
