"""Renders a quote to PDF with WeasyPrint (HTML template -> PDF)."""

from pathlib import Path

from django.core.files.base import ContentFile
from django.template.loader import render_to_string

from core.models import StudioProfile

from . import calculator
from .document_text import LABELS, format_date, format_money, format_number, format_quantity
from .snapshots import client_snapshot, seller_snapshot


class QuotePDFRenderer:
    template_name = "quotes/quote_pdf.html"

    def __init__(self, quote):
        self.quote = quote

    def render(self):
        """Return the PDF as bytes."""
        from weasyprint import HTML  # imported lazily: loads system libraries

        html = render_to_string(self.template_name, self.get_context())
        return HTML(string=html, base_url=str(Path.cwd())).write_pdf()

    def save(self):
        """Render the PDF and attach it to the quote (does not save the quote)."""
        filename = f"{self.quote.number or f'draft-{self.quote.pk}'}.pdf"
        if self.quote.pdf:
            self.quote.pdf.delete(save=False)
        self.quote.pdf.save(filename, ContentFile(self.render()), save=False)

    def get_context(self):
        quote = self.quote
        language = quote.language
        currency = quote.currency

        # Sent quotes use the details copied at sending; drafts use live data.
        seller = quote.seller_snapshot or seller_snapshot(StudioProfile.get_solo(), currency)
        client = quote.client_snapshot or client_snapshot(quote)

        def money(amount):
            return format_money(amount, currency, language)

        units = LABELS[language]["units"]
        items = list(quote.items.select_related("vat_rate"))
        rows = {"one_off": [], "recurring": [], "optional": []}
        for item in items:
            row = {
                "description_lines": item.description.splitlines(),
                "quantity": format_quantity(item.quantity, language),
                "unit": units.get(item.unit, item.unit),
                "unit_price": money(item.unit_price),
                "discount": (
                    f"{format_number(item.discount_percent, language, 0)}%"
                    if item.discount_percent
                    else ""
                ),
                "vat": f"{format_number(item.vat_percent, language, 0)}%",
                "net": money(item.line_net),
                "per": {
                    "monthly": LABELS[language]["per_month"],
                    "yearly": LABELS[language]["per_year"],
                }.get(item.billing_type, ""),
            }
            if item.is_optional:
                rows["optional"].append(row)
            elif item.billing_type == "one_off":
                rows["one_off"].append(row)
            else:
                rows["recurring"].append(row)

        totals = calculator.calculate(
            [
                calculator.LineInput(
                    i.line_net, i.vat_rate_id, i.vat_percent, i.billing_type, i.is_optional
                )
                for i in items
            ],
            quote.discount_percent,
            currency,
        )
        vat_lines = [
            {
                "label": f"{LABELS[language]['vat']} {format_number(g.vat_percent, language, 0)}%",
                "amount": money(g.vat),
            }
            for g in totals.vat_groups
        ]
        vat_notes = []
        for item in items:
            rate = item.vat_rate
            note = rate.note_hu if language == "hu" else rate.note_en
            if note and note not in vat_notes:
                vat_notes.append(note)

        return {
            "quote": quote,
            "t": LABELS[language],
            "language": language,
            "is_draft": quote.is_editable,
            "seller": seller,
            "client": client,
            "logo_uri": Path(seller["logo_path"]).as_uri() if seller.get("logo_path") else "",
            "issue_date": format_date(quote.issue_date, language),
            "valid_until": format_date(quote.valid_until, language),
            "rows": rows,
            "subtotal": money(quote.subtotal_net),
            "discount_percent": format_number(quote.discount_percent, language, 0),
            "discount_amount": money(quote.discount_amount),
            "net_total": money(quote.net_total),
            "vat_lines": vat_lines,
            "gross_total": money(quote.gross_total),
            "monthly": money(quote.recurring_monthly_net) if quote.recurring_monthly_net else "",
            "yearly": money(quote.recurring_yearly_net) if quote.recurring_yearly_net else "",
            "vat_notes": vat_notes,
        }
