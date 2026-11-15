"""Copies of the client and studio details stored on a document when it goes
out for signature.

A signed agreement must keep showing who the parties were on the day it was
signed, even if the client moves office or the studio changes its name.
"""


def client_snapshot(document):
    client = document.client
    return {
        "name": client.name,
        "address": client.address,
        "country": client.country.code,
        "country_name": client.country.name,
        "tax_number": client.tax_number,
        "eu_vat_number": client.eu_vat_number,
        "signers": [
            {
                "name": signer.name,
                "email": signer.email,
                "is_studio_side": signer.is_studio_side,
                "signing_order": signer.signing_order,
            }
            for signer in document.signers.all()
        ],
    }


def studio_snapshot(profile):
    return {
        "company_name": profile.company_name,
        "address": profile.address,
        "tax_number": profile.tax_number,
        "eu_vat_number": profile.eu_vat_number,
        "registration_number": profile.registration_number,
        "email": profile.email,
        "phone": profile.phone,
        "website": profile.website,
    }
