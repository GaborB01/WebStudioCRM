from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.test import TestCase, override_settings
from django.urls import reverse

from .backends import ObjectNotFound
from .models import StoredObject, StoredObjectVersion
from .signing import sign_object_url
from .testing import temp_bucket

BUCKET = temp_bucket()


@override_settings(CLOUD_STORAGE=BUCKET)
class StorageBackendTests(TestCase):
    def save(self, key="contracts/nda.txt", body=b"v1"):
        return default_storage.save(key, ContentFile(body))

    def test_save_indexes_the_object_with_a_checksum(self):
        key = self.save(body=b"hello")

        stored = StoredObject.objects.get(key=key)
        version = stored.current_version
        self.assertEqual(stored.key, "contracts/nda.txt")
        self.assertEqual(version.version_number, 1)
        self.assertEqual(version.size, 5)
        self.assertEqual(
            version.checksum_sha256,
            "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
        )
        self.assertFalse(stored.is_deleted)

    def test_saving_an_existing_key_adds_a_version_instead_of_overwriting(self):
        self.save(body=b"first")
        key = self.save(body=b"second")

        stored = StoredObject.objects.get(key=key)
        self.assertEqual(stored.versions.count(), 2)
        self.assertEqual(stored.current_version.version_number, 2)
        with default_storage.open(key) as handle:
            self.assertEqual(handle.read(), b"second")
        # the earlier bytes are still in the bucket
        old = stored.versions.get(version_number=1)
        self.assertEqual(default_storage.live_path(key, old.version_id).read_bytes(), b"first")

    def test_delete_writes_a_marker_and_keeps_history(self):
        key = self.save(body=b"payload")
        default_storage.delete(key)

        stored = StoredObject.objects.get(key=key)
        self.assertTrue(stored.is_deleted)
        self.assertTrue(stored.current_version.is_delete_marker)
        self.assertIsNone(stored.live_version)
        self.assertFalse(default_storage.exists(key))
        self.assertEqual(stored.versions.count(), 2)
        with self.assertRaises(ObjectNotFound):
            default_storage.live_path(key)

    def test_restore_copies_an_old_version_forward(self):
        key = self.save(body=b"original")
        self.save(body=b"replacement")
        stored = StoredObject.objects.get(key=key)
        first = stored.versions.get(version_number=1)

        restored = default_storage.restore_version(first)

        self.assertEqual(restored.version_number, 3)
        self.assertEqual(restored.restored_from_id, first.pk)
        with default_storage.open(key) as handle:
            self.assertEqual(handle.read(), b"original")
        # restoring is non-destructive: the replacement is still there
        self.assertEqual(stored.versions.count(), 3)

    def test_restore_brings_back_a_delete_marked_object(self):
        key = self.save(body=b"payload")
        default_storage.delete(key)
        stored = StoredObject.objects.get(key=key)

        default_storage.restore_version(stored.versions.get(version_number=1))

        stored.refresh_from_db()
        self.assertFalse(stored.is_deleted)
        self.assertTrue(default_storage.exists(key))

    def test_purge_removes_every_version(self):
        key = self.save(body=b"a")
        self.save(body=b"b")
        stored = StoredObject.objects.get(key=key)

        default_storage.purge(stored)

        self.assertFalse(StoredObject.objects.filter(key=key).exists())
        self.assertEqual(StoredObjectVersion.objects.count(), 0)
        self.assertFalse(default_storage.exists(key))

    def test_path_is_not_available_like_a_real_bucket(self):
        key = self.save()
        with self.assertRaises(NotImplementedError):
            default_storage.path(key)

    def test_objects_are_immutable(self):
        key = self.save()
        with self.assertRaises(ValueError):
            default_storage.open(key, "wb")

    def test_keys_cannot_escape_the_bucket(self):
        from django.core.exceptions import SuspiciousFileOperation

        with self.assertRaises(SuspiciousFileOperation):
            default_storage.save("../../escape.txt", ContentFile(b"x"))

    def test_size_and_listdir_reflect_the_index(self):
        self.save(key="a/one.txt", body=b"123")
        self.save(key="a/b/two.txt", body=b"45")

        self.assertEqual(default_storage.size("a/one.txt"), 3)
        directories, files = default_storage.listdir("a")
        self.assertEqual((directories, files), (["b"], ["one.txt"]))

    @override_settings(CLOUD_STORAGE={**BUCKET, "OVERWRITE": False})
    def test_without_versioning_a_clashing_key_gets_suffixed(self):
        first = self.save(body=b"one")
        second = self.save(body=b"two")

        self.assertNotEqual(first, second)
        self.assertEqual(StoredObject.objects.count(), 2)


