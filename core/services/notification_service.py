"""
Notification service for sending email and webhook notifications.

This module handles sending notifications when script runs complete.
Supports email via SMTP or Resend, and webhook POST notifications.
"""

import logging
from typing import TYPE_CHECKING

import requests
from django.core.mail import EmailMultiAlternatives
from django.core.mail.backends.smtp import EmailBackend as SMTPBackend
from django.template.loader import render_to_string
from django.utils import timezone

from .encryption_service import EncryptionService, EncryptionError

if TYPE_CHECKING:
    from core.models import Run, GlobalSettings

logger = logging.getLogger(__name__)


class NotificationService:
    """
    Handles sending notifications via email and webhooks.
    """

    WEBHOOK_TIMEOUT = 10  # seconds

    @classmethod
    def should_notify(cls, run: "Run") -> bool:
        """
        Determine if a notification should be sent for this run.

        Args:
            run: The completed Run instance

        Returns:
            bool: True if notification should be sent
        """
        from core.models import Script

        script = run.script
        notify_on = script.notify_on

        if notify_on == Script.NotifyOn.NEVER:
            return False
        elif notify_on == Script.NotifyOn.SUCCESS:
            return run.status == "success"
        elif notify_on == Script.NotifyOn.FAILURE:
            return run.status in ["failed", "timeout"]
        elif notify_on == Script.NotifyOn.BOTH:
            return run.is_finished
        return False

    @classmethod
    def send_notification(cls, run: "Run") -> dict:
        """
        Send all applicable notifications for a completed run.

        Args:
            run: The completed Run instance

        Returns:
            dict: Results of notification attempts
        """
        results = {
            "email_sent": False,
            "email_error": None,
            "webhook_sent": False,
            "webhook_error": None,
        }

        if not cls.should_notify(run):
            logger.debug(f"Skipping notification for run {run.id} - notify_on={run.script.notify_on}")
            return results

        # Send email notification
        if cls._should_send_email(run):
            try:
                cls._send_email_notification(run)
                results["email_sent"] = True
                logger.info(f"Email notification sent for run {run.id}")
            except Exception as e:
                logger.error(f"Failed to send email notification for run {run.id}: {e}")
                results["email_error"] = str(e)

        # Send webhook notification
        if run.script.notify_webhook_enabled and run.script.notify_webhook_url:
            try:
                cls._send_webhook_notification(run)
                results["webhook_sent"] = True
                logger.info(f"Webhook notification sent for run {run.id}")
            except Exception as e:
                logger.error(f"Failed to send webhook notification for run {run.id}: {e}")
                results["webhook_error"] = str(e)

        return results

    @classmethod
    def _should_send_email(cls, run: "Run") -> bool:
        """Check if email notification should be sent."""
        from core.models import GlobalSettings

        settings = GlobalSettings.get_settings()
        if settings.email_backend == GlobalSettings.EmailBackend.DISABLED:
            return False

        # Check if there's a recipient email
        recipient = run.script.notify_email or settings.default_notification_email
        return bool(recipient)

    @classmethod
    def _get_email_backend(cls, settings: "GlobalSettings") -> SMTPBackend | None:
        """
        Get the appropriate email backend based on settings.

        Returns:
            Email backend instance or None if disabled
        """
        from core.models import GlobalSettings

        if settings.email_backend == GlobalSettings.EmailBackend.SMTP:
            password = ""
            if settings.smtp_password_encrypted:
                try:
                    password = EncryptionService.decrypt(settings.smtp_password_encrypted)
                except EncryptionError as e:
                    logger.error(f"Failed to decrypt SMTP password: {e}")

            return SMTPBackend(
                host=settings.smtp_host,
                port=settings.smtp_port,
                username=settings.smtp_username,
                password=password,
                use_tls=settings.smtp_use_tls,
                fail_silently=False,
            )
        elif settings.email_backend == GlobalSettings.EmailBackend.RESEND:
            # Use Resend's SMTP gateway
            api_key = ""
            if settings.resend_api_key_encrypted:
                try:
                    api_key = EncryptionService.decrypt(settings.resend_api_key_encrypted)
                except EncryptionError as e:
                    logger.error(f"Failed to decrypt Resend API key: {e}")

            return SMTPBackend(
                host="smtp.resend.com",
                port=587,
                username="resend",
                password=api_key,
                use_tls=True,
                fail_silently=False,
            )
        return None

    @classmethod
    def _send_email_notification(cls, run: "Run") -> None:
        """Send email notification for a run."""
        from core.models import GlobalSettings

        settings = GlobalSettings.get_settings()
        backend = cls._get_email_backend(settings)

        if not backend:
            raise ValueError("Email backend not configured")

        recipient = run.script.notify_email or settings.default_notification_email
        if settings.email_backend == GlobalSettings.EmailBackend.SMTP:
            from_email = settings.smtp_from_email
        else:
            from_email = settings.resend_from_email

        if not recipient or not from_email:
            raise ValueError("Missing email configuration (recipient or from address)")

        # Render templates
        context = cls._build_email_context(run)
        status_display = run.status.upper()
        subject = f"[PyRunner] {run.script.name} - {status_display}"

        text_content = render_to_string("notifications/email/run_completed.txt", context)
        html_content = render_to_string("notifications/email/run_completed.html", context)

        # Send email
        email = EmailMultiAlternatives(
            subject=subject,
            body=text_content,
            from_email=from_email,
            to=[recipient],
            connection=backend,
        )
        email.attach_alternative(html_content, "text/html")
        email.send()

        logger.info(f"Email notification sent for run {run.id} to {recipient}")

    @classmethod
    def _send_webhook_notification(cls, run: "Run") -> None:
        """Send webhook notification for a run."""
        url = run.script.notify_webhook_url
        payload = cls._build_webhook_payload(run)

        response = requests.post(
            url,
            json=payload,
            timeout=cls.WEBHOOK_TIMEOUT,
            headers={"Content-Type": "application/json", "User-Agent": "PyRunner/1.0"},
        )

        logger.info(
            f"Webhook notification sent for run {run.id} to {url} "
            f"(status: {response.status_code})"
        )

    @classmethod
    def _build_email_context(cls, run: "Run") -> dict:
        """Build context for email templates."""
        # Calculate duration display
        duration_display = "N/A"
        if run.duration:
            duration_secs = run.duration
            if duration_secs < 60:
                duration_display = f"{duration_secs:.1f}s"
            else:
                minutes = int(duration_secs // 60)
                seconds = int(duration_secs % 60)
                duration_display = f"{minutes}m {seconds}s"

        return {
            "run": run,
            "script": run.script,
            "status": run.status,
            "duration": duration_display,
            "error_excerpt": run.stderr[:500] if run.stderr else None,
        }

    @classmethod
    def _build_webhook_payload(cls, run: "Run") -> dict:
        """Build JSON payload for webhook notification."""
        return {
            "event_type": "run_completed",
            "script": {
                "id": str(run.script.id),
                "name": run.script.name,
            },
            "run": {
                "id": str(run.id),
                "status": run.status,
                "exit_code": run.exit_code,
                "duration_seconds": run.duration,
                "trigger_type": run.trigger_type,
            },
            "timestamps": {
                "started_at": run.started_at.isoformat() if run.started_at else None,
                "ended_at": run.ended_at.isoformat() if run.ended_at else None,
            },
            "error": run.stderr[:1000] if run.stderr and run.status in ["failed", "timeout"] else None,
        }

    @classmethod
    def send_test_email(cls, recipient_email: str) -> bool:
        """
        Send a test email to verify configuration.

        Args:
            recipient_email: Email address to send test to

        Returns:
            bool: True if successful

        Raises:
            Exception: If sending fails
        """
        from core.models import GlobalSettings

        settings = GlobalSettings.get_settings()
        backend = cls._get_email_backend(settings)

        if not backend:
            raise ValueError("Email backend not configured or disabled")

        if settings.email_backend == GlobalSettings.EmailBackend.SMTP:
            from_email = settings.smtp_from_email
        else:
            from_email = settings.resend_from_email

        if not from_email:
            raise ValueError("From email address not configured")

        subject = "[PyRunner] Test Email"
        text_content = (
            "This is a test email from PyRunner.\n\n"
            "If you receive this, your email configuration is working correctly."
        )
        html_content = render_to_string(
            "notifications/email/test_email.html",
            {"timestamp": timezone.now()},
        )

        email = EmailMultiAlternatives(
            subject=subject,
            body=text_content,
            from_email=from_email,
            to=[recipient_email],
            connection=backend,
        )
        email.attach_alternative(html_content, "text/html")
        email.send()

        logger.info(f"Test email sent to {recipient_email}")
        return True
