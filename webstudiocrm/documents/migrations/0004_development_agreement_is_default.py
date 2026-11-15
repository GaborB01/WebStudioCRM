"""Accepting a quote raises a draft Development Agreement by default.

Change this in Admin -> Documents -> Document Types by moving the
"Raise For Accepted Quotes" tick to another type, or clearing it to turn the
behaviour off.
"""

from django.db import migrations

TYPE_NAME = "Development Agreement"


def set_default(apps, schema_editor):
    DocumentType = apps.get_model("documents", "DocumentType")
    if DocumentType.objects.filter(is_default_for_projects=True).exists():
        return
    DocumentType.objects.filter(name=TYPE_NAME).update(is_default_for_projects=True)


def clear_default(apps, schema_editor):
    DocumentType = apps.get_model("documents", "DocumentType")
    DocumentType.objects.filter(name=TYPE_NAME).update(is_default_for_projects=False)


class Migration(migrations.Migration):
    dependencies = [("documents", "0003_documenttype_is_default_for_projects_and_more")]

    operations = [migrations.RunPython(set_default, clear_default)]
