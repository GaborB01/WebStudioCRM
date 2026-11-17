"""Store existing contact addresses the way new ones are stored: lowercased
and trimmed, so lookups by address find them.
"""

from django.db import migrations


def lowercase_emails(apps, schema_editor):
    Contact = apps.get_model("clients", "Contact")
    for contact in Contact.objects.only("email"):
        normalized = contact.email.strip().lower()
        if normalized != contact.email:
            contact.email = normalized
            contact.save(update_fields=["email"])


class Migration(migrations.Migration):
    dependencies = [("clients", "0006_contact_email_lookups")]

    # Not reversible in content (the original casing is gone), but harmless
    # to step back over.
    operations = [migrations.RunPython(lowercase_emails, migrations.RunPython.noop)]
