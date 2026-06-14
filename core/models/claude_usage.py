"""
Claude usage tracking model.

One row per Claude call (from a script via pyrunner_ai, or a connection test).
Rows are written both by Django (test calls) and directly via sqlite3 from
isolated script subprocesses (the pyrunner_ai helper), so attribution columns
are plain UUID/char fields (no enforced FK) and survive script/run deletion.
"""

import uuid

from django.db import models


class ClaudeUsage(models.Model):
    class Source(models.TextChoices):
        SCRIPT = "script", "Script"
        TEST = "test", "Test"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    # Attribution (no DB FK constraint by design -- see module docstring).
    script_id = models.UUIDField(null=True, blank=True, db_index=True)
    run_id = models.UUIDField(null=True, blank=True)
    script_name = models.CharField(max_length=255, blank=True)
    source = models.CharField(
        max_length=10, choices=Source.choices, default=Source.SCRIPT
    )

    model = models.CharField(max_length=100, blank=True)

    # Token counts (from the Anthropic usage block; input_tokens excludes cache).
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    cache_creation_tokens = models.PositiveIntegerField(default=0)
    cache_read_tokens = models.PositiveIntegerField(default=0)

    num_turns = models.PositiveIntegerField(default=0)
    duration_ms = models.PositiveIntegerField(default=0)

    # Estimated API-equivalent cost. Stored for completeness; not shown in the
    # UI (subscription usage is not billed per token).
    cost_usd = models.FloatField(null=True, blank=True)

    class Meta:
        db_table = "claude_usage"
        ordering = ["-created_at"]
        verbose_name = "Claude usage"
        verbose_name_plural = "Claude usage"
        indexes = [
            models.Index(fields=["-created_at"]),
        ]

    def __str__(self):
        return f"{self.model or 'claude'} {self.total_tokens} tok @ {self.created_at:%Y-%m-%d %H:%M}"

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_creation_tokens
            + self.cache_read_tokens
        )
