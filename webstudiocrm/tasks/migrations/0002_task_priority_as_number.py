import django.db.models.expressions
from django.db import migrations, models

PRIORITY_LEVELS = {"low": 1, "medium": 2, "high": 3, "urgent": 4}


def labels_to_numbers(apps, schema_editor):
    Task = apps.get_model("tasks", "Task")
    for label, level in PRIORITY_LEVELS.items():
        Task.objects.filter(priority_label=label).update(priority=level)


def numbers_to_labels(apps, schema_editor):
    Task = apps.get_model("tasks", "Task")
    for label, level in PRIORITY_LEVELS.items():
        Task.objects.filter(priority=level).update(priority_label=label)


class Migration(migrations.Migration):

    dependencies = [
        ("tasks", "0001_initial"),
    ]

    operations = [
        migrations.RenameField(
            model_name="task",
            old_name="priority",
            new_name="priority_label",
        ),
        migrations.AddField(
            model_name="task",
            name="priority",
            field=models.PositiveSmallIntegerField(
                choices=[(1, "Low"), (2, "Medium"), (3, "High"), (4, "Urgent")],
                default=2,
                verbose_name="Priority",
            ),
        ),
        migrations.RunPython(labels_to_numbers, numbers_to_labels),
        migrations.RemoveField(
            model_name="task",
            name="priority_label",
        ),
        migrations.AlterModelOptions(
            name="task",
            options={
                "ordering": [
                    django.db.models.expressions.OrderBy(
                        django.db.models.expressions.F("due_date"), nulls_last=True
                    ),
                    "-priority",
                ],
                "verbose_name": "Task",
                "verbose_name_plural": "Tasks",
            },
        ),
    ]
