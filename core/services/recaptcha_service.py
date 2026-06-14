"""
Google reCAPTCHA v2 verification service.

Verifies the token submitted by the reCAPTCHA widget against Google's
siteverify endpoint. Used to protect the login page from automated abuse.
"""

import logging

import requests

logger = logging.getLogger(__name__)


class RecaptchaService:
    """Verify reCAPTCHA v2 responses against Google's siteverify API."""

    VERIFY_URL = "https://www.google.com/recaptcha/api/siteverify"
    TIMEOUT = 10

    @classmethod
    def verify(cls, secret_key: str, token: str, remote_ip: str = "") -> bool:
        """
        Verify a reCAPTCHA token.

        Args:
            secret_key: The reCAPTCHA secret key (server-side).
            token: The ``g-recaptcha-response`` value from the form submission.
            remote_ip: Optional client IP address for additional verification.

        Returns:
            True if the token is valid, False otherwise. Network or
            configuration errors are treated as verification failures.
        """
        if not secret_key or not token:
            return False

        payload = {"secret": secret_key, "response": token}
        if remote_ip:
            payload["remoteip"] = remote_ip

        try:
            response = requests.post(
                cls.VERIFY_URL,
                data=payload,
                timeout=cls.TIMEOUT,
            )
            response.raise_for_status()
            result = response.json()
        except (requests.RequestException, ValueError) as e:
            logger.warning("reCAPTCHA verification request failed: %s", e)
            return False

        if not result.get("success"):
            logger.info(
                "reCAPTCHA verification rejected: %s",
                result.get("error-codes", []),
            )
            return False

        return True
