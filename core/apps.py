from django.apps import AppConfig
from django.utils import timezone

# Module-level variable to store app start time
APP_START_TIME = None


class CoreConfig(AppConfig):
    name = "core"

    def ready(self):
        global APP_START_TIME
        APP_START_TIME = timezone.now()

        # Register signal handlers for worker heartbeat
        self._register_worker_signals()

        # Use post_migrate signal to ensure heartbeat schedule exists after migrations
        # This avoids database access during app initialization
        from django.db.models.signals import post_migrate

        post_migrate.connect(self._on_post_migrate, sender=self)

    def _on_post_migrate(self, sender, **kwargs):
        """Run after migrations are complete to set up heartbeat schedule."""
        self._ensure_heartbeat_schedule()

    def _register_worker_signals(self):
        """Register django-q2 signals for worker heartbeat."""
        try:
            from django_q.signals import post_execute

            def update_heartbeat(sender, task, **kwargs):
                """Update heartbeat timestamp after each task execution."""
                try:
                    from core.models import GlobalSettings

                    settings = GlobalSettings.get_settings()
                    settings.worker_heartbeat_at = timezone.now()
                    settings.save(update_fields=["worker_heartbeat_at"])
                except Exception:
                    pass  # Fail silently - don't break task execution

            post_execute.connect(update_heartbeat, dispatch_uid="worker_heartbeat")
        except Exception:
            pass  # django-q signals not available

    def _ensure_heartbeat_schedule(self):
        """Ensure the worker heartbeat schedule exists in database."""
        try:
            # Only run if database tables exist (not during migrations)
            from django.db import connection

            if "django_q_schedule" in connection.introspection.table_names():
                from core.services.schedule_service import ScheduleService

                ScheduleService.ensure_heartbeat_schedule()
        except Exception:
            pass  # Database not ready yet
