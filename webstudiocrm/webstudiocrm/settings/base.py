import os
from pathlib import Path

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# Application definition

INSTALLED_APPS = [
    # django.contrib.admin with the CRM's own site (menu, dashboard)
    "webstudiocrm.apps.CRMAdminConfig",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # third party
    "django_countries",
    # apps
    "cloudstorage",
    "core",
    "clients",
    "projects",
    "tasks",
    "quotes",
    "documents",
    "communications",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "webstudiocrm.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        # Project-wide overrides (the admin home page) live here
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "webstudiocrm.wsgi.application"

LANGUAGE_CODE = "en-us"

TIME_ZONE = "Europe/Budapest"

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/6.0/howto/static-files/

STATIC_URL = "static/"

# Uploaded files (logos, generated PDFs)
#
# Uploads go to object storage, not to the local media directory. In
# development that store is simulated by cloudstorage.backends.LocalCloudStorage
# (see the app docstring): objects are versioned, private, and only readable
# through time-limited presigned URLs, so the code already behaves the way it
# must against a real bucket.
#
# To move to Cloudflare R2 / S3 / B2 later: `uv add django-storages[s3]`, then
# override STORAGES in the production settings with
# storages.backends.s3.S3Storage (bucket private, default_acl="private",
# querystring_auth=True, file_overwrite=False) and enable bucket versioning.
# Nothing else changes: models store keys, and reads already go through
# storage.open() / storage.url().
STORAGES = {
    "default": {"BACKEND": "cloudstorage.backends.LocalCloudStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

# Electronic signatures.
#
# "manual" collects signatures on paper and records them in the admin; it
# needs no account and is the safe default. Switch PROVIDER to "documenso"
# once the token and webhook secret below are filled in from the environment
# (Documenso Cloud -> Settings -> API tokens / Webhooks).
#
# Documenso does not sign its webhook payloads: it echoes the secret back in
# an X-Documenso-Secret header. Use a long random value and serve the
# webhook over HTTPS only.
SIGNING = {
    "PROVIDER": os.environ.get("SIGNING_PROVIDER", "manual"),
    "DOCUMENSO": {
        "API_BASE": os.environ.get("DOCUMENSO_API_BASE", "https://app.documenso.com/api/v2"),
        "API_TOKEN": os.environ.get("DOCUMENSO_API_TOKEN", ""),
        "WEBHOOK_SECRET": os.environ.get("DOCUMENSO_WEBHOOK_SECRET", ""),
        "TIMEOUT": 30,
    },
}

CLOUD_STORAGE = {
    "BUCKET_NAME": "webstudiocrm-local",
    # Deliberately outside MEDIA_ROOT: nothing here is served by the web
    # server, the same as a private bucket.
    "ROOT": BASE_DIR.parent / "cloud-bucket",
    # How long a presigned URL stays valid.
    "URL_EXPIRY_SECONDS": 900,
    # True = versioned bucket: writing an existing key keeps the old bytes as
    # a previous version. False mirrors file_overwrite=False in
    # django-storages, which suffixes the new key instead.
    "OVERWRITE": True,
}

# Legacy local uploads from before the switch; kept as the source for
# `manage.py import_media_to_cloud`.
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR.parent / "media"

# Logging
#
# Everything goes to stderr, where the process manager (systemd, Docker, a
# PaaS) collects it; nothing is written to files inside the project.
# Third-party libraries only report warnings (WeasyPrint's font subsetting
# is chatty at INFO); this project's apps log at INFO, so webhook
# rejections and scheduled-command summaries are visible.
PROJECT_APPS = [
    "cloudstorage", "core", "clients", "projects", "tasks", "quotes", "documents",
    "communications",
]

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "plain": {"format": "{asctime} {levelname} {name}: {message}", "style": "{"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "plain"},
    },
    "root": {"handlers": ["console"], "level": "WARNING"},
    "loggers": {app: {"level": "INFO"} for app in PROJECT_APPS},
}

# Background jobs (Celery)
#
# The broker is Redis. Without CELERY_BROKER_URL in the environment,
# tasks run inline in the calling process (ALWAYS_EAGER): that is how
# development and the tests work, with no Redis to install. Production
# requires the broker (settings/prod.py) and a worker:
#     celery -A webstudiocrm worker --loglevel=info
CELERY_TASK_ALWAYS_EAGER = not os.environ.get("CELERY_BROKER_URL")
# The in-memory transport when running inline: nothing to connect to
CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL") or "memory://localhost/"
CELERY_TASK_EAGER_PROPAGATES = True
CELERY_TIMEZONE = TIME_ZONE
CELERY_TASK_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_RESULT_BACKEND = None
# Only acknowledge a job once it finished, so a worker crash re-runs it
CELERY_TASK_ACKS_LATE = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1

# Mail intake
#
# SOURCE picks where mail is read from:
#   "folder" - a directory of .eml files with inbox/ and sent/ subfolders;
#              the default, for development (no mailbox needed)
#   "imap"   - a real mailbox; read-only, nothing is marked or moved
# `python manage.py sync_mail` reads once; with a Celery worker and beat
# running, SYNC_INTERVAL_SECONDS repeats it automatically.
MAIL = {
    "SOURCE": os.environ.get("MAIL_SOURCE", "folder"),
    "SYNC_INTERVAL_SECONDS": int(os.environ.get("MAIL_SYNC_INTERVAL_SECONDS", "300")),
    "FOLDER": {
        "PATH": os.environ.get("MAIL_FOLDER", str(BASE_DIR.parent / "mail-inbox")),
    },
    "IMAP": {
        "HOST": os.environ.get("IMAP_HOST", ""),
        "PORT": int(os.environ.get("IMAP_PORT", "993")),
        "SSL": os.environ.get("IMAP_SSL", "true").lower() in {"1", "true", "yes", "on"},
        "USER": os.environ.get("IMAP_USER", ""),
        "PASSWORD": os.environ.get("IMAP_PASSWORD", ""),
        "FOLDERS": {
            os.environ.get("IMAP_INBOX", "INBOX"): "in",
            os.environ.get("IMAP_SENT", "Sent"): "out",
        },
        "BATCH": int(os.environ.get("IMAP_BATCH", "200")),
        "TIMEOUT": 30,
    },
}

MAIL["OUTBOX"] = {
    "PATH": os.environ.get("MAIL_OUTBOX", str(BASE_DIR.parent / "mail-outbox")),
}

# Sending mail
#
# Development writes every sent email into MAIL["OUTBOX"]["PATH"] as an .eml
# file instead of contacting a mail server (see communications.mail_backends).
# Production sends over SMTP (settings/prod.py). The From address is the
# Studio Profile's email; there is no separate DEFAULT_FROM_EMAIL to keep
# in step.
EMAIL_BACKEND = os.environ.get(
    "EMAIL_BACKEND", "communications.mail_backends.OutboxFolderBackend"
)
EMAIL_HOST = os.environ.get("EMAIL_HOST", "localhost")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = os.environ.get("EMAIL_USE_TLS", "true").lower() in {"1", "true", "yes", "on"}
EMAIL_USE_SSL = os.environ.get("EMAIL_USE_SSL", "false").lower() in {"1", "true", "yes", "on"}
EMAIL_TIMEOUT = 30

# Run by `celery -A webstudiocrm beat` next to the worker
CELERY_BEAT_SCHEDULE = {
    "sync-mail": {
        "task": "communications.tasks.sync_mail",
        "schedule": MAIL["SYNC_INTERVAL_SECONDS"],
    },
}
