"""
Script-level operations that are more than one model method.

Today that is forking: duplicating a script into the same workspace as a fresh,
user-owned copy. The copy rules are explicit allow-lists rather than "every
field except…" so that adding a column to ``Script`` or ``ScriptSchedule``
forces a decision here — ``core/test_script_fork.py`` asserts the lists cover
every concrete field, so a new column can't be silently dropped from forks.
"""

from django.db import transaction

from core.models import Script, ScriptLibrary, ScriptSchedule, SecretGrant

# Script fields copied verbatim onto the fork.
FORK_COPIED_FIELDS = (
    "description",
    "code",
    "environment",
    "timeout_seconds",
    "is_enabled",
    "injection_mode",
    "isolation_mode",
    "notify_on",
    "notify_email",
    "notify_webhook_url",
    "notify_webhook_enabled",
    "retention_days_override",
    "retention_count_override",
)

# Script fields deliberately NOT copied, and why.
FORK_RESET_FIELDS = (
    "id",  # fresh UUID
    "name",  # "Copy of …" unless the caller names it
    "workspace",  # stamped from the source: a fork never crosses workspaces
    "owner_plugin",  # the fork is user-owned; the plugin does not manage it
    "owner_key",
    "webhook_token",  # a token names exactly one script; the fork starts without
    "archived_at",  # forking an archived script yields a live one
    "archived_by",
    "created_at",
    "updated_at",
    "created_by",  # the forking user
)

# Many-to-many relations copied row by row (through rows included).
FORK_COPIED_RELATIONS = ("tags", "notify_channels", "granted_secrets", "libraries")

# Schedule configuration carried over; the fork's schedule is always created
# inactive so a duplicate can't start firing the moment it exists.
FORK_SCHEDULE_COPIED_FIELDS = (
    "run_mode",
    "interval_minutes",
    "daily_times",
    "timezone",
    "weekly_days",
    "weekly_times",
    "monthly_days",
    "monthly_times",
    "cron_expression",
    "yearly_month",
    "yearly_day",
    "yearly_time",
)
FORK_SCHEDULE_RESET_FIELDS = (
    "id",
    "script",
    "workspace",
    "is_active",  # always False on the fork
    "q_schedule_ids",  # nothing is registered with django-q until it is enabled
    "next_run",
    "last_scheduled_run",
    "created_at",
    "updated_at",
    "created_by",
)

NAME_MAX_LENGTH = Script._meta.get_field("name").max_length


def fork_name(source_name: str) -> str:
    """Default name for a fork, trimmed to the column width."""
    return f"Copy of {source_name}"[:NAME_MAX_LENGTH]


@transaction.atomic
def fork_script(source: Script, *, user, name: str | None = None) -> Script:
    """Duplicate ``source`` into its own workspace as a new user-owned script.

    Copies code, settings, tags, notification channels, secret grants and
    library attachments. The webhook token and plugin ownership are cleared,
    archive state is dropped, and the schedule (if any) is cloned inactive.
    Runs are never copied — history belongs to the original.
    """
    fork = Script(
        name=name or fork_name(source.name),
        workspace=source.workspace,
        created_by=user,
    )
    for field_name in FORK_COPIED_FIELDS:
        attname = Script._meta.get_field(field_name).attname
        setattr(fork, attname, getattr(source, attname))
    fork.save()

    fork.tags.set(source.tags.all())
    fork.notify_channels.set(source.notify_channels.all())
    SecretGrant.objects.bulk_create(
        [
            SecretGrant(script=fork, secret_id=grant.secret_id, active=grant.active)
            for grant in source.secret_grants.all()
        ]
    )
    ScriptLibrary.objects.bulk_create(
        [
            ScriptLibrary(script=fork, library_id=attachment.library_id)
            for attachment in source.library_attachments.all()
        ]
    )

    try:
        source_schedule = source.schedule
    except ScriptSchedule.DoesNotExist:
        source_schedule = None
    if source_schedule is not None:
        config = {
            name_: getattr(source_schedule, name_)
            for name_ in FORK_SCHEDULE_COPIED_FIELDS
        }
        ScriptSchedule.objects.create(
            script=fork,
            workspace=fork.workspace,
            created_by=user,
            is_active=False,
            **config,
        )

    return fork
