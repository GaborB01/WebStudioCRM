"use strict";
{
    const $ = django.jQuery;

    // Autocomplete fields that should only list objects of the selected
    // client, keyed by "<model_name>.<field_name>" as sent by Django.
    const CLIENT_FILTERED_FIELDS = {
        "project.primary_contact": "id_primary_contact",
        "task.project": "id_project",
    };

    // Django's autocomplete widget doesn't send other form values, so add
    // the selected client to its requests before they go out.
    $.ajaxPrefilter((options) => {
        if (typeof options.data !== "string") {
            return;
        }
        const params = new URLSearchParams(options.data);
        const key = `${params.get("model_name")}.${params.get("field_name")}`;
        const client = document.getElementById("id_client");
        if (key in CLIENT_FILTERED_FIELDS && client && client.value) {
            options.data += `&client=${encodeURIComponent(client.value)}`;
        }
    });

    // A different client invalidates the current contact/project choice.
    $(document).on("change", "#id_client", () => {
        for (const fieldId of Object.values(CLIENT_FILTERED_FIELDS)) {
            $(`#${fieldId}`).val(null).trigger("change");
        }
    });
}
