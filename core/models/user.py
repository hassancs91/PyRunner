"""
User and MagicToken models for passwordless authentication.
"""

import secrets
from datetime import timedelta

from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone


class User(AbstractUser):
    """
    Custom user model for magic link authentication.
    Uses email as the primary identifier instead of username.
    """

    email = models.EmailField(unique=True)
    is_verified = models.BooleanField(
        default=False,
        help_text="Whether the user has verified their email via magic link",
    )

    # Use email as the username field
    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []  # Email is already required via USERNAME_FIELD

    class Meta:
        db_table = "users"
        verbose_name = "user"
        verbose_name_plural = "users"

    def __str__(self):
        return self.email

    def save(self, *args, **kwargs):
        # Auto-set username to email if not provided
        if not self.username:
            self.username = self.email
        # Ensure password is unusable (magic link only)
        if not self.has_usable_password():
            self.set_unusable_password()
        super().save(*args, **kwargs)


class MagicToken(models.Model):
    """
    One-time use token for passwordless authentication.
    Tokens expire after a configurable time and can only be used once.
    """

    EXPIRY_MINUTES = 15

    token = models.CharField(max_length=64, unique=True, db_index=True)
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="magic_tokens",
        null=True,
        blank=True,
    )
    email = models.EmailField(help_text="Email address this token was sent to")
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    ip_address = models.GenericIPAddressField(
        null=True,
        blank=True,
        help_text="IP address that requested this token",
    )

    class Meta:
        db_table = "magic_tokens"
        verbose_name = "magic token"
        verbose_name_plural = "magic tokens"
        indexes = [
            models.Index(fields=["token", "expires_at"]),
            models.Index(fields=["email", "created_at"]),
        ]

    def __str__(self):
        status = "used" if self.used_at else ("expired" if not self.is_valid() else "valid")
        return f"MagicToken for {self.email} ({status})"

    @classmethod
    def create_for_email(cls, email: str, ip_address: str = None) -> "MagicToken":
        """
        Create a new magic token for an email address.
        Invalidates any existing unused tokens for the same email.
        """
        # Invalidate existing unused tokens for this email
        cls.objects.filter(email=email, used_at__isnull=True).update(
            expires_at=timezone.now()
        )

        # Get or create user for this email
        user, _ = User.objects.get_or_create(
            email=email,
            defaults={"is_verified": False},
        )

        # Create new token
        return cls.objects.create(
            token=secrets.token_urlsafe(48),
            user=user,
            email=email,
            expires_at=timezone.now() + timedelta(minutes=cls.EXPIRY_MINUTES),
            ip_address=ip_address,
        )

    def is_valid(self) -> bool:
        """Check if the token is still valid (not expired and not used)."""
        return self.used_at is None and self.expires_at > timezone.now()

    def consume(self) -> User:
        """
        Mark the token as used and return the associated user.
        Also marks the user as verified.
        """
        if not self.is_valid():
            raise ValueError("Token is no longer valid")

        self.used_at = timezone.now()
        self.save(update_fields=["used_at"])

        # Mark user as verified
        self.user.is_verified = True
        self.user.save(update_fields=["is_verified"])

        return self.user