@override_settings(CLOUD_STORAGE=BUCKET)
class PresignedUrlTests(TestCase):
    def setUp(self):
        self.key = default_storage.save("contracts/sla.txt", ContentFile(b"signed"))

    def test_storage_url_is_signed_and_serves_the_object(self):
        url = default_storage.url(self.key)
        self.assertIn("token=", url)

        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), b"signed")

    def test_unsigned_request_is_refused(self):
        response = self.client.get(reverse("cloudstorage:object", kwargs={"key": self.key}))
        self.assertEqual(response.status_code, 403)

    def test_tampered_token_is_refused(self):
        url = default_storage.url(self.key)
        response = self.client.get(url[:-2] + "xy")
        self.assertEqual(response.status_code, 403)

    def test_expired_url_is_refused(self):
        url = sign_object_url(self.key, expires_in=-1)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 403)
        self.assertIn(b"expired", response.content)

    def test_token_cannot_be_reused_for_another_key(self):
        other = default_storage.save("contracts/other.txt", ContentFile(b"secret"))
        token = default_storage.url(self.key).split("token=")[1]

        response = self.client.get(
            reverse("cloudstorage:object", kwargs={"key": other}), {"token": token}
        )

        self.assertEqual(response.status_code, 403)

    def test_a_signed_url_can_target_one_specific_version(self):
        default_storage.save(self.key, ContentFile(b"newer"))
        first = StoredObject.objects.get(key=self.key).versions.get(version_number=1)

        response = self.client.get(sign_object_url(self.key, version_id=first.version_id))

        self.assertEqual(b"".join(response.streaming_content), b"signed")


@override_settings(CLOUD_STORAGE=BUCKET)
class VersionDownloadViewTests(TestCase):
    def setUp(self):
        self.key = default_storage.save("contracts/agreement.txt", ContentFile(b"body"))
        self.version = StoredObject.objects.get(key=self.key).versions.get(version_number=1)
        self.url = reverse("cloudstorage:version-download", args=[self.version.pk])

    def test_staff_are_redirected_to_a_fresh_presigned_url(self):
        self.client.force_login(User.objects.create_superuser("admin", "a@a.hu", "pw"))

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 302)
        self.assertIn("token=", response["Location"])

    def test_anonymous_users_are_sent_to_the_login_page(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])


@override_settings(CLOUD_STORAGE=BUCKET)
class AdminTests(TestCase):
    def setUp(self):
        self.client.force_login(User.objects.create_superuser("admin", "a@a.hu", "pw"))
        self.key = default_storage.save("contracts/nda.txt", ContentFile(b"v1"))
        default_storage.save(self.key, ContentFile(b"v2"))
        self.stored = StoredObject.objects.get(key=self.key)

    def test_object_list_shows_the_bucket_summary(self):
        response = self.client.get(reverse("admin:cloudstorage_storedobject_changelist"))
        self.assertContains(response, "webstudiocrm-local")
        self.assertContains(response, self.key)

    def test_object_page_lists_the_version_history(self):
        response = self.client.get(
            reverse("admin:cloudstorage_storedobject_change", args=[self.stored.pk])
        )
        self.assertContains(response, "v1")
        self.assertContains(response, "v2 (current)")

    def test_restore_action_creates_a_new_current_version(self):
        first = self.stored.versions.get(version_number=1)

        self.client.post(
            reverse("admin:cloudstorage_storedobjectversion_changelist"),
            {"action": "restore_as_current", "_selected_action": [first.pk]},
            follow=True,
        )

        self.assertEqual(self.stored.versions.count(), 3)
        with default_storage.open(self.key) as handle:
            self.assertEqual(handle.read(), b"v1")

    def test_delete_marker_action_keeps_the_versions(self):
        self.client.post(
            reverse("admin:cloudstorage_storedobject_changelist"),
            {"action": "write_delete_marker", "_selected_action": [self.stored.pk]},
            follow=True,
        )

        self.stored.refresh_from_db()
        self.assertTrue(self.stored.is_deleted)
        self.assertEqual(self.stored.versions.count(), 3)


