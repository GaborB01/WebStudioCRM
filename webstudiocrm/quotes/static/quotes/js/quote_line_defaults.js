"use strict";
{
    const $ = django.jQuery;

    // Fields a line takes from its service item. The endpoint returns exactly
    // what QuoteLineItem.fill_defaults() would apply when the quote is saved.
    const SERVICE_FIELDS = ["description", "unit_price", "unit", "billing_type", "vat_rate"];

    function endpoint() {
        const config = document.getElementById("quote-line-defaults");
        return config && config.dataset.url;
    }

    // Inline fields are named "items-<n>-<field>"; find a sibling of this row.
    function rowField(serviceSelect, fieldName) {
        const prefix = serviceSelect.name.match(/^(.*-)service_item$/);
        // "__prefix__" is the hidden row Django clones for "Add another";
        // filling it would leak the values into every new row.
        if (!prefix || prefix[1].includes("__prefix__")) {
            return null;
        }
        return document.querySelector(`[name="${prefix[1]}${fieldName}"]`);
    }

    // Only fill a field that is empty, or that still holds the value we put
    // there for the previously chosen service - never overwrite typed input.
    function isSafeToFill(field) {
        return !field.value || field.value === field.dataset.filledFromService;
    }

    function hasOption(select, value) {
        return Array.from(select.options).some((option) => option.value === value);
    }

    function applyDefaults(serviceSelect, data) {
        for (const fieldName of SERVICE_FIELDS) {
            const field = rowField(serviceSelect, fieldName);
            if (!field || !isSafeToFill(field)) {
                continue;
            }
            const value = data[fieldName] || "";
            if (field.tagName === "SELECT" && value && !hasOption(field, value)) {
                continue;
            }
            field.value = value;
            // Remember what we filled so a later service can replace it.
            field.dataset.filledFromService = value;
            $(field).trigger("change");
        }

        // The service has no price in this quote's currency: leave the field
        // empty and say why, rather than failing only on save.
        const priceField = rowField(serviceSelect, "unit_price");
        if (priceField) {
            priceField.placeholder = data.missing_price
                ? `No ${data.currency} price - enter one`
                : "";
        }
    }

    $(document).on("change", 'select[name$="-service_item"]', function () {
        const serviceSelect = this;
        const url = endpoint();
        if (!url || !serviceSelect.value) {
            return;
        }
        $.getJSON(url, { service: serviceSelect.value })
            .done((data) => applyDefaults(serviceSelect, data))
            .fail(() => {
                // Leave the row alone; saving still fills the defaults server-side.
            });
    });
}
