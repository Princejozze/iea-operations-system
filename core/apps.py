from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = "core"
    verbose_name = "Core"

    def ready(self):
        # Connects the delete-auditing signal receiver.
        from . import signals  # noqa: F401
