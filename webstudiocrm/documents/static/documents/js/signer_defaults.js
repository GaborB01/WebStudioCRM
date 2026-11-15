"use strict";
{
    const $ = django.jQuery;

    // Fields a signer row takes from the chosen contact. The endpoint returns
    // exactly what DocumentSigner.fill_from_contact() would copy on save.
    const CONTACT_FIELDS = ["name", "email"];

    function endpoint() {
        const config = document.getElementById("signer-defaults");
        return config && config.dataset.url;
    }

    // Signer fields are named "signers-<n>-<field>"; find a sibling of this row.
    function rowField(contactSelect, fieldName) {
        const prefix = contactSelect.name.match(/^(signers-.*-)contact$/);
        // "__prefix__" is the hidden row Django clones for "Add another";
        // filling it would leak the values into every new row.
        if (!prefix || prefix[1].includes("__prefix__")) {
            return null;
        }
        return document.querySelector(`[name="${prefix[1]}${fieldName}"]`);
    }

    // Only fill a field that is empty, or that still holds the value we put
    // there for the previously chosen contact - never overwrite typed input.
    function isSafeToFill(field) {
        return !field.value || field.value === field.dataset.filledFromContact;
    }

    $(document).on("change", 'select[name^="signers-"][name$="-contact"]', function () {
        const contactSelect = this;
        const url = endpoint();
        if (!url || !contactSelect.value) {
            return;
        }
        $.getJSON(url, { contact: contactSelect.value })
            .done((data) => {
                for (const fieldName of CONTACT_FIELDS) {
                    const field = rowField(contactSelect, fieldName);
                    if (!field || !isSafeToFill(field)) {
                        continue;
                    }
                    field.value = data[fieldName] || "";
                    // Remember what we filled so a later contact can replace it.
                    field.dataset.filledFromContact = field.value;
                }
            })
            .fail(() => {
                // Leave the row alone; saving still copies the details server-side.
            });
    });
}