@override_settings(CLOUD_STORAGE=BUCKET)
class IndexDiskDriftTests(TestCase):
    """The bucket directory is deleted while the index rows survive - e.g. a
    cleanup removed cloud-bucket/, or the database travelled to another copy
    of the project. Ghost objects must be detectable and repairable, and the
    admin-facing views must explain instead of crashing."""

    def setUp(self):
        self.key = default_storage.save("quotes/2026/Q-2026-0001.pdf", ContentFile(b"%PDF-1.7"))
        self.stored = StoredObject.objects.get(key=self.key)
        self.version = self.stored.versions.get()
        # Wipe the bucket directory, keep the index - the user's incident.
        import shutil

        shutil.rmtree(default_storage.root)

    def test_a_ghost_object_does_not_count_as_existing(self):
        self.assertFalse(default_storage.exists(self.key))
        self.assertFalse(self.version.bytes_present)

    def test_reimporting_repairs_a_ghost_object(self):
        import tempfile
        from pathlib import Path

        from django.core.management import call_command

        media = Path(tempfile.mkdtemp())
        (media / "quotes/2026").mkdir(parents=True)
        (media / self.key).write_bytes(b"%PDF-1.7")

        import io

        output = io.StringIO()
        with override_settings(MEDIA_ROOT=media):
            call_command("import_media_to_cloud", stdout=output)
        self.assertIn("repair", output.getvalue())

        self.assertTrue(default_storage.exists(self.key))
        self.assertEqual(self.stored.versions.count(), 2)  # the ghost v1 stays as history
        with default_storage.open(self.key) as handle:
            self.assertEqual(handle.read(), b"%PDF-1.7")

    def test_presigned_url_of_a_ghost_is_a_404_not_a_crash(self):
        response = self.client.get(default_storage.url(self.key))
        self.assertEqual(response.status_code, 404)

    def test_version_download_of_a_ghost_explains_the_problem(self):
        self.client.force_login(User.objects.create_superuser("admin", "a@a.hu", "pw"))

        response = self.client.get(
            reverse("cloudstorage:version-download", args=[self.version.pk]), follow=True
        )

        self.assertEqual(response.status_code, 200)  # back on the object's admin page
        self.assertContains(response, "import_media_to_cloud")

    def test_admin_shows_the_missing_bytes(self):
        self.client.force_login(User.objects.create_superuser("admin", "a@a.hu", "pw"))

        changelist = self.client.get(reverse("admin:cloudstorage_storedobject_changelist"))
        detail = self.client.get(
            reverse("admin:cloudstorage_storedobject_change", args=[self.stored.pk])
        )

        self.assertContains(changelist, "bytes missing")
        self.assertContains(detail, "missing")


@override_settings(CLOUD_STORAGE=BUCKET)
class VersionProvenanceTests(TestCase):
    """Every version records why it exists, so a repair is never mistaken for
    an ordinary new upload."""

    def test_a_plain_upload_is_recorded_as_such_with_no_note(self):
        key = default_storage.save("docs/a.txt", ContentFile(b"x"))
        version = StoredObject.objects.get(key=key).versions.get()

        self.assertEqual(version.origin, StoredObjectVersion.Origin.UPLOAD)
        self.assertEqual(version.note, "")

    def test_a_delete_marker_is_recorded_as_a_delete(self):
        key = default_storage.save("docs/b.txt", ContentFile(b"x"))
        default_storage.delete(key)

        marker = StoredObject.objects.get(key=key).current_version
        self.assertEqual(marker.origin, StoredObjectVersion.Origin.DELETE)

    def test_a_restore_explains_which_version_it_came_from(self):
        key = default_storage.save("docs/c.txt", ContentFile(b"one"))
        default_storage.save(key, ContentFile(b"two"))
        first = StoredObject.objects.get(key=key).versions.get(version_number=1)

        restored = default_storage.restore_version(first)

        self.assertEqual(restored.origin, StoredObjectVersion.Origin.RESTORE)
        self.assertIn("Restored from version 1", restored.note)
        self.assertIn("no version was removed", restored.note)


