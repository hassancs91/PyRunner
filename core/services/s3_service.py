"""
S3 storage service for backup operations.

Supports any S3-compatible provider: AWS, MinIO, DigitalOcean Spaces, Backblaze B2, etc.
"""

import logging
from typing import Tuple

from core.models import GlobalSettings
from core.services.encryption_service import EncryptionService, EncryptionError

logger = logging.getLogger(__name__)


class S3ServiceError(Exception):
    """Raised when S3 operations fail."""

    pass


class S3Service:
    """
    Provides S3 storage operations for backup functionality.

    Supports any S3-compatible provider: AWS, MinIO, DigitalOcean Spaces, etc.
    """

    @classmethod
    def get_client(cls):
        """
        Create and return a configured boto3 S3 client.

        Returns:
            boto3 S3 client configured with current settings

        Raises:
            S3ServiceError: If S3 is not configured or credentials invalid
        """
        try:
            import boto3
            from botocore.config import Config
        except ImportError:
            raise S3ServiceError(
                "boto3 is not installed. Install it with: pip install boto3"
            )

        settings = GlobalSettings.get_settings()

        if not settings.s3_bucket_name:
            raise S3ServiceError("S3 bucket name is not configured")

        if not settings.s3_access_key_encrypted or not settings.s3_secret_key_encrypted:
            raise S3ServiceError("S3 credentials are not configured")

        # Decrypt credentials
        try:
            access_key = EncryptionService.decrypt(settings.s3_access_key_encrypted)
            secret_key = EncryptionService.decrypt(settings.s3_secret_key_encrypted)
        except EncryptionError as e:
            raise S3ServiceError(f"Failed to decrypt S3 credentials: {e}")

        # Build client config
        config = Config(
            signature_version="s3v4",
            s3={"addressing_style": "path" if settings.s3_path_style else "auto"},
        )

        client_kwargs = {
            "service_name": "s3",
            "aws_access_key_id": access_key,
            "aws_secret_access_key": secret_key,
            "config": config,
            "use_ssl": settings.s3_use_ssl,
        }

        if settings.s3_endpoint_url:
            client_kwargs["endpoint_url"] = settings.s3_endpoint_url

        if settings.s3_region:
            client_kwargs["region_name"] = settings.s3_region

        return boto3.client(**client_kwargs)

    @classmethod
    def test_connection(cls) -> Tuple[bool, str]:
        """
        Test the S3 connection by attempting to access the bucket.

        Returns:
            Tuple of (success: bool, message: str)
        """
        from django.utils import timezone

        settings = GlobalSettings.get_settings()

        try:
            client = cls.get_client()

            # Try to head the bucket (checks existence and permissions)
            client.head_bucket(Bucket=settings.s3_bucket_name)

            # Update last tested timestamp
            settings.s3_last_tested_at = timezone.now()
            settings.save(update_fields=["s3_last_tested_at"])

            return True, f"Successfully connected to bucket '{settings.s3_bucket_name}'"

        except S3ServiceError as e:
            return False, str(e)
        except Exception as e:
            error_msg = str(e)
            # Parse common boto3 errors for better messages
            if "NoSuchBucket" in error_msg:
                return False, f"Bucket '{settings.s3_bucket_name}' does not exist"
            elif "AccessDenied" in error_msg or "403" in error_msg:
                return (
                    False,
                    "Access denied. Check your credentials and bucket permissions",
                )
            elif "InvalidAccessKeyId" in error_msg:
                return False, "Invalid access key ID"
            elif "SignatureDoesNotMatch" in error_msg:
                return False, "Invalid secret access key"
            elif "EndpointConnectionError" in error_msg or "ConnectTimeoutError" in error_msg:
                return (
                    False,
                    "Cannot connect to endpoint. Check URL and network connectivity",
                )
            elif "InvalidEndpoint" in error_msg:
                return False, "Invalid endpoint URL format"
            else:
                logger.exception("S3 connection test failed")
                return False, f"Connection failed: {error_msg}"

    @classmethod
    def is_configured(cls) -> bool:
        """Check if S3 is properly configured (has required fields)."""
        settings = GlobalSettings.get_settings()
        return bool(
            settings.s3_bucket_name
            and settings.s3_access_key_encrypted
            and settings.s3_secret_key_encrypted
        )

    @classmethod
    def get_status(cls) -> dict:
        """
        Get S3 configuration status for UI display.

        Returns:
            Dict with status information
        """
        settings = GlobalSettings.get_settings()

        return {
            "enabled": settings.s3_enabled,
            "configured": cls.is_configured(),
            "bucket": settings.s3_bucket_name or None,
            "endpoint": settings.s3_endpoint_url or "AWS S3 (default)",
            "region": settings.s3_region or "us-east-1",
            "use_ssl": settings.s3_use_ssl,
            "path_style": settings.s3_path_style,
            "last_tested": settings.s3_last_tested_at,
        }

    @classmethod
    def upload_file(cls, file_bytes: bytes, key: str) -> dict:
        """
        Upload a file to S3.

        Args:
            file_bytes: File content as bytes
            key: S3 object key (path)

        Returns:
            dict: Upload result with 'success', 'key', 'size', optional 'error'
        """
        settings = GlobalSettings.get_settings()

        try:
            client = cls.get_client()
            client.put_object(
                Bucket=settings.s3_bucket_name,
                Key=key,
                Body=file_bytes,
                ContentType="application/gzip",
            )

            return {
                "success": True,
                "key": key,
                "size": len(file_bytes),
            }
        except S3ServiceError as e:
            logger.error(f"S3 upload failed for key {key}: {e}")
            return {
                "success": False,
                "key": key,
                "error": str(e),
            }
        except Exception as e:
            logger.exception(f"S3 upload failed for key {key}")
            return {
                "success": False,
                "key": key,
                "error": str(e),
            }

    @classmethod
    def list_files(cls, prefix: str = "") -> list[dict]:
        """
        List files in S3 bucket with optional prefix.

        Args:
            prefix: Key prefix to filter results

        Returns:
            list: List of dicts with 'key', 'size', 'last_modified'
        """
        settings = GlobalSettings.get_settings()

        try:
            client = cls.get_client()
            response = client.list_objects_v2(
                Bucket=settings.s3_bucket_name,
                Prefix=prefix,
            )

            files = []
            for obj in response.get("Contents", []):
                files.append({
                    "key": obj["Key"],
                    "size": obj["Size"],
                    "last_modified": obj["LastModified"],
                })

            return files
        except S3ServiceError as e:
            logger.error(f"S3 list failed for prefix {prefix}: {e}")
            return []
        except Exception as e:
            logger.exception(f"S3 list failed for prefix {prefix}")
            return []

    @classmethod
    def delete_file(cls, key: str) -> bool:
        """
        Delete a file from S3.

        Args:
            key: S3 object key to delete

        Returns:
            bool: True if deleted successfully
        """
        settings = GlobalSettings.get_settings()

        try:
            client = cls.get_client()
            client.delete_object(
                Bucket=settings.s3_bucket_name,
                Key=key,
            )
            return True
        except Exception as e:
            logger.exception(f"S3 delete failed for key {key}")
            return False

    @classmethod
    def delete_files(cls, keys: list[str]) -> int:
        """
        Delete multiple files from S3.

        Args:
            keys: List of S3 object keys to delete

        Returns:
            int: Number of files deleted
        """
        settings = GlobalSettings.get_settings()

        if not keys:
            return 0

        try:
            client = cls.get_client()
            response = client.delete_objects(
                Bucket=settings.s3_bucket_name,
                Delete={
                    "Objects": [{"Key": key} for key in keys],
                },
            )
            return len(response.get("Deleted", []))
        except Exception as e:
            logger.exception("S3 bulk delete failed")
            return 0

    @classmethod
    def generate_backup_key(cls, timestamp=None) -> str:
        """
        Generate a unique S3 key for a backup file.

        Args:
            timestamp: Optional datetime, defaults to now

        Returns:
            str: S3 key like "pyrunner-backups/backup_20240315_143022.json.gz"
        """
        from django.utils import timezone

        settings = GlobalSettings.get_settings()

        if timestamp is None:
            timestamp = timezone.now()

        prefix = settings.s3_backup_prefix.rstrip("/")
        filename = f"backup_{timestamp.strftime('%Y%m%d_%H%M%S')}.json.gz"

        return f"{prefix}/{filename}" if prefix else filename
