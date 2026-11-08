"""Copies of client and studio details stored on a quote when it is sent.

A sent quote must keep showing what the client received, even if the
client's address or the studio's bank account changes later.
"""


def client_snapshot(quote):
    client = quote.client
    contact = quote.contact
    return {
        "name": client.name,
        "address": client.address,
        "country": client.country.code,
        "country_name": client.country.name,
        "tax_number": client.tax_number,
        "eu_vat_number": client.eu_vat_number,
        "contact_name": f"{contact.first_name} {contact.last_name}" if contact else "",
        "contact_email": contact.email if contact else "",
    }


def seller_snapshot(profile, currency):
    bank = profile.bank_account_for(currency)
    return {
        "company_name": profile.company_name,
        "address": profile.address,
        "tax_number": profile.tax_number,
        "eu_vat_number": profile.eu_vat_number,
        "registration_number": profile.registration_number,
        "email": profile.email,
        "phone": profile.phone,
        "website": profile.website,
        "logo_path": profile.logo.path if profile.logo else "",
        "bank": {
            "bank_name": bank.bank_name,
            "account_number": bank.account_number,
            "iban": bank.iban,
            "swift_bic": bank.swift_bic,
        }
        if bank
        else None,
    }
