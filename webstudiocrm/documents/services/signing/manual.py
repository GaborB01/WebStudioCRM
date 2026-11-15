"""Wet ink and scanned copies: no provider involved.

Signatures are recorded by hand in the admin. This adapter exists so the
manual route is a provider like any other, rather than a special case the
rest of the code has to know about.
"""

from .base import SigningProvider


class ManualProvider(SigningProvider):
    name = "manual"
    requires_configuration = False

    def send(self, document, signature_request):
        # Nothing to call: the studio sends the file itself and records the
        # signatures as they come back.
        return ""
