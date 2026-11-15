"""Test helpers for code that writes to the bucket."""

import tempfile

from django.conf import settings

from .backends import DEFAULT_CONFIG


def temp_bucket(**overrides):
    """CLOUD_STORAGE settings pointing at a throwaway bucket directory.

    Use with override_settings so a test never writes into the real bucket:

        @override_settings(CLOUD_STORAGE=temp_bucket())
        class QuoteWorkflowTests(TestCase):
            ...
    """
    config = {
        **DEFAULT_CONFIG,
        **getattr(settings, "CLOUD_STORAGE", {}),
        "ROOT": tempfile.mkdtemp(prefix="test-bucket-"),
        **overrides,
    }
    return config
