from django.db import migrations, models


def flag_to_treatment(apps, schema_editor):
    VatRate = apps.get_model("core", "VatRate")
    VatRate.objects.filter(is_reverse_charge=True).update(treatment="eu_reverse_charge")
    VatRate.objects.filter(is_reverse_charge=False, rate=0, is_default=False).exclude(
        note_en=""
    ).update(treatment="outside_eu")


def treatment_to_flag(apps, schema_editor):
    VatRate = apps.get_model("core", "VatRate")
    VatRate.objects.filter(treatment="eu_reverse_charge").update(is_reverse_charge=True)


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0002_default_vat_rates"),
    ]

    operations = [
        migrations.AddField(
            model_name="vatrate",
            name="treatment",
            field=models.CharField(
                choices=[
                    ("standard", "Hungarian VAT"),
                    ("eu_reverse_charge", "EU reverse charge (B2B)"),
                    ("outside_eu", "Outside the EU"),
                ],
                default="standard",
                help_text=(
                    "Which clients this rate is for. EU reverse charge: the client "
                    "accounts for the VAT and needs an EU VAT number."
                ),
                max_length=20,
                verbose_name="VAT Treatment",
            ),
        ),
        migrations.RunPython(flag_to_treatment, treatment_to_flag),
        migrations.RemoveField(
            model_name="vatrate",
            name="is_reverse_charge",
        ),
    ]
