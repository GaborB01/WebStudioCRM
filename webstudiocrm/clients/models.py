import re

from django.core.exceptions import ValidationError
from django.db import models
from django_countries.fields import CountryField

from core.choices import EU_COUNTRIES, HOME_COUNTRY, VatTreatment
from core.emails import (
    DOMAIN_PATTERN,
    domain_candidates,
    domain_of,
    is_free_mail,
    normalize_domain,
    normalize_email,
)
from core.models import StudioProfile

# EU VAT numbers start with the country code; Greece uses EL instead of GR
EU_VAT_PREFIXES = {"GR": "EL"}
EU_VAT_NUMBER_PATTERN = re.compile(r"^[A-Z]{2}[0-9A-Z+*]{2,12}$")


class ClientQuerySet(models.QuerySet):
    def with_email_domain(self, address):
        """Clients whose registered domain the address belongs to.

        The fallback when no contact has the address: a stranger writing
        from @acme.com is most likely someone at Acme. Subdomains count,
        so mail.acme.com matches a client registered as acme.com.
        """
        candidates = domain_candidates(domain_of(address))
        if not candidates:
            return self.none()
        return self.filter(email_domains__domain__in=candidates).distinct()


class Client(models.Model):
    STATUS_CHOICES = [
        ("inquiry", "Inquiry"),
        ("active", "Active"),
        ("ghosted", "Ghosted"),
        ("inactive", "Inactive"),
    ]
    COMPANY_TYPE_CHOICES = [
        ("smb", "SMB"),
        ("startup", "Startup"),
        ("sole_trader", "Sole Trader"),
        ("agency", "Agency"),
        ("enterprise", "Enterprise"),
    ]

    name = models.CharField("Company Name", max_length=255)
    tax_number = models.CharField("Tax / VAT Number", max_length=50, blank=True)
    eu_vat_number = models.CharField(
        "EU VAT Number",
        max_length=20,
        blank=True,
        help_text="With country prefix, e.g. DE123456789. Needed for EU reverse charge.",
    )
    address = models.TextField("Billing Address", blank=True)
    country = CountryField("Country", default=HOME_COUNTRY)
    website = models.URLField("Website", blank=True)

    status = models.CharField(
        "Status", max_length=20, choices=STATUS_CHOICES, default="inquiry"
    )
    company_type = models.CharField(
        "Company Type",
        max_length=20,
        choices=COMPANY_TYPE_CHOICES,
        default="smb",
    )

    notes = models.TextField("Internal Notes", blank=True)
    created_at = models.DateTimeField("Created At", auto_now_add=True)
    updated_at = models.DateTimeField("Updated At", auto_now=True)

    objects = ClientQuerySet.as_manager()

    class Meta:
        verbose_name = "Client"
        verbose_name_plural = "Clients"
        ordering = ["-created_at"]

    def __str__(self):
        return self.name

    def clean(self):
        super().clean()
        self.eu_vat_number = self.eu_vat_number.replace(" ", "").upper()
        if not self.eu_vat_number:
            return
        if self.country.code not in EU_COUNTRIES:
            raise ValidationError(
                {"eu_vat_number": "Only clients in the EU have an EU VAT number."}
            )
        prefix = EU_VAT_PREFIXES.get(self.country.code, self.country.code)
        if not (
            EU_VAT_NUMBER_PATTERN.match(self.eu_vat_number)
            and self.eu_vat_number.startswith(prefix)
        ):
            raise ValidationError(
                {"eu_vat_number": f"Must start with {prefix}, e.g. {prefix}12345678."}
            )

    @property
    def vat_treatment(self):
        """Which VAT applies when the studio bills this client.

        EU clients without an EU VAT number (consumers, unregistered
        businesses) are charged Hungarian VAT.
        """
        code = self.country.code
        if code == HOME_COUNTRY:
            return VatTreatment.STANDARD
        if code in EU_COUNTRIES:
            if self.eu_vat_number:
                return VatTreatment.EU_REVERSE_CHARGE
            return VatTreatment.STANDARD
        return VatTreatment.OUTSIDE_EU


class ContactQuerySet(models.QuerySet):
    def with_email(self, address):
        """Contacts reachable at the address, by main or additional email.

        Can return several contacts, even at different clients: one person
        may be a contact for two companies (a freelancer, an agency). The
        caller decides what an ambiguous match means.
        """
        address = normalize_email(address)
        if not address:
            return self.none()
        return self.filter(
            models.Q(email=address) | models.Q(email_addresses__email=address)
        ).distinct()


