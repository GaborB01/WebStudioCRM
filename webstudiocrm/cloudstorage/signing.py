"""Presigned URLs for the simulated bucket.

A real private bucket is not readable over HTTP; you ask the SDK for a
presigned URL that carries a signature and an expiry, and that URL works for
anyone holding it until it expires. These helpers reproduce that contract with
`django.core.signing`, so application code never builds a plain file URL.
"""

from datetime import timedelta
from urllib.parse import urlencode

from django.core import signing
from django.urls import reverse
from django.utils import timezone

SALT = "cloudstorage.presigned-object"

# Raised by unsign_object_token; callers turn these into 403s.
BadSignature = signing.BadSignature


class SignatureExpired(signing.BadSignature):
    """The presigned URL was valid but its expiry has passed."""


def sign_object_url(key, version_id=None, expires_in=None, download=False):
    """A time-limited URL for a key (or one specific version of it)."""
    from .backends import get_config

    expires_in = get_config()["URL_EXPIRY_SECONDS"] if expires_in is None else expires_in
    expires_at = timezone.now() + timedelta(seconds=expires_in)
    token = signing.dumps(
        {"key": key, "version": version_id, "exp": expires_at.timestamp()}, salt=SALT
    )
    params = {"token": token}
    if download:
        params["download"] = "1"
    return f"{reverse('cloudstorage:object', kwargs={'key': key})}?{urlencode(params)}"


def unsign_object_token(token, key):
    """Return the version id the token grants access to (None = current).

    Raises BadSignature if the token is forged, was issued for a different
    key, or has expired.
    """
    payload = signing.loads(token, salt=SALT)
    if payload.get("key") != key:
        raise BadSignature("Signature was issued for a different key.")
    if payload.get("exp", 0) < timezone.now().timestamp():
        raise SignatureExpired("This link has expired.")
    return payload.get("version")
