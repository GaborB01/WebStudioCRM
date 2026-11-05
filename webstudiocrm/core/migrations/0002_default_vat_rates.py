from decimal import Decimal

from django.db import migrations

# Starting rates for a Hungarian studio. The notes are printed on documents;
# have them checked by your accountant and adjust them in the admin.
DEFAULT_VAT_RATES = [
    {
        "name": "Standard",
        "rate": Decimal("27.00"),
        "is_default": True,
    },
    {
        "name": "EU reverse charge",
        "rate": Decimal("0.00"),
        "is_reverse_charge": True,
        "note_en": (
            "Reverse charge: VAT is to be accounted for by the recipient "
            "(Article 196, Council Directive 2006/112/EC)."
        ),
        "note_hu": (
            "Fordított adózás: az adót a szolgáltatás igénybevevője fizeti "
            "(2006/112/EK irányelv 196. cikk)."
        ),
    },
    {
        "name": "Outside the EU",
        "rate": Decimal("0.00"),
        "note_en": "Not subject to Hungarian VAT: the place of supply is outside the EU.",
        "note_hu": "Az ügylet nem tartozik a magyar áfa hatálya alá: a teljesítés helye az EU-n kívül van.",
    },
]


def create_default_vat_rates(apps, schema_editor):
    VatRate = apps.get_model("core", "VatRate")
    for fields in DEFAULT_VAT_RATES:
        VatRate.objects.get_or_create(name=fields["name"], defaults=fields)


def delete_default_vat_rates(apps, schema_editor):
    VatRate = apps.get_model("core", "VatRate")
    VatRate.objects.filter(name__in=[r["name"] for r in DEFAULT_VAT_RATES]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(create_default_vat_rates, delete_default_vat_rates),
    ]
