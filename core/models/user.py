"""
User, MagicToken, and PasswordResetToken models for authentication.
Supports both password-based and magic link authentication.
"""

import secrets
from datetime import timedelta

from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone


class User(AbstractUser):
    """
    Custom user model supporting both password and magic link authentication.
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
        # Only set unusable password for new users without a password
        # This allows password auth while keeping magic link as an option
        if self._state.adding and not self.password:
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

        The first user to register is automatically promoted to admin (superuser).
        """
        # Invalidate existing unused tokens for this email
        cls.objects.filter(email=email, used_at__isnull=True).update(
            expires_at=timezone.now()
        )

        # Check if this will be the first user (before creating)
        is_first_user = User.objects.count() == 0

        # Get or create user for this email
        user, created = User.objects.get_or_create(
            email=email,
            defaults={"is_verified": False},
        )

        # Auto-promote first user to admin and disable open registration
        if created and is_first_user:
            user.is_staff = True
            user.is_superuser = True
            user.save(update_fields=["is_staff", "is_superuser"])

            # Auto-disable open registration after first user
            from core.models.settings import GlobalSettings
            settings = GlobalSettings.get_settings()
            settings.allow_registration = False
            settings.save(update_fields=["allow_registration"])

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


class UserInvite(models.Model):
    """
    Invitation for a new user. Admin creates invite, gets shareable link.
    Can be used once to create an account when registration is closed.
    """

    EXPIRY_DAYS = 7

    email = models.EmailField(
        unique=True,
        help_text="Email address of the invited user",
    )
    token = models.CharField(max_length=64, unique=True, db_index=True)
    created_by = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="sent_invites",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    used_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="received_invite",
    )

    class Meta:
        db_table = "user_invites"
        verbose_name = "user invite"
        verbose_name_plural = "user invites"
        indexes = [
            models.Index(fields=["token"]),
            models.Index(fields=["email"]),
        ]

    def __str__(self):
        status = "used" if self.used_at else ("expired" if not self.is_valid() else "pending")
        return f"Invite for {self.email} ({status})"

    @classmethod
    def create_invite(cls, email: str, created_by: User) -> "UserInvite":
        """
        Create a new invitation. Deletes any existing unused invite for the same email.
        """
        # Delete existing unused invite for this email
        cls.objects.filter(email__iexact=email, used_at__isnull=True).delete()

        return cls.objects.create(
            email=email.lower().strip(),
            token=secrets.token_urlsafe(48),
            created_by=created_by,
            expires_at=timezone.now() + timedelta(days=cls.EXPIRY_DAYS),
        )

    def is_valid(self) -> bool:
        """Check if the invite is still valid (not expired and not used)."""
        return self.used_at is None and self.expires_at > timezone.now()

    def mark_used(self, user: User) -> None:
        """Mark the invite as used by the specified user."""
        self.used_at = timezone.now()
        self.used_by = user
        self.save(update_fields=["used_at", "used_by"])


class PasswordResetToken(models.Model):
    """
    Token for password reset functionality.
    Allows users to reset their password via email link.
    """

    EXPIRY_HOURS = 24

    token = models.CharField(max_length=64, unique=True, db_index=True)
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="password_reset_tokens",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "password_reset_tokens"
        verbose_name = "password reset token"
        verbose_name_plural = "password reset tokens"
        indexes = [
            models.Index(fields=["token", "expires_at"]),
        ]

    def __str__(self):
        status = "used" if self.used_at else ("expired" if not self.is_valid() else "valid")
        return f"PasswordResetToken for {self.user.email} ({status})"

    @classmethod
    def create_for_user(cls, user: User) -> "PasswordResetToken":
        """
        Create a new password reset token for a user.
        Invalidates any existing unused tokens for the same user.
        """
        # Invalidate existing unused tokens for this user
        cls.objects.filter(user=user, used_at__isnull=True).update(
            expires_at=timezone.now()
        )

        return cls.objects.create(
            token=secrets.token_urlsafe(48),
            user=user,
            expires_at=timezone.now() + timedelta(hours=cls.EXPIRY_HOURS),
        )

    def is_valid(self) -> bool:
        """Check if the token is still valid (not expired and not used)."""
        return self.used_at is None and self.expires_at > timezone.now()

    def consume(self) -> User:
        """
        Mark the token as used and return the associated user.
        """
        if not self.is_valid():
            raise ValueError("Token is no longer valid")

        self.used_at = timezone.now()
        self.save(update_fields=["used_at"])

        return self.user
