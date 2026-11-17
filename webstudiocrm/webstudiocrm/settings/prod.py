"""Production settings.

Everything secret or host-specific comes from the environment (or a .env
file next to manage.py); see .env.example for the full list. Missing
required values stop the process at start-up instead of failing on the
first request.

Assumes the app runs behind a reverse proxy (nginx, Caddy, a PaaS router)
that terminates HTTPS and sets X-Forwarded-Proto. Public webhooks - the
signature provider today, inbound email later - must only ever be reached
over HTTPS.
"""

import os

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

from .base import *  # noqa: F403

load_dotenv()


def required(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise ImproperlyConfigured(f"Set the {name} environment variable.")
    return value


def env_list(name, default=""):
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


def env_bool(name, default):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


DEBUG = False

SECRET_KEY = required("SECRET_KEY")

ALLOWED_HOSTS = env_list("ALLOWED_HOSTS")
if not ALLOWED_HOSTS:
    raise ImproperlyConfigured("Set ALLOWED_HOSTS, e.g. crm.example.com.")

# Admin form posts arrive over HTTPS through the proxy
CSRF_TRUSTED_ORIGINS = env_list(
    "CSRF_TRUSTED_ORIGINS", ",".join(f"https://{host}" for host in ALLOWED_HOSTS)
)

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": required("POSTGRES_DB"),
        "USER": required("POSTGRES_USER"),
        "PASSWORD": os.environ.get("POSTGRES_PASSWORD", ""),
        "HOST": os.environ.get("POSTGRES_HOST", "localhost"),
        "PORT": os.environ.get("POSTGRES_PORT", "5432"),
        # Reuse connections between requests; health checks drop dead ones
        "CONN_MAX_AGE": int(os.environ.get("POSTGRES_CONN_MAX_AGE", "60")),
        "CONN_HEALTH_CHECKS": True,
        "OPTIONS": {"sslmode": os.environ.get("POSTGRES_SSLMODE", "prefer")},
    }
}

# ---- HTTPS ------------------------------------------------------------------

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
# Turn off only if the proxy already redirects http -> https
SECURE_SSL_REDIRECT = env_bool("SECURE_SSL_REDIRECT", True)
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
# Start low; raise to a year once HTTPS is known to work on every subdomain
SECURE_HSTS_SECONDS = int(os.environ.get("SECURE_HSTS_SECONDS", "3600"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool("SECURE_HSTS_INCLUDE_SUBDOMAINS", False)
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"

CELERY_BROKER_URL = required("CELERY_BROKER_URL")

# Real mail goes out over SMTP; the outbox folder is for development only
EMAIL_BACKEND = os.environ.get("EMAIL_BACKEND", "django.core.mail.backends.smtp.EmailBackend")
if EMAIL_BACKEND.endswith("smtp.EmailBackend"):
    EMAIL_HOST = required("EMAIL_HOST")
CELERY_TASK_ALWAYS_EAGER = False

# Level for the project's own apps; libraries stay at WARNING
for _logger in LOGGING["loggers"].values():  # noqa: F405
    _logger["level"] = os.environ.get("LOG_LEVEL", "INFO")