@override_settings(CLOUD_STORAGE=BUCKET)
class RepairProvenanceTests(TestCase):
    """A version written to repair storage drift says what was wrong, what was
    done, and whether the recovered content was verified."""

    KEY = "quotes/2026/Q-2026-0001.pdf"
    BODY = b"%PDF-1.7 original"

    def strand(self, body=None):
        """Index a key, then lose its bytes - the storage drift scenario."""
        import shutil

        default_storage.save(self.KEY, ContentFile(body or self.BODY))
        shutil.rmtree(default_storage.root)
        return StoredObject.objects.get(key=self.KEY)

    def reimport(self, body):
        import io
        import tempfile
        from pathlib import Path

        from django.core.management import call_command

        media = Path(tempfile.mkdtemp())
        (media / self.KEY).parent.mkdir(parents=True, exist_ok=True)
        (media / self.KEY).write_bytes(body)
        output = io.StringIO()
        with override_settings(MEDIA_ROOT=media):
            call_command("import_media_to_cloud", stdout=output)
        return output.getvalue()

    def test_matching_content_is_recorded_as_verified(self):
        stored = self.strand()

        self.reimport(self.BODY)

        repaired = stored.versions.order_by("-version_number").first()
        self.assertEqual(repaired.origin, StoredObjectVersion.Origin.REPAIR)
        self.assertIn("Storage drift repaired", repaired.note)
        self.assertIn("missing from the bucket", repaired.note)
        self.assertIn("Integrity verified", repaired.note)
        self.assertIn("identical to the original", repaired.note)
        self.assertNotIn("FAILED", repaired.note)

    def test_different_content_is_recorded_as_a_failed_check(self):
        stored = self.strand()

        self.reimport(b"%PDF-1.7 DIFFERENT")

        repaired = stored.versions.order_by("-version_number").first()
        self.assertEqual(repaired.origin, StoredObjectVersion.Origin.REPAIR)
        self.assertIn("Integrity check FAILED", repaired.note)
        self.assertIn("must be reviewed", repaired.note)

    def test_the_note_is_printed_by_the_command(self):
        self.strand()

        output = self.reimport(self.BODY)

        self.assertIn("repair", output)
        self.assertIn("Integrity verified", output)

    def test_a_fresh_import_is_not_labelled_a_repair(self):
        output = self.reimport(self.BODY)

        version = StoredObject.objects.get(key=self.KEY).versions.get()
        self.assertEqual(version.origin, StoredObjectVersion.Origin.UPLOAD)
        self.assertEqual(version.note, "")
        self.assertIn("import", output)

    def test_the_admin_shows_the_repair_and_its_reason(self):
        stored = self.strand()
        self.reimport(self.BODY)
        self.client.force_login(User.objects.create_superuser("admin", "a@a.hu", "pw"))

        detail = self.client.get(
            reverse("admin:cloudstorage_storedobject_change", args=[stored.pk])
        )
        changelist = self.client.get(
            reverse("admin:cloudstorage_storedobject_changelist")
        )
        versions = self.client.get(
            reverse("admin:cloudstorage_storedobjectversion_changelist")
        )

        self.assertContains(detail, "Storage drift repaired")
        self.assertContains(detail, "Repaired storage drift")
        self.assertContains(changelist, "OK (repaired)")
        self.assertContains(versions, "Integrity verified")

    def test_repairs_can_be_filtered_in_the_admin(self):
        self.strand()
        self.reimport(self.BODY)
        self.client.force_login(User.objects.create_superuser("admin", "a@a.hu", "pw"))

        response = self.client.get(
            reverse("admin:cloudstorage_storedobjectversion_changelist"),
            {"origin": "repair"},
        )

        self.assertContains(response, "Storage drift repaired")
        self.assertEqual(response.context["cl"].result_count, 1)
