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

        # Ensure every newly-created user lands in a workspace (tenancy Stage 0).
        self._register_membership_signal()

        # Use post_migrate signal to ensure heartbeat schedule exists after migrations
        # This avoids database access during app initialization
        from django.db.models.signals import post_migrate

        post_migrate.connect(self._on_post_migrate, sender=self)

        # System checks (manage.py check / runserver / test): vector-store prerequisites.
        from core import checks  # noqa: F401

    def _on_post_migrate(self, sender, **kwargs):
        """Run after migrations are complete to set up recurring schedules."""
        self._ensure_heartbeat_schedule()
        self._ensure_update_check_schedule()
        self._ensure_resync_schedule()

    def _register_membership_signal(self):
        """Ensure a new User always gets a default-workspace membership.

        This is the runtime counterpart to the 0031 backfill (which covers
        users that already existed). Without it an invited/created user would
        have zero memberships and the active-workspace middleware couldn't
        resolve a workspace for them. Idempotent and best-effort — it must never
        block user creation.
        """
        from django.db.models.signals import post_save

        def ensure_membership(sender, instance, created, **kwargs):
            if not created:
                return
            try:
                from core.models import Workspace, WorkspaceMembership

                default = Workspace.get_default()
                if default is None:
                    return
                role = (
                    WorkspaceMembership.ROLE_OWNER
                    if instance.is_superuser
                    else WorkspaceMembership.ROLE_MEMBER
                )
                WorkspaceMembership.ensure(instance, default, role=role)
            except Exception:
                pass  # Never break user creation on membership bookkeeping.

        from core.models import User

        # weak=False is REQUIRED: ensure_membership is a local closure with no
        # other strong reference, so a weak connection (the default) would be
        # garbage-collected after ready() returns and the signal would never fire.
        post_save.connect(
            ensure_membership,
            sender=User,
            dispatch_uid="ensure_workspace_membership",
            weak=False,
        )

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

    def _ensure_update_check_schedule(self):
        """Ensure the daily update-check schedule exists in database."""
        try:
            # Only run if database tables exist (not during migrations)
            from django.db import connection

            if "django_q_schedule" in connection.introspection.table_names():
                from core.services.schedule_service import ScheduleService

                ScheduleService.ensure_update_check_schedule()
        except Exception:
            pass  # Database not ready yet

    def _ensure_resync_schedule(self):
        """Ensure the daily schedule-resync (DST heal) schedule exists."""
        try:
            # Only run if database tables exist (not during migrations)
            from django.db import connection

            if "django_q_schedule" in connection.introspection.table_names():
                from core.services.schedule_service import ScheduleService

                ScheduleService.ensure_resync_schedule()
        except Exception:
            pass  # Database not ready yet
