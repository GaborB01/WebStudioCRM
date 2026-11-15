"""A local stand-in for an S3-compatible object store (Cloudflare R2, S3, B2).

The point of simulating rather than just writing to MEDIA_ROOT is that cloud
buckets differ from a local disk in ways that change how application code must
be written. This backend reproduces those differences so the code is already
correct when a real bucket is plugged in:

* objects live outside MEDIA_ROOT, so the web server never serves them;
  every read goes through a time-limited presigned URL
* `path()` raises NotImplementedError - there is no local path for an object
  in a bucket, and code that assumes one breaks in production
* the bucket is versioned: saving over a key keeps the previous bytes, and
  deleting a key writes a delete marker instead of destroying history

On disk a key is a directory holding one directory per version:

    <bucket root>/quotes/2026/Q-2026-0001.pdf/
        20260301T101500Z-3f9a1c22/Q-2026-0001.pdf   <- v1
        20260304T082211Z-7bd04e10/Q-2026-0001.pdf   <- v2 (current)

`cloudstorage.models` indexes the same thing in the database so the admin can
show it. Swapping in django-storages later means changing STORAGES in the
settings; no model or call-site changes.
"""

import hashlib
import logging
import mimetypes
import shutil
import uuid
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.exceptions import SuspiciousFileOperation
from django.core.files import File
from django.core.files.storage import Storage
from django.core.files.utils import validate_file_name
from django.db import DatabaseError, transaction
from django.utils import timezone
from django.utils._os import safe_join

logger = logging.getLogger(__name__)

DEFAULT_CONFIG = {
    "BUCKET_NAME": "webstudiocrm-local",
    "ROOT": None,  # filled from settings; defaults to <project>/cloud-bucket
    "URL_EXPIRY_SECONDS": 900,
    # True mirrors a versioned bucket: writing an existing key adds a version.
    # False mirrors django-storages' file_overwrite=False, which instead gives
    # the new upload a suffixed key and so never produces a second version.
    "OVERWRITE": True,
}


def get_config():
    """Current CLOUD_STORAGE settings.

    Read on every operation (never cached on the instance) so tests can point
    the bucket at a temp directory with override_settings.
    """
    config = {**DEFAULT_CONFIG, **getattr(settings, "CLOUD_STORAGE", {})}
    config["ROOT"] = Path(config["ROOT"] or Path(settings.BASE_DIR).parent / "cloud-bucket")
    return config


def new_version_id():
    """Sortable, filesystem-safe version id, e.g. 20260304T082211Z-7bd04e10."""
    return f"{timezone.now().strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"


class ObjectNotFound(FileNotFoundError):
    """The key has no live version (never stored, or delete-marked)."""