class Contact(models.Model):
    client = models.ForeignKey(
        Client,
        on_delete=models.CASCADE,
        related_name="contacts",
        verbose_name="Client",
    )

    first_name = models.CharField("First Name", max_length=100)
    last_name = models.CharField("Last Name", max_length=100)
    role = models.CharField(
        "Role / Position",
        max_length=100,
        blank=True,
    )
    email = models.EmailField(
        "Email Address",
        db_index=True,
        help_text="The address quotes and documents go to. Add others the "
        "contact writes from under Other Email Addresses.",
    )
    phone = models.CharField("Phone Number", max_length=30, blank=True)
    is_primary = models.BooleanField("Primary Contact", default=False)
    notes = models.TextField("Notes", blank=True)

    objects = ContactQuerySet.as_manager()

    class Meta:
        verbose_name = "Contact"
        verbose_name_plural = "Contacts"
        ordering = ["last_name", "first_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["client"],
                condition=models.Q(is_primary=True),
                name="unique_primary_contact_per_client",
                violation_error_message="This client already has a primary contact.",
            )
        ]

    def __str__(self):
        return f"{self.first_name} {self.last_name} ({self.client.name})"

    def clean(self):
        super().clean()
        self.email = normalize_email(self.email)

    def save(self, *args, **kwargs):
        # Also here, not only in clean(): code paths that skip validation
        # must not store an address the lookups can't find.
        self.email = normalize_email(self.email)
        super().save(*args, **kwargs)

    @property
    def all_emails(self):
        """Every address the contact is known by, main address first."""
        others = [e.email for e in self.email_addresses.all() if e.email != self.email]
        return [self.email, *others]


class ContactEmail(models.Model):
    """Another address a contact writes from: personal, alias, an old one.

    The contact's own email field stays the address quotes and documents
    are sent to; these are only used to recognise incoming mail.
    """

    contact = models.ForeignKey(
        Contact,
        on_delete=models.CASCADE,
        related_name="email_addresses",
        verbose_name="Contact",
    )
    email = models.EmailField("Email Address", db_index=True)
    label = models.CharField(
        "Label", max_length=50, blank=True, help_text="E.g. personal, old address."
    )
    created_at = models.DateTimeField("Added At", auto_now_add=True)

    class Meta:
        verbose_name = "Other Email Address"
        verbose_name_plural = "Other Email Addresses"
        ordering = ["email"]
        constraints = [
            models.UniqueConstraint(
                fields=["contact", "email"],
                name="unique_email_per_contact",
                violation_error_message="The contact already has this address.",
            )
        ]

    def __str__(self):
        return self.email

    def clean(self):
        super().clean()
        self.email = normalize_email(self.email)
        if self.contact_id and self.email == normalize_email(self.contact.email):
            raise ValidationError({"email": "This is already the contact's main address."})

    def save(self, *args, **kwargs):
        self.email = normalize_email(self.email)
        super().save(*args, **kwargs)


class ClientEmailDomain(models.Model):
    """A domain the client's staff write from, e.g. acme.com.

    Mail from an address nobody is a contact for is matched to the client
    owning its domain. A domain belongs to one client only, and free-mail
    domains (gmail.com etc.) are refused: they identify nobody.
    """

    client = models.ForeignKey(
        Client,
        on_delete=models.CASCADE,
        related_name="email_domains",
        verbose_name="Client",
    )
    domain = models.CharField(
        "Email Domain",
        max_length=253,
        unique=True,
        help_text="Without the @, e.g. acme.com. Subdomains match too.",
        error_messages={"unique": "Another client already has this domain."},
    )

    class Meta:
        verbose_name = "Email Domain"
        verbose_name_plural = "Email Domains"
        ordering = ["domain"]

    def __str__(self):
        return self.domain

    def clean(self):
        super().clean()
        self.domain = normalize_domain(self.domain)
        if not DOMAIN_PATTERN.match(self.domain):
            raise ValidationError({"domain": "Enter a domain such as acme.com."})
        if is_free_mail(self.domain):
            raise ValidationError(
                {"domain": "Anyone can have an address here, so it can't identify a client."}
            )
        studio_domain = domain_of(StudioProfile.get_solo().email)
        if studio_domain and studio_domain in domain_candidates(self.domain):
            raise ValidationError(
                {"domain": "This is the studio's own domain; every sent email would match it."}
            )

    def save(self, *args, **kwargs):
        self.domain = normalize_domain(self.domain)
        super().save(*args, **kwargs)
