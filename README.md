# WebStudioCRM

A custom, admin-centric CRM system built with Django, specifically tailored for web development studios. It manages the entire lifecycle from initial client acquisition and quoting to production, maintenance contracts, and post-launch monitoring.

## Key Features

- **Client & Contact Management**: Master data tracking for companies (SMBs, Startups, Agencies) with associated primary contacts.
- **Project Pipeline**: Full sales and production tracking from initial inquiry to completion.
- **Task Management**: Granular to-dos with priorities, deadlines, and statuses
- **Quoting**: Line-item quotes with a service catalog, mixed VAT rates (incl. EU reverse charge), HUF/EUR/USD, yearly numbering (`Q-2026-0001`), versioning and English/Hungarian PDFs
- **Django Admin First**: Optimized with `autocomplete_fields`, `list_editable`, and custom filters

## Tech Stack

- **Backend**: Python, Django 6.0.4
- **Package Management**: `uv`
- **Database**: SQLite (Development) / PostgreSQL (Production ready)
- **PDF**: WeasyPrint (needs the Pango system library, e.g. `apt install libpango-1.0-0 libpangoft2-1.0-0`)

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
7. Schedule the daily quote expiry job (e.g. cron):
    ```bash
    python manage.py expire_quotes --settings=webstudiocrm.settings.prod
    ```