class LocalCloudStorage(Storage):
    # ---- configuration -------------------------------------------------------

    @property
    def config(self):
        return get_config()

    @property
    def bucket_name(self):
        return self.config["BUCKET_NAME"]

    @property
    def root(self):
        """Directory holding this bucket's objects."""
        return self.config["ROOT"] / self.bucket_name

    def _normalise_key(self, name):
        key = str(name).replace("\\", "/").lstrip("/")
        if not key:
            raise SuspiciousFileOperation("Empty object key.")
        validate_file_name(key, allow_relative_path=True)
        return key

    def _absolute(self, relative_path):
        """Resolve a path inside the bucket, refusing anything that escapes it."""
        return Path(safe_join(str(self.root), relative_path))

    # ---- Django Storage API --------------------------------------------------

    def get_available_name(self, name, max_length=None):
        key = self._normalise_key(name)
        if not self.config["OVERWRITE"]:
            return super().get_available_name(key, max_length)
        # Versioned bucket: keep the key, the save becomes a new version.
        if max_length and len(key) > max_length:
            raise SuspiciousFileOperation(
                f"Object key is {len(key)} characters, longer than the allowed {max_length}."
            )
        return key

    def save(self, name, content, max_length=None, origin=None, note=""):
        """Store an object, optionally recording why this version was written.

        Same as Storage.save() plus `origin`/`note`, which land on the version
        so the audit trail can tell a repair or a restore apart from an
        ordinary upload.
        """
        if name is None:
            name = content.name
        if not hasattr(content, "chunks"):
            content = File(content, name)
        name = self.get_available_name(name, max_length=max_length)
        name = self._save(name, content, origin=origin, note=note)
        return validate_file_name(name, allow_relative_path=True)

    def _save(self, name, content, origin=None, note=""):
        key = self._normalise_key(name)
        data = b"".join(content.chunks())  # CRM documents are small enough to buffer
        version_id = new_version_id()
        relative_path = f"{key}/{version_id}/{key.rsplit('/', 1)[-1]}"

        destination = self._absolute(relative_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)

        self._record_version(
            key,
            version_id=version_id,
            relative_path=relative_path,
            size=len(data),
            checksum=hashlib.sha256(data).hexdigest(),
            content_type=mimetypes.guess_type(key)[0] or "application/octet-stream",
            origin=origin,
            note=note,
        )
        return key

    def _open(self, name, mode="rb"):
        if "w" in mode or "a" in mode or "+" in mode:
            raise ValueError(
                "Objects in the bucket are immutable; save a new version instead of "
                f"opening {name!r} for writing."
            )
        path = self.live_path(name)
        return File(path.open("rb"), name=name)

    def delete(self, name):
        """Append a delete marker; the stored versions stay for the audit trail.

        Use `purge()` to actually remove the bytes.
        """
        key = self._normalise_key(name)
        self._record_version(key, kind_is_delete_marker=True)

    def exists(self, name):
        """True only when the key's current bytes are actually readable.

        Checks the disk, not just the index: if the bucket directory was
        deleted or moved while the index rows survived, the key reads as
        missing - so a re-run of import_media_to_cloud repairs it instead of
        skipping it as "already in the bucket".
        """
        try:
            self.live_path(name)
        except (ObjectNotFound, SuspiciousFileOperation):
            return False
        return True

    def bytes_on_disk(self, relative_path):
        """Whether a version's stored bytes are really in the bucket."""
        if not relative_path:
            return False
        try:
            return self._absolute(relative_path).is_file()
        except (SuspiciousFileOperation, ValueError):
            return False

    def size(self, name):
        version = self._live_version(self._normalise_key(name))
        if version is not None:
            return version.size
        return self.live_path(name).stat().st_size

    def url(self, name):
        """A presigned, time-limited URL - the only way to read an object."""
        from .signing import sign_object_url

        return sign_object_url(self._normalise_key(name))

    def path(self, name):
        raise NotImplementedError(
            "Objects live in a bucket and have no local filesystem path. "
            "Open the file through the storage API, or use .url() for a presigned link."
        )

    def get_modified_time(self, name):
        version = self._live_version(self._normalise_key(name))
        if version is not None:
            return version.uploaded_at
        return datetime.fromtimestamp(
            self.live_path(name).stat().st_mtime, tz=timezone.get_current_timezone()
        )

    get_created_time = get_modified_time
    get_accessed_time = get_modified_time

    def listdir(self, path):
        """Directories and keys directly under a prefix."""
        from .models import StoredObject

        prefix = f"{self._normalise_key(path).rstrip('/')}/" if str(path).strip("/") else ""
        directories, files = set(), []
        keys = StoredObject.objects.filter(
            bucket=self.bucket_name, is_deleted=False, key__startswith=prefix
        ).values_list("key", flat=True)
        for key in keys:
            remainder = key[len(prefix) :]
            head, separator, _ = remainder.partition("/")
            if separator:
                directories.add(head)
            else:
                files.append(remainder)
        return sorted(directories), sorted(files)

    # ---- version lookup ------------------------------------------------------

    def _stored_object(self, key):
        from .models import StoredObject

        return StoredObject.objects.filter(bucket=self.bucket_name, key=key).first()

    def _live_version(self, key):
        """Newest content version of a key, or None when the index is unavailable.

        Raises ObjectNotFound if the key is known to have no readable version.
        """
        try:
            stored = self._stored_object(key)
        except DatabaseError:
            # Migrations have not created the index tables yet; fall back to disk.
            return None
        if stored is None:
            if self._disk_versions(key):
                return None  # on disk but unindexed - let the disk fallback handle it
            raise ObjectNotFound(f"No such object: {key}")
        version = stored.live_version
        if version is None:
            raise ObjectNotFound(f"Object is delete-marked: {key}")
        return version

    def _disk_versions(self, key):
        """Version directories on disk, oldest first (version ids sort by time)."""
        try:
            key_directory = self._absolute(key)
        except (SuspiciousFileOperation, ValueError):
            return []
        if not key_directory.is_dir():
            return []
        return sorted(d for d in key_directory.iterdir() if d.is_dir())

    def live_path(self, name, version_id=None):
        """On-disk path of a version's bytes. Internal: callers outside this
        module should go through open()/url() so the code stays portable."""
        key = self._normalise_key(name)
        if version_id is None:
            version = self._live_version(key)
            if version is not None:
                path = self._absolute(version.relative_path)
                if not path.is_file():
                    raise ObjectNotFound(f"Indexed bytes are missing from the bucket: {key}")
                return path
            directories = self._disk_versions(key)
            if not directories:
                raise ObjectNotFound(f"No such object: {key}")
            version_directory = directories[-1]
        else:
            version_directory = self._absolute(f"{key}/{version_id}")
        files = (
            [f for f in version_directory.iterdir() if f.is_file()]
            if version_directory.is_dir()
            else []
        )
        if not files:
            raise ObjectNotFound(f"No such version: {key} ({version_id})")
        return files[0]

    # ---- writing the index ---------------------------------------------------

    def _record_version(
        self,
        key,
        version_id=None,
        relative_path="",
        size=0,
        checksum="",
        content_type="",
        kind_is_delete_marker=False,
        restored_from=None,
        origin=None,
        note="",
    ):
        """Index a new version. Best effort: a bucket whose index is missing is
        still usable, the admin just cannot show it."""
        from .models import StoredObject, StoredObjectVersion

        try:
            with transaction.atomic():
                stored, _ = StoredObject.objects.select_for_update().get_or_create(
                    bucket=self.bucket_name, key=key
                )
                last = stored.versions.order_by("-version_number").first()
                if kind_is_delete_marker and last is None:
                    return None  # nothing to mark as deleted
                version = StoredObjectVersion.objects.create(
                    stored_object=stored,
                    version_id=version_id or new_version_id(),
                    version_number=(last.version_number if last else 0) + 1,
                    kind=(
                        StoredObjectVersion.Kind.DELETE_MARKER
                        if kind_is_delete_marker
                        else StoredObjectVersion.Kind.CONTENT
                    ),
                    size=size,
                    checksum_sha256=checksum,
                    content_type=content_type,
                    relative_path=relative_path,
                    restored_from=restored_from,
                    origin=origin
                    or (
                        StoredObjectVersion.Origin.DELETE
                        if kind_is_delete_marker
                        else StoredObjectVersion.Origin.UPLOAD
                    ),
                    note=note,
                )
                stored.is_deleted = kind_is_delete_marker
                stored.save(update_fields=["is_deleted", "updated_at"])
                return version
        except DatabaseError:
            logger.warning(
                "Cloud storage index unavailable; %r was written to the bucket but not indexed.",
                key,
                exc_info=True,
            )
            return None

    # ---- operations beyond the Storage API -----------------------------------

    def restore_version(self, version):
        """Copy an older version's bytes back as a new current version.

        Non-destructive, like copying an old S3 version over the current key:
        the history keeps growing rather than rewinding.
        """
        if version.is_delete_marker:
            raise ValueError("A delete marker has no content to restore.")
        key = version.stored_object.key
        source = self._absolute(version.relative_path)
        if not source.is_file():
            raise ObjectNotFound(f"The bytes of {key} v{version.version_number} are gone.")

        version_id = new_version_id()
        relative_path = f"{key}/{version_id}/{key.rsplit('/', 1)[-1]}"
        destination = self._absolute(relative_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)

        from .models import StoredObjectVersion

        return self._record_version(
            key,
            version_id=version_id,
            relative_path=relative_path,
            size=version.size,
            checksum=version.checksum_sha256,
            content_type=version.content_type,
            restored_from=version,
            origin=StoredObjectVersion.Origin.RESTORE,
            note=(
                f"Restored from version {version.version_number}, stored "
                f"{version.uploaded_at:%Y-%m-%d %H:%M}. The earlier content was copied "
                f"forward as a new version; no version was removed."
            ),
        )

    def purge(self, stored_object):
        """Permanently remove a key: every version's bytes and its index rows."""
        key_directory = self._absolute(stored_object.key)
        if key_directory.is_dir():
            shutil.rmtree(key_directory)
        stored_object.delete()
