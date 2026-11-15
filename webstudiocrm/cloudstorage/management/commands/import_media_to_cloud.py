"""One-off migration of legacy MEDIA_ROOT uploads into the bucket.

Files were stored under MEDIA_ROOT before the switch to object storage. The
database keeps paths relative to the storage root (`quotes/2026/Q-...pdf`), so
copying the files across is enough - no rows need updating.

    python manage.py import_media_to_cloud --dry-run --settings=webstudiocrm.settings.dev
    python manage.py import_media_to_cloud --settings=webstudiocrm.settings.dev
"""

import hashlib
from pathlib import Path

from django.conf import settings
from django.core.files.base import File
from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand

from cloudstorage.models import StoredObject, StoredObjectVersion, format_bytes


class Command(BaseCommand):
    help = "Copy files from MEDIA_ROOT into the cloud storage bucket."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List what would be imported without writing anything.",
        )
        parser.add_argument(
            "--overwrite",
            action="store_true",
            help="Import even if the key already exists, adding a new version.",
        )
        parser.add_argument(
            "--delete-source",
            action="store_true",
            help="Delete each file from MEDIA_ROOT after it is imported.",
        )

    @staticmethod
    def _stranded_version(key):
        """The indexed version whose bytes have gone missing from the bucket.

        None when the key is new, or when it is indexed and intact.
        """
        stored = StoredObject.objects.filter(
            bucket=default_storage.bucket_name, key=key
        ).first()
        if stored is None:
            return None
        version = stored.live_version
        if version is None or version.bytes_present:
            return None
        return version

    @staticmethod
    def _provenance(path, stranded):
        """Origin and note to record against the version about to be written."""
        if stranded is None:
            return StoredObjectVersion.Origin.UPLOAD, ""

        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        recorded = stranded.checksum_sha256
        note = (
            f"Storage drift repaired. Version {stranded.version_number} was listed in "
            f"the storage index but its content was missing from the bucket, so the "
            f"object could not be read. The content has been re-imported from the "
            f"local media copy and written as a new version; version "
            f"{stranded.version_number} has been kept in the history as a record of "
            f"the fault. "
        )
        if not recorded:
            return StoredObjectVersion.Origin.REPAIR, note + (
                "No checksum was recorded for the missing version, so the recovered "
                "content could not be verified against it and should be reviewed."
            )
        if digest == recorded:
            return StoredObjectVersion.Origin.REPAIR, note + (
                f"Integrity verified: the SHA-256 of the recovered content matches the "
                f"checksum recorded for version {stranded.version_number} "
                f"({recorded[:12]}...), confirming the content is identical to the "
                f"original."
            )
        return StoredObjectVersion.Origin.REPAIR, note + (
            f"Integrity check FAILED: the SHA-256 of the recovered content "
            f"({digest[:12]}...) does not match the checksum recorded for version "
            f"{stranded.version_number} ({recorded[:12]}...). The recovered content is "
            f"not identical to the original and must be reviewed before it is relied on."
        )

    def handle(self, *args, **options):
        media_root = Path(settings.MEDIA_ROOT)
        if not media_root.is_dir():
            self.stdout.write(self.style.WARNING(f"No media directory at {media_root}; nothing to do."))
            return

        dry_run = options["dry_run"]
        imported = skipped = 0
        total_bytes = 0

        for path in sorted(p for p in media_root.rglob("*") if p.is_file()):
            key = path.relative_to(media_root).as_posix()
            # exists() verifies the bytes on disk, so a key whose bucket files
            # were lost (index row left behind) is re-imported, not skipped.
            if not options["overwrite"] and default_storage.exists(key):
                self.stdout.write(f"  skip   {key} (already in the bucket)")
                skipped += 1
                continue

            # A key already in the index whose bytes are gone is a repair, not
            # a fresh import: the new version has to say why it exists.
            stranded = self._stranded_version(key)
            verb = "repair" if stranded else "import"
            size = path.stat().st_size
            if dry_run:
                self.stdout.write(f"  {verb} {key} ({format_bytes(size)})")
            else:
                origin, note = self._provenance(path, stranded)
                with path.open("rb") as handle:
                    default_storage.save(key, File(handle), origin=origin, note=note)
                self.stdout.write(self.style.SUCCESS(f"  {verb} {key} ({format_bytes(size)})"))
                if stranded:
                    self.stdout.write(f"         {note}")
                if options["delete_source"]:
                    path.unlink()
            imported += 1
            total_bytes += size

        verb = "Would import" if dry_run else "Imported"
        self.stdout.write(
            self.style.SUCCESS(
                f"{verb} {imported} file(s), {format_bytes(total_bytes)}; {skipped} skipped."
            )
        )
        if dry_run:
            self.stdout.write("Dry run: nothing was written. Re-run without --dry-run to apply.")
