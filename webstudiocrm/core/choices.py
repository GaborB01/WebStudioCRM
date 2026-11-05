from django.db import models


class Currency(models.TextChoices):
    HUF = "HUF", "Hungarian Forint (HUF)"
    EUR = "EUR", "Euro (EUR)"
    USD = "USD", "US Dollar (USD)"


class DocumentLanguage(models.TextChoices):
    ENGLISH = "en", "English"
    HUNGARIAN = "hu", "Hungarian"
