"""
Email sending abstraction for magic link authentication.
Supports multiple backends: Console (dev), Resend API (production).
"""
import logging
from django.conf import settings
from django.core.mail import send_mail
from django.urls import reverse

logger = logging.getLogger(__name__)


def send_magic_link_email(request, magic_token) -> bool:
    """
    Send magic link email using configured backend.

    Args:
        request: HttpRequest object (for building absolute URL)
        magic_token: MagicToken instance

    Returns:
        bool: True if email sent successfully
    """
    verify_path = reverse("auth:verify", kwargs={"token": magic_token.token})
    verify_url = request.build_absolute_uri(verify_path)

    # Print URL directly to console in development (bypasses MIME encoding)
    if settings.DEBUG:
        print("\n" + "=" * 60)
        print("MAGIC LINK (click or copy):")
        print(verify_url)
        print("=" * 60 + "\n")

    subject = "Your PyRunner Login Link"

    text_message = f"""Hi there!

Click the link below to sign in to PyRunner:

========================================
{verify_url}
========================================

This link will expire in 15 minutes and can only be used once.

If you didn't request this link, you can safely ignore this email.

---
PyRunner - Python Script Automation
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
        <h2>Sign in to PyRunner</h2>
        <p>Click the button below to sign in:</p>
        <p style="margin: 30px 0;">
            <a href="{verify_url}" class="button">Sign In to PyRunner</a>
        </p>
        <p style="color: #6b7280; font-size: 14px;">
            This link expires in 15 minutes and can only be used once.
        </p>
        <p style="color: #6b7280; font-size: 14px;">
            Or copy and paste this URL into your browser:<br>
            <code style="background: #f3f4f6; padding: 2px 6px; border-radius: 4px;">{verify_url}</code>
        </p>
        <div class="footer">
            <p>If you didn't request this link, you can safely ignore this email.</p>
            <p><strong>PyRunner</strong> - Python Script Automation</p>
        </div>
    </div>
</body>
</html>"""

    try:
        if _should_use_resend():
            return _send_via_resend(
                to_email=magic_token.email,
                subject=subject,
                text_content=text_message,
                html_content=html_message,
            )

        send_mail(
            subject=subject,
            message=text_message,
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@localhost"),
            recipient_list=[magic_token.email],
            html_message=html_message,
            fail_silently=False,
        )
        logger.info(f"Magic link email sent to {magic_token.email}")
        return True

    except Exception as e:
        logger.error(f"Failed to send magic link email to {magic_token.email}: {e}")
        return False


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

    subject = "Reset Your PyRunner Password"

    text_message = f"""Hi there!

You requested a password reset for your PyRunner account.

Click the link below to reset your password:

========================================
{reset_url}
========================================

This link will expire in 24 hours and can only be used once.

If you didn't request this, you can safely ignore this email.

---
PyRunner - Python Script Automation
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
        <p>You requested a password reset for your PyRunner account ({user.email}).</p>
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
            <p><strong>PyRunner</strong> - Python Script Automation</p>
        </div>
    </div>
</body>
</html>"""

    try:
        if _should_use_resend():
            return _send_via_resend(
                to_email=user.email,
                subject=subject,
                text_content=text_message,
                html_content=html_message,
            )

        send_mail(
            subject=subject,
            message=text_message,
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@localhost"),
            recipient_list=[user.email],
            html_message=html_message,
            fail_silently=False,
        )
        logger.info(f"Password reset email sent to {user.email}")
        return True

    except Exception as e:
        logger.error(f"Failed to send password reset email to {user.email}: {e}")
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
