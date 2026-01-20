"""
Encryption service using Fernet symmetric encryption.

This module provides secure encryption/decryption for secrets stored in the database.
"""

import logging
import os

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings

logger = logging.getLogger(__name__)


class EncryptionError(Exception):
    """Raised when encryption/decryption fails."""

    pass


class EncryptionService:
    """
    Provides symmetric encryption/decryption using Fernet.

    The encryption key is loaded from the ENCRYPTION_KEY environment variable
    or Django settings. If not set, encryption operations will fail.
    """

    _fernet = None

    @classmethod
    def _get_fernet(cls) -> Fernet:
        """Get or create the Fernet instance."""
        if cls._fernet is None:
            key = cls._get_encryption_key()
            cls._fernet = Fernet(key)
        return cls._fernet

    @classmethod
    def _get_encryption_key(cls) -> bytes:
        """
        Get the encryption key from settings/environment.

        Returns:
            The Fernet key as bytes

        Raises:
            EncryptionError: If key is not configured or invalid
        """
        key = getattr(settings, "ENCRYPTION_KEY", None)

        if not key:
            key = os.environ.get("ENCRYPTION_KEY", "")

        if not key:
            raise EncryptionError(
                "ENCRYPTION_KEY not configured. "
                'Generate one with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
            )

        # Convert string key to bytes if necessary
        if isinstance(key, str):
            key = key.encode("utf-8")

        # Validate key format
        try:
            Fernet(key)
        except Exception as e:
            raise EncryptionError(f"Invalid ENCRYPTION_KEY format: {e}")

        return key

    @classmethod
    def encrypt(cls, plaintext: str) -> str:
        """
        Encrypt a plaintext string.

        Args:
            plaintext: The string to encrypt

        Returns:
            Base64-encoded encrypted string

        Raises:
            EncryptionError: If encryption fails
        """
        if not plaintext:
            raise EncryptionError("Cannot encrypt empty value")

        try:
            fernet = cls._get_fernet()
            encrypted = fernet.encrypt(plaintext.encode("utf-8"))
            return encrypted.decode("utf-8")
        except EncryptionError:
            raise
        except Exception as e:
            logger.error(f"Encryption failed: {e}")
            raise EncryptionError(f"Encryption failed: {e}")

    @classmethod
    def decrypt(cls, encrypted_value: str) -> str:
        """
        Decrypt an encrypted string.

        Args:
            encrypted_value: Base64-encoded encrypted string

        Returns:
            Decrypted plaintext string

        Raises:
            EncryptionError: If decryption fails (invalid key or corrupted data)
        """
        if not encrypted_value:
            raise EncryptionError("Cannot decrypt empty value")

        try:
            fernet = cls._get_fernet()
            decrypted = fernet.decrypt(encrypted_value.encode("utf-8"))
            return decrypted.decode("utf-8")
        except InvalidToken:
            logger.error("Decryption failed: Invalid token (wrong key or corrupted data)")
            raise EncryptionError(
                "Decryption failed: Invalid encryption key or corrupted data"
            )
        except EncryptionError:
            raise
        except Exception as e:
            logger.error(f"Decryption failed: {e}")
            raise EncryptionError(f"Decryption failed: {e}")

    @classmethod
    def generate_key(cls) -> str:
        """
        Generate a new Fernet encryption key.

        Returns:
            A new Fernet key as a string
        """
        return Fernet.generate_key().decode("utf-8")

    @classmethod
    def is_configured(cls) -> bool:
        """Check if encryption is properly configured."""
        try:
            cls._get_encryption_key()
            return True
        except EncryptionError:
            return False

    @classmethod
    def reset(cls) -> None:
        """Reset the cached Fernet instance (useful for testing)."""
        cls._fernet = None
