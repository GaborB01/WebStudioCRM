# WebStudioCRM

A custom, admin-centric CRM system built with Django, specifically tailored for web development studios. It manages the entire lifecycle from initial client acquisition and quoting to production, maintenance contracts, and post-launch monitoring.

## Key Features

- **Client & Contact Management**: Master data tracking for companies (SMBs, Startups, Agencies) with associated primary contacts.
- **Project Pipeline**: Full sales and production tracking from initial inquiry to completion.
- **Task Management**: Granular to-dos with priorities, deadlines, and statuses
- **Quoting**: Line-item quotes with a service catalog, mixed VAT rates (incl. EU reverse charge), HUF/EUR/USD, yearly numbering (`Q-2026-0001`), versioning and English/Hungarian PDFs
- **Contracts & Documents**: NDAs, development agreements and SLAs with signers, signed dates, terms, renewals and a full version history; numbered per type (`NDA-2026-0001`)
- **Django Admin First**: Optimized with `autocomplete_fields`, `list_editable`, and custom filters
- **Object Storage**: All uploads go to a versioned, private bucket reached through time-limited presigned URLs. Locally the bucket is simulated on disk, so the code already behaves as it must against Cloudflare R2 / S3

## Tech Stack

- **Backend**: Python, Django 6.0.4
- **Package Management**: `uv`
- **Database**: SQLite (Development) / PostgreSQL (Production ready)
- **PDF**: WeasyPrint (needs the Pango system library, e.g. `apt install libpango-1.0-0 libpangoft2-1.0-0`)
- **File Storage**: `cloudstorage.backends.LocalCloudStorage` — a local simulation of an S3-compatible bucket (no cloud account or extra packages needed)

## File Storage

Uploads (quote PDFs, the studio logo, and contracts later on) are not written to a plain media directory. They go to an object store that is **simulated locally** by the `cloudstorage` app, which reproduces the behaviour that differs from a local disk:

- **Versioned** — saving over a key keeps the previous bytes as an earlier version, and deleting a key writes a *delete marker* instead of destroying history. Any version can be restored.
- **Private** — the bucket lives outside `MEDIA_ROOT` and is never served by the web server. Reads go through a **presigned URL** that expires (15 minutes by default).
- **No local paths** — `storage.path()` raises `NotImplementedError`, exactly as it does on S3, so code that assumes a filesystem path fails here rather than in production.

Browse the bucket in **Admin → Cloud Storage → Stored Objects**: every key, its version history with sizes and SHA-256 checksums, and per-version download links. **Object Versions** adds a *Restore as the current version* action.

Configure it with the `CLOUD_STORAGE` dict in `webstudiocrm/settings/base.py` (bucket name, root directory, URL expiry, versioning on/off).

Migrating files that predate the switch:

```bash
python manage.py import_media_to_cloud --dry-run --settings=webstudiocrm.settings.dev
python manage.py import_media_to_cloud --settings=webstudiocrm.settings.dev
```

**Going to a real bucket** (Cloudflare R2, S3 or B2) is a settings change, not a code change: `uv add django-storages[s3]`, then point `STORAGES["default"]` at `storages.backends.s3.S3Storage` with a private bucket, `querystring_auth=True` and versioning enabled. Models store keys, and all reads already go through `storage.open()` / `storage.url()`.

## Getting Started

1. Clone the repository and navigate into the project root.
2. Set up your virtual environment and install dependencies using `uv`.
3. Run migrations:
   ```bash
   python manage.py migrate --settings=webstudiocrm.settings.dev
   ```
4. Create a superuser:
    ```bash
    python manage.py createsuperuser --settings=webstudiocrm.settings.dev
    ```
5. Run the development server:
    ```bash
    python manage.py runserver --settings=webstudiocrm.settings.dev
    ```
6. Fill in your company details, bank accounts and quote terms in **Admin → Core → Studio Profile**, then add your rates in **Quotes → Service Items**.
7. Schedule the daily jobs (e.g. cron):
    ```bash
    python manage.py expire_quotes --settings=webstudiocrm.settings.prod
    python manage.py review_documents --settings=webstudiocrm.settings.prod
    ```
   `review_documents` activates documents whose start date has arrived, renews or
   expires those past their term, and raises renewal tasks inside the notice window.
   `sync_signatures` (below) is the fallback for missed signature webhooks.

## Electronic Signatures

Signatures are collected through a provider adapter, chosen with the `SIGNING` setting.
Out of the box the provider is `manual` — the studio emails the file and records
signatures in the admin — which needs no account.

**Documenso Cloud** is the supported electronic provider (the hosted service; self-hosting
is not required). To switch:

1. Create an API token and a webhook in Documenso Cloud, then set:
   ```bash
   SIGNING_PROVIDER=documenso
   DOCUMENSO_API_TOKEN=api_...
   DOCUMENSO_WEBHOOK_SECRET=<long random string>
   ```
2. Point the Documenso webhook at `https://<your-host>/webhooks/signing/documenso/`
   and subscribe to *document.signed*, *document.completed*, *document.rejected* and
   *document.cancelled*. `GET /webhooks/signing/health/` confirms the route is reachable.
3. Schedule the fallback poll (hourly is plenty; it is idempotent and safe to run
   alongside live webhooks):
   ```bash
   python manage.py sync_signatures --quiet-if-idle --settings=webstudiocrm.settings.prod
   ```

Documenso does **not** sign webhook payloads — it echoes the configured secret in an
`X-Documenso-Secret` header, which the endpoint compares in constant time. Use a long
random secret and serve the endpoint over HTTPS only.

> Documenso's API v2 is in beta. The adapter follows the published documentation but has
> not been exercised against a live account; send one document with a sandbox token and
> confirm the envelope create/send/download routes in
> `documents/services/signing/documenso.py` before relying on it in production.
