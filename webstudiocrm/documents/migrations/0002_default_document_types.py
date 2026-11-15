"""The document types a web studio normally needs, so the module is usable
straight away. Edit or deactivate them in Admin -> Documents -> Document Types.
"""

from django.db import migrations

DEFAULT_TYPES = [
    # name, prefix, countersigned, term (months), description
    ("Non-Disclosure Agreement", "NDA", True, 24,
     "Mutual confidentiality agreement, usually signed before quoting."),
    ("Development Agreement", "DEV", True, None,
     "The main contract for a build: scope, price, deadlines and handover."),
    ("Service Level Agreement", "SLA", True, 12,
     "Maintenance and support terms: response times, uptime and cover."),
    ("Maintenance Contract", "MNT", True, 12,
     "Recurring maintenance work and its monthly or yearly fee."),
    ("Data Processing Agreement", "DPA", True, None,
     "GDPR Article 28 terms for processing personal data on the client's behalf."),
    ("Statement of Work", "SOW", True, None,
     "Scope for one piece of work under an existing development agreement."),
]


def create_types(apps, schema_editor):
    DocumentType = apps.get_model("documents", "DocumentType")
    for name, prefix, countersigned, months, description in DEFAULT_TYPES:
        DocumentType.objects.get_or_create(
            name=name,
            defaults={
                "prefix": prefix,
                "requires_countersignature": countersigned,
                "default_validity_months": months,
                "description": description,
            },
        )


def remove_types(apps, schema_editor):
    DocumentType = apps.get_model("documents", "DocumentType")
    # Only the untouched seed rows; anything in use is left alone.
    DocumentType.objects.filter(
        name__in=[name for name, *_ in DEFAULT_TYPES], documents__isnull=True
    ).delete()


class Migration(migrations.Migration):
    dependencies = [("documents", "0001_initial")]

    operations = [migrations.RunPython(create_types, remove_types)]
