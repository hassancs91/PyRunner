"""
Tag model for categorizing scripts.
"""

import uuid

from django.conf import settings
from django.db import models


class Tag(models.Model):
    """
    A tag for categorizing and filtering scripts.
    """

    class Color(models.TextChoices):
        GRAY = "gray", "Gray"
        RED = "red", "Red"
        ORANGE = "orange", "Orange"
        YELLOW = "yellow", "Yellow"
        GREEN = "green", "Green"
        BLUE = "blue", "Blue"
        PURPLE = "purple", "Purple"
        PINK = "pink", "Pink"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(
        max_length=50,
        unique=True,
        help_text="Tag name (must be unique)",
    )
    color = models.CharField(
        max_length=20,
        choices=Color.choices,
        default=Color.GRAY,
        help_text="Tag color for visual distinction",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_tags",
    )

    class Meta:
        db_table = "tags"
        verbose_name = "tag"
        verbose_name_plural = "tags"
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def script_count(self) -> int:
        """Return the number of scripts using this tag."""
        return self.scripts.count()
