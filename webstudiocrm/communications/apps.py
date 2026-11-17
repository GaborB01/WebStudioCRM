from django.apps import AppConfig


class CommunicationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "communications"
    verbose_name = "Mail & Timeline"

    def ready(self):
        from . import signals  # noqa: F401  (connects the receivers)
