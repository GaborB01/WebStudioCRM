"""Admin view of the simulated bucket - the local equivalent of the S3 console.

Everything here is read-only except the three deliberate operations a real
bucket also offers: restore an old version, write a delete marker, and purge
a key for good.
"""

from django.contrib import admin, messages
from django.core.files.storage import default_storage
from django.db.models import Count, Sum
from django.urls import reverse
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from webstudiocrm.admin_site import ListTitleMixin

from .backends import ObjectNotFound
from .models import StoredObject, StoredObjectVersion, format_bytes


def _reason(version):
    """The version's recorded note, flagged when it reports a problem."""
    if not version.note:
        return ""
    failed = "FAILED" in version.note
    return format_html(
        '<span style="{}">{}</span>',
        "color:#ba2121;font-weight:bold" if failed else "color:#444",
        version.note,
    )


def _download_link(version, label=None):
    if version.is_delete_marker:
        return "-"
    url = reverse("cloudstorage:version-download", args=[version.pk])
    # New tab: the admin page stays put while the object opens separately.
    return format_html(
        '<a href="{}" target="_blank" rel="noopener">{}</a>', url, label or version.filename
    )


class StoredObjectVersionInline(admin.TabularInline):
    """The version history of one key, newest first."""

    model = StoredObjectVersion
    extra = 0
    can_delete = False
    fields = (
        "version_label",
        "kind",
        "size_column",
        "checksum_short",
        "content_type",
        "uploaded_at",
        "origin",
        "why",
        "bytes_column",
        "download",
    )
    readonly_fields = fields
    ordering = ("-version_number",)

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    @admin.display(description="Version")
    def version_label(self, obj):
        marker = " (current)" if obj.is_current else ""
        return f"v{obj.version_number}{marker}"

    @admin.display(description="Size")
    def size_column(self, obj):
        return obj.size_display or "-"

    @admin.display(description="SHA-256")
    def checksum_short(self, obj):
        return obj.checksum_sha256[:12] or "-"

    @admin.display(description="Bytes")
    def bytes_column(self, obj):
        present = obj.bytes_present
        if present is None:
            return "-"
        if present:
            return "✓"
        return mark_safe('<strong style="color:#ba2121">missing</strong>')

    @admin.display(description="Why")
    def why(self, obj):
        """The recorded reason, so a repair or restore is never mistaken for
        an ordinary new upload."""
        return _reason(obj)

    @admin.display(description="Download")
    def download(self, obj):
        return _download_link(obj, label="download")


