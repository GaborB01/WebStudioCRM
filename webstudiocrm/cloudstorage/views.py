"""Serving objects out of the simulated bucket.

Two endpoints, matching how a private bucket is used in production:

* `object` stands in for the bucket's own HTTP endpoint. It authenticates the
  presigned token, not the user - exactly like S3, a valid unexpired link
  works on its own.
* `version_download` is the staff-only entry point the admin links to. It
  mints a fresh presigned URL and redirects, so links in the admin can never
  go stale and the bucket endpoint is never linked directly.
"""

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.core.files.storage import default_storage
from django.http import FileResponse, Http404, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse

from .backends import ObjectNotFound
from .signing import BadSignature, SignatureExpired, sign_object_url, unsign_object_token


def serve_object(request, key):
    """The bucket endpoint: readable only with a valid presigned token."""
    token = request.GET.get("token")
    if not token:
        return HttpResponseForbidden("This object is private; a presigned URL is required.")
    try:
        version_id = unsign_object_token(token, key)
    except SignatureExpired:
        return HttpResponseForbidden("This link has expired. Open the document again.")
    except BadSignature:
        return HttpResponseForbidden("Invalid signature for this object.")

    try:
        path = default_storage.live_path(key, version_id=version_id)
    except ObjectNotFound as error:
        # Distinguishes "never stored / delete-marked" from "indexed but the
        # bytes are gone from the bucket directory".
        raise Http404(str(error))
    except (NotImplementedError, AttributeError, ValueError):
        raise Http404("No such object in the bucket.")

    return FileResponse(
        path.open("rb"),
        as_attachment=bool(request.GET.get("download")),
        filename=key.rsplit("/", 1)[-1],
    )


@staff_member_required
def version_download(request, pk):
    """Redirect staff to a freshly presigned URL for one specific version."""
    from .models import StoredObjectVersion

    if not request.user.has_perm("cloudstorage.view_storedobject"):
        return HttpResponseForbidden("You do not have access to the bucket.")
    version = get_object_or_404(
        StoredObjectVersion.objects.select_related("stored_object"), pk=pk
    )
    if version.is_delete_marker:
        raise Http404("A delete marker has no content.")
    if not version.bytes_present:
        # Say what is actually wrong instead of redirecting into a 404.
        messages.error(
            request,
            f"The bytes of {version.stored_object.key} (v{version.version_number}) are "
            "missing from the bucket directory: the index and the disk are out of sync. "
            "If the file still exists under media/, re-run "
            "`manage.py import_media_to_cloud` to repair it.",
        )
        return redirect(
            reverse(
                "admin:cloudstorage_storedobject_change", args=[version.stored_object_id]
            )
        )
    return redirect(
        sign_object_url(
            version.stored_object.key,
            version_id=version.version_id,
            download=bool(request.GET.get("download")),
        )
    )
