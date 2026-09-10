"""
Email sending abstraction for authentication emails (password reset).
Supports multiple backends: Console (dev), Resend API (production).
"""
import logging
from django.conf import settings
from django.core.mail import EmailMultiAlternatives, send_mail
from django.urls import reverse

logger = logging.getLogger(__name__)


def send_password_reset_email(request, user, reset_token) -> bool:
    """
    Send password reset email.

    Args:
        request: HttpRequest object (for building absolute URL)
        user: User instance
        reset_token: PasswordResetToken instance

    Returns:
        bool: True if email sent successfully
    """
    reset_path = reverse("auth:reset_password", kwargs={"token": reset_token.token})
    reset_url = request.build_absolute_uri(reset_path)

    # Print URL directly to console in development
    if settings.DEBUG:
        print("\n" + "=" * 60)
        print("PASSWORD RESET LINK (click or copy):")
        print(reset_url)
        print("=" * 60 + "\n")

    subject = "Reset Your PyRun Password"

    text_message = f"""Hi there!

You requested a password reset for your PyRun account.

Click the link below to reset your password:

========================================
{reset_url}
========================================

This link will expire in 24 hours and can only be used once.

If you didn't request this, you can safely ignore this email.

---
PyRun - Python Script Automation
"""

    html_message = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; line-height: 1.6; color: #333; }}
        .container {{ max-width: 600px; margin: 0 auto; padding: 40px 20px; }}
        .button {{ display: inline-block; background: #2563eb; color: #ffffff !important; padding: 14px 28px; text-decoration: none; border-radius: 6px; font-weight: 600; }}
        .footer {{ margin-top: 40px; padding-top: 20px; border-top: 1px solid #e5e7eb; color: #6b7280; font-size: 14px; }}
    </style>
</head>
<body>
    <div class="container">
        <h2>Reset Your Password</h2>
        <p>You requested a password reset for your PyRun account ({user.email}).</p>
        <p style="margin: 30px 0;">
            <a href="{reset_url}" class="button">Reset Password</a>
        </p>
        <p style="color: #6b7280; font-size: 14px;">
            This link expires in 24 hours and can only be used once.
        </p>
        <p style="color: #6b7280; font-size: 14px;">
            Or copy and paste this URL into your browser:<br>
            <code style="background: #f3f4f6; padding: 2px 6px; border-radius: 4px;">{reset_url}</code>
        </p>
        <div class="footer">
            <p>If you didn't request this, you can safely ignore this email.</p>
            <p><strong>PyRun</strong> - Python Script Automation</p>
        </div>
    </div>
</body>
</html>"""

    return _send_email(user.email, subject, text_message, html_message)


def _get_db_backend_and_from_email():
    """
    Returns (backend, from_email) if GlobalSettings has a non-DISABLED email
    backend configured, else (None, None). Local imports avoid a circular
    import with core.services.notification_service.
    """
    from core.models import GlobalSettings
    from core.services.notification_service import NotificationService

    gs = GlobalSettings.get_settings()
    if gs.email_backend == GlobalSettings.EmailBackend.DISABLED:
        return None, None

    backend = NotificationService._get_email_backend(gs)
    if backend is None:
        return None, None

    from_email = (
        gs.smtp_from_email
        if gs.email_backend == GlobalSettings.EmailBackend.SMTP
        else gs.resend_from_email
    )
    return backend, from_email


def _send_email(to_email: str, subject: str, text_content: str, html_content: str) -> bool:
    """
    Unified sender for auth emails.
    Resolution order: DB-configured backend → env Resend API → Django send_mail().
    A DB backend failure is NOT retried via env — it is surfaced as False.
    """
    backend, from_email = _get_db_backend_and_from_email()
    if backend is not None:
        if not from_email:
            logger.error(
                f"DB email backend configured but from_email is empty; cannot send to {to_email}"
            )
            return False
        try:
            msg = EmailMultiAlternatives(
                subject=subject,
                body=text_content,
                from_email=from_email,
                to=[to_email],
                connection=backend,
            )
            msg.attach_alternative(html_content, "text/html")
            msg.send()
            logger.info(f"Auth email sent to {to_email} via DB backend")
            return True
        except Exception as e:
            logger.error(f"DB backend failed to send auth email to {to_email}: {e}")
            return False

    if _should_use_resend():
        return _send_via_resend(to_email, subject, text_content, html_content)

    try:
        send_mail(
            subject=subject,
            message=text_content,
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@localhost"),
            recipient_list=[to_email],
            html_message=html_content,
            fail_silently=False,
        )
        logger.info(f"Auth email sent to {to_email} via env backend")
        return True
    except Exception as e:
        logger.error(f"Env backend failed to send auth email to {to_email}: {e}")
        return False


def _should_use_resend() -> bool:
    """Check if Resend API should be used."""
    return (
        hasattr(settings, "RESEND_API_KEY") and
        settings.RESEND_API_KEY and
        getattr(settings, "USE_RESEND", False)
    )


def _send_via_resend(to_email: str, subject: str, text_content: str, html_content: str) -> bool:
    """
    Send email via Resend API.

    Requires:
        - RESEND_API_KEY in settings
        - USE_RESEND = True in settings
        - resend package installed
    """
    try:
        import resend
        resend.api_key = settings.RESEND_API_KEY

        params = {
            "from": getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@localhost"),
            "to": [to_email],
            "subject": subject,
            "text": text_content,
            "html": html_content,
        }

        response = resend.Emails.send(params)
        logger.info(f"Resend email sent to {to_email}, id: {response.get('id')}")
        return True

    except ImportError:
        logger.error("resend package not installed. Run: pip install resend")
        return False
    except Exception as e:
        logger.error(f"Resend API error: {e}")
        return False