@admin.register(StoredObject)
class StoredObjectAdmin(ListTitleMixin, admin.ModelAdmin):
    list_title = "Stored files"
    list_display = (
        "key",
        "bucket",
        "state",
        "version_count",
        "current_size",
        "stored_size",
        "integrity",
        "updated_at",
        "current_download",
    )
    list_filter = ("bucket", "is_deleted")
    search_fields = ("key",)
    ordering = ("key",)
    inlines = [StoredObjectVersionInline]
    readonly_fields = ("bucket", "key", "state", "current_size", "stored_size",
                       "created_at", "updated_at", "presigned_url")
    fields = readonly_fields
    actions = ["write_delete_marker", "purge_permanently"]

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("versions")

    def has_add_permission(self, request):
        # Objects appear by being written through the storage backend.
        return False

    def has_change_permission(self, request, obj=None):
        return False

    @admin.display(description="State", ordering="is_deleted")
    def state(self, obj):
        return "Delete marker" if obj.is_deleted else "Active"

    @admin.display(description="Versions")
    def version_count(self, obj):
        return len(obj.versions.all())

    @admin.display(description="Size")
    def current_size(self, obj):
        return format_bytes(obj.size) if not obj.is_deleted else "-"

    @admin.display(description="On Disk (all versions)")
    def stored_size(self, obj):
        return format_bytes(obj.total_stored_bytes)

    @admin.display(description="Integrity")
    def integrity(self, obj):
        """Whether the current version's bytes really are in the bucket.

        "bytes missing" means the index survived but the bucket directory did
        not (deleted, moved, or belonging to another copy of the project);
        import_media_to_cloud re-imports such keys.
        """
        version = obj.live_version
        if version is None:
            return "-"
        if not version.bytes_present:
            return mark_safe('<strong style="color:#ba2121">bytes missing</strong>')
        if version.origin == version.Origin.REPAIR:
            return mark_safe('<span style="color:#996a00">OK (repaired)</span>')
        return "OK"

    @admin.display(description="Current")
    def current_download(self, obj):
        version = obj.live_version
        return _download_link(version, label="download") if version else "-"

    @admin.display(description="Presigned URL")
    def presigned_url(self, obj):
        """Shows what application code gets from storage.url() - signed and short-lived."""
        if obj.is_deleted:
            return "-"
        expiry = default_storage.config["URL_EXPIRY_SECONDS"]
        return format_html(
            '<a href="{}" target="_blank" rel="noopener">open</a> '
            '<span style="color:#666">(expires in {} seconds)</span>',
            default_storage.url(obj.key),
            expiry,
        )

    def changelist_view(self, request, extra_context=None):
        """Show bucket-wide totals above the list, like a storage dashboard."""
        totals = StoredObject.objects.aggregate(
            keys=Count("id"), bytes=Sum("versions__size")
        )
        live = StoredObject.objects.filter(is_deleted=False).count()
        extra_context = {
            **(extra_context or {}),
            "title": (
                f"Bucket {default_storage.bucket_name}: {live} live of "
                f"{totals['keys'] or 0} keys, {format_bytes(totals['bytes'] or 0)} stored "
                f"across all versions"
            ),
        }
        return super().changelist_view(request, extra_context)

    @admin.action(description="Write a delete marker (keeps the version history)")
    def write_delete_marker(self, request, queryset):
        marked = 0
        for stored in queryset:
            if stored.is_deleted:
                continue
            default_storage.delete(stored.key)
            marked += 1
        self.message_user(
            request,
            f"Wrote a delete marker on {marked} object(s). The bytes are still in the bucket; "
            f"restore any version to bring a key back.",
            messages.SUCCESS,
        )

    @admin.action(description="Purge permanently (deletes every version's bytes)")
    def purge_permanently(self, request, queryset):
        if not request.user.is_superuser:
            self.message_user(
                request, "Only a superuser can purge objects from the bucket.", messages.ERROR
            )
            return
        keys = [stored.key for stored in queryset]
        for stored in queryset:
            default_storage.purge(stored)
        self.message_user(
            request,
            f"Purged {len(keys)} object(s) and all their versions: {', '.join(keys[:5])}"
            + ("..." if len(keys) > 5 else ""),
            messages.WARNING,
        )


@admin.register(StoredObjectVersion)
class StoredObjectVersionAdmin(ListTitleMixin, admin.ModelAdmin):
    list_title = "File versions"
    """Every version in the bucket - the audit trail, and where restores happen."""

    list_display = (
        "stored_object",
        "version_number",
        "kind",
        "size_column",
        "checksum_short",
        "uploaded_at",
        "current",
        "origin",
        "why",
        "bytes_column",
        "download",
    )
    list_filter = ("origin", "kind", "stored_object__bucket", "content_type")
    search_fields = ("stored_object__key", "version_id", "checksum_sha256", "note")
    ordering = ("-uploaded_at",)
    list_select_related = ("stored_object",)
    actions = ["restore_as_current"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        # Versions are the audit trail; remove a whole key via Stored Objects instead.
        return False

    @admin.display(description="Size")
    def size_column(self, obj):
        return obj.size_display or "-"

    @admin.display(description="SHA-256")
    def checksum_short(self, obj):
        return obj.checksum_sha256[:12] or "-"

    @admin.display(description="Current", boolean=True)
    def current(self, obj):
        return obj.is_current

    @admin.display(description="Why")
    def why(self, obj):
        return _reason(obj)

    @admin.display(description="Bytes")
    def bytes_column(self, obj):
        present = obj.bytes_present
        if present is None:
            return "-"
        if present:
            return "✓"
        return mark_safe('<strong style="color:#ba2121">missing</strong>')

    @admin.display(description="Download")
    def download(self, obj):
        return _download_link(obj, label="download")

    @admin.action(description="Restore as the current version")
    def restore_as_current(self, request, queryset):
        restored, skipped = [], []
        for version in queryset.select_related("stored_object"):
            if version.is_delete_marker:
                skipped.append(f"{version} (delete marker)")
                continue
            try:
                new_version = default_storage.restore_version(version)
            except (ObjectNotFound, ValueError) as error:
                skipped.append(f"{version} ({error})")
                continue
            restored.append(f"{version.stored_object.key}: v{version.version_number} "
                            f"copied to v{new_version.version_number}")
        if restored:
            self.message_user(request, "Restored - " + "; ".join(restored), messages.SUCCESS)
        if skipped:
            self.message_user(request, "Skipped - " + "; ".join(skipped), messages.WARNING)
