"""Provider registry.

`SIGNING["PROVIDER"]` in the settings picks the default; a SignatureRequest
remembers the provider it was sent with, so switching providers never
disturbs documents already out for signature.
"""

from django.conf import settings

from .base import ProviderError, ProviderState, RecipientState, SigningProvider
from .documenso import DocumensoProvider
from .manual import ManualProvider

PROVIDERS = {
    ManualProvider.name: ManualProvider,
    DocumensoProvider.name: DocumensoProvider,
}

__all__ = [
    "ProviderError",
    "ProviderState",
    "RecipientState",
    "SigningProvider",
    "PROVIDERS",
    "get_provider",
    "default_provider_name",
]


def default_provider_name():
    return getattr(settings, "SIGNING", {}).get("PROVIDER", ManualProvider.name)


def get_provider(name=None):
    """The adapter for `name`, or the configured default."""
    name = name or default_provider_name()
    try:
        provider_class = PROVIDERS[name]
    except KeyError:
        raise ProviderError(f"Unknown signing provider {name!r}.")
    return provider_class()
