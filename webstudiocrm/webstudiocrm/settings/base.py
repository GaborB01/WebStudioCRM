import os
from pathlib import Path

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# Application definition

INSTALLED_APPS = [
    "django.contrib.admin",
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
        "DIRS": [],
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
