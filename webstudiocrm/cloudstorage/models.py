"""Metadata for the simulated object store.

The bytes live on disk under the bucket root; these tables are the bucket's
*index*. They exist so the contents of the bucket - every key, every version,
every delete marker - are visible and auditable in the Django admin, the same
way the S3 console shows object versions.

Written by `cloudstorage.backends.LocalCloudStorage`; never edit by hand.
"""

from django.db import models


def format_bytes(size):
    """Human readable size, e.g. 1.4 MB."""
    if size is None:
        return ""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            precision = 0 if unit == "B" else 1
            return f"{value:.{precision}f} {unit}"
        value /= 1024


class StoredObject(models.Model):
    """One key in the bucket (e.g. `quotes/2026/Q-2026-0001.pdf`).

    A key keeps its identity across uploads: saving over it adds a version
    rather than replacing the object.
    """

    bucket = models.CharField("Bucket", max_length=100)
    key = models.CharField("Key", max_length=500)
    is_deleted = models.BooleanField(
        "Delete Marker",
        default=False,
        help_text="The newest version is a delete marker: the key reads as missing, "
        "but the earlier versions are still stored.",
    )
    created_at = models.DateTimeField("First Stored", auto_now_add=True)
    updated_at = models.DateTimeField("Last Changed", auto_now=True)

    class Meta:
        verbose_name = "Stored File"
        verbose_name_plural = "Stored Files"
        ordering = ["key"]
        constraints = [
            models.UniqueConstraint(fields=["bucket", "key"], name="unique_key_per_bucket")
        ]

    def __str__(self):
        return self.key

    @property
    def current_version(self):
        """The newest version, delete marker included.

        Reads `versions.all()` so a prefetch in the admin is reused.
        """
        return max(self.versions.all(), key=lambda v: v.version_number, default=None)

    @property
    def live_version(self):
        """The newest version that actually holds bytes (None if deleted)."""
        current = self.current_version
        return None if current is None or current.is_delete_marker else current

    @property
    def size(self):
        live = self.live_version
        return live.size if live else 0

    @property
    def total_stored_bytes(self):
        """What the key costs on disk: every version kept, not just the current one."""
        return sum(v.size for v in self.versions.all())


class StoredObjectVersion(models.Model):
    """One immutable upload of a key, or a delete marker.

    Delete markers are versions too (as in S3): deleting a key appends a
    marker instead of destroying the history.
    """

    class Kind(models.TextChoices):
        CONTENT = "content", "Content"
        DELETE_MARKER = "delete_marker", "Delete Marker"

    class Origin(models.TextChoices):
        """Why this version exists.

        Without this, a version written to repair a storage fault is
        indistinguishable from an ordinary re-upload in the audit trail.
        """

        UPLOAD = "upload", "Upload"
        RESTORE = "restore", "Restored earlier version"
        REPAIR = "repair", "Repaired storage drift"
        DELETE = "delete", "Delete marker"

    stored_object = models.ForeignKey(
        StoredObject, on_delete=models.CASCADE, related_name="versions", verbose_name="Object"
    )
    version_id = models.CharField("Version ID", max_length=40)
    version_number = models.PositiveIntegerField("Version")
    kind = models.CharField("Kind", max_length=20, choices=Kind, default=Kind.CONTENT)

    size = models.PositiveBigIntegerField("Size (bytes)", default=0)
    checksum_sha256 = models.CharField("SHA-256", max_length=64, blank=True)
    content_type = models.CharField("Content Type", max_length=100, blank=True)
    relative_path = models.CharField(
        "Path In Bucket",
        max_length=700,
        blank=True,
        help_text="Where the bytes sit under the bucket root. Empty for delete markers.",
    )
    restored_from = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="restores",
        verbose_name="Restored From",
        help_text="Set when this version was created by restoring an older one.",
    )
    origin = models.CharField(
        "Origin", max_length=20, choices=Origin, default=Origin.UPLOAD
    )
    note = models.TextField(
        "Note",
        blank=True,
        help_text="Why this version exists, when it was not a plain upload: "
        "what was wrong, what was done about it, and how it was verified.",
    )
    uploaded_at = models.DateTimeField("Stored At", auto_now_add=True)

    class Meta:
        verbose_name = "File Version"
        verbose_name_plural = "File Versions"
        ordering = ["stored_object", "-version_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["stored_object", "version_number"], name="unique_version_number_per_object"
            ),
            models.UniqueConstraint(
                fields=["stored_object", "version_id"], name="unique_version_id_per_object"
            ),
        ]

    def __str__(self):
        return f"{self.stored_object.key} v{self.version_number}"

    @property
    def is_delete_marker(self):
        return self.kind == self.Kind.DELETE_MARKER

    @property
    def bytes_present(self):
        """Whether this version's bytes are really in the bucket.

        None for delete markers (nothing to check). False means the index and
        the disk have drifted apart - the bucket directory was deleted, moved,
        or belongs to another copy of the project. import_media_to_cloud
        re-imports keys in that state.
        """
        if self.is_delete_marker:
            return None
        from django.core.files.storage import default_storage

        return default_storage.bytes_on_disk(self.relative_path)

    @property
    def is_current(self):
        current = self.stored_object.current_version
        return current is not None and current.pk == self.pk

    @property
    def size_display(self):
        return "" if self.is_delete_marker else format_bytes(self.size)

    @property
    def filename(self):
        return self.stored_object.key.rsplit("/", 1)[-1]
