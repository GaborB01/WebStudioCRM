"""Set `origin` on versions written before the field existed.

Only what the database can prove: delete markers, and versions created by a
restore (they point at the version they were copied from). Everything else
stays "upload" - a repair done before this field existed leaves no trace in
the database to recognise it by.
"""

from django.db import migrations


def set_origin(apps, schema_editor):
    Version = apps.get_model("cloudstorage", "StoredObjectVersion")
    Version.objects.filter(kind="delete_marker").update(origin="delete")
    Version.objects.filter(restored_from__isnull=False).update(origin="restore")


def unset_origin(apps, schema_editor):
    Version = apps.get_model("cloudstorage", "StoredObjectVersion")
    Version.objects.update(origin="upload")


class Migration(migrations.Migration):
    dependencies = [
        ("cloudstorage", "0002_storedobjectversion_note_storedobjectversion_origin"),
    ]

    operations = [migrations.RunPython(set_origin, unset_origin)]
