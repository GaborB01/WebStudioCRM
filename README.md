# WebStudioCRM

A custom, admin-centric CRM system built with Django, specifically tailored for web development studios. It manages the entire lifecycle from initial client acquisition and quoting to production, maintenance contracts, and post-launch monitoring.

## Key Features

- **Dashboard**: the admin home page lists only what needs attention - emails to review or
  match, emails that failed or are stuck, a failed mail sync, failed signature requests,
  overdue tasks and quotes about to expire - each linking to exactly those items

- **Client & Contact Management**: Master data tracking for companies (SMBs, Startups, Agencies) with associated primary contacts.
- **Project Pipeline**: Full sales and production tracking from initial inquiry to completion. Every project gets a reference number (`PRJ-2026-0001`) to quote in email subjects.
- **Task Management**: Granular to-dos with priorities, deadlines, and statuses
- **Quoting**: Line-item quotes with a service catalog, mixed VAT rates (incl. EU reverse charge), HUF/EUR/USD, yearly numbering (`Q-2026-0001`), versioning and English/Hungarian PDFs
- **Contracts & Documents**: NDAs, development agreements and SLAs with signers, signed dates, terms, renewals and a full version history; numbered per type (`NDA-2026-0001`)
- **Activity Timeline**: Every client and project page lists what happened: quotes sent and accepted, documents signed, emails exchanged, and notes typed by hand. Emails are logged with their thread, attachments and the original `.eml`, and record which client and project they were matched to and how.
- **Django Admin First**: Optimized with `autocomplete_fields`, `list_editable`, and custom filters
- **Object Storage**: All uploads go to a versioned, private bucket reached through time-limited presigned URLs. Locally the bucket is simulated on disk, so the code already behaves as it must against Cloudflare R2 / S3

## Tech Stack

- **Backend**: Python, Django 6.0.4
- **Background Jobs**: Celery with Redis in production; jobs run inline in development, so no Redis is needed locally
- **Package Management**: `uv`
- **Database**: SQLite (Development) / PostgreSQL (Production ready)
- **PDF**: WeasyPrint (needs the Pango system library, e.g. `apt install libpango-1.0-0 libpangoft2-1.0-0`)
- **File Storage**: `cloudstorage.backends.LocalCloudStorage` — a local simulation of an S3-compatible bucket (no cloud account or extra packages needed)

## File Storage

Uploads (quote PDFs, the studio logo, and contracts later on) are not written to a plain media directory. They go to an object store that is **simulated locally** by the `cloudstorage` app, which reproduces the behaviour that differs from a local disk:

- **Versioned** — saving over a key keeps the previous bytes as an earlier version, and deleting a key writes a *delete marker* instead of destroying history. Any version can be restored.
- **Private** — the bucket lives outside `MEDIA_ROOT` and is never served by the web server. Reads go through a **presigned URL** that expires (15 minutes by default).
- **No local paths** — `storage.path()` raises `NotImplementedError`, exactly as it does on S3, so code that assumes a filesystem path fails here rather than in production.

Browse the bucket in **Admin → System → Stored Files**: every key, its version history with sizes and SHA-256 checksums, and per-version download links. **File Versions** adds a *Restore as the current version* action.

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
6. Fill in your company details, bank accounts and quote terms in **Admin → Settings → Studio Profile**, then add your rates in **Settings → Service Items**.
   For email matching, add each client's company domain on the client page,
   any extra addresses a contact writes from on the contact page, and senders to skip
   (your own domain, newsletters) in **Admin → Settings → Ignored Email Addresses**.
   When you add a contact or a client, leave **Match waiting emails** ticked and the
   unmatched or flagged emails that the new addresses explain are matched in the
   background. The same button sits on every contact and client page for later.
   Deleting a contact never deletes its emails: each one moves to another client if
   something else explains it (with a note), or stays where it was, flagged for review,
   until you confirm, move or delete it.
7. In production, use `--settings=webstudiocrm.settings.prod`. It reads PostgreSQL credentials,
   the Redis broker URL, allowed hosts and HTTPS options from the environment (see
   `webstudiocrm/.env.example`) and refuses to start if a required one is missing. It
   expects a reverse proxy that terminates HTTPS. Run a worker next to the web process:
   ```bash
   celery -A webstudiocrm worker --loglevel=info
   ```
8. Schedule the daily jobs (e.g. cron):
    ```bash
    python manage.py expire_quotes --settings=webstudiocrm.settings.prod
    python manage.py review_documents --settings=webstudiocrm.settings.prod
    ```
   `review_documents` activates documents whose start date has arrived, renews or
   expires those past their term, and raises renewal tasks inside the notice window.
   `sync_signatures` (below) is the fallback for missed signature webhooks.

## Mail Intake

Email is read from a **mail source**, chosen with `MAIL_SOURCE`:

- **`folder`** (default, for development): a directory standing in for a mailbox,
  `webstudiocrm/mail-inbox/` unless `MAIL_FOLDER` says otherwise. Put `.eml` files in
  `inbox/` (received) or `sent/` (sent). After a sync each file is moved to `processed/`,
  or to `failed/` with a `.error.txt` if it is not an email.
- **`imap`**: a real mailbox, read-only (nothing is marked as read, moved or deleted).
  Set `IMAP_HOST`, `IMAP_USER`, `IMAP_PASSWORD` (an app password) and, if your provider
  names it differently, `IMAP_SENT` (Gmail: `[Gmail]/Sent Mail`).

```bash
python manage.py sync_mail --settings=webstudiocrm.settings.dev   # read once
python manage.py mail_demo --settings=webstudiocrm.settings.dev   # demo clients + 16 sample emails
python manage.py mail_demo --reset --settings=webstudiocrm.settings.dev
```

**Sync mail** on the dashboard (or **Mail & Timeline → Inbox & Sent Mail → Sync mail now**) does the same as `sync_mail`. Each run is
listed under **System → Mail Sync Log**. With a worker, `celery -A webstudiocrm beat` repeats the sync
every `MAIL_SYNC_INTERVAL_SECONDS` (default 300). The same message is never logged twice,
so re-running a sync or copying a file back into `inbox/` is safe.

## Sending Email

Write email in **Admin → Mail & Timeline → Write Email**, or start one from where you are:
**Send email** on a client, contact or project page, **Reply** on a logged email, **Email quote**
on a sent quote (the PDF is attached) and **Email document** on a document (its latest file is
attached). Drafts start with the recipient, the signature from **Settings → Studio Profile → Email Signature**
and, for replies, the quoted message.

**Save and send** adds the project number to the subject (`[PRJ-2026-0001] …`) so the client's
answer is matched back to the project, then hands the email to a background job. Once the mail
server accepts it, the sent copy is logged like any other email: in its thread, on the timeline,
with its `.eml` in the bucket.

- **Development**: nothing is sent. Each email is written to `webstudiocrm/mail-outbox/` as an
  `.eml` file (open it in any mail program); `MAIL_OUTBOX` moves the folder.
- **Production**: SMTP. Set `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`
  and `EMAIL_USE_TLS` (see `.env.example`); the sender is the Studio Profile's email address.

Connection problems are retried twice (after 1 and 5 minutes). Anything else - a refused
address, a wrong password - fails at once with the reason shown on the email; fix it and use
**Send again**. An email is never sent twice: if a send is interrupted halfway, it is marked
failed with a note to check whether it arrived.

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
