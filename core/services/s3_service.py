"""
S3-compatible object storage service.

Every operation takes a ``StorageConnection`` (see ``core.models.storage_connection``),
so one instance can talk to several buckets: backups to a private one, plugin assets
to another. Resolve a connection with ``S3Service.for_backup()`` (the ``is_default``
row) or ``S3Service.for_assets()`` (``GlobalSettings.assets_storage``).

Supports any S3-compatible provider: Cloudflare R2, AWS, MinIO, DigitalOcean Spaces,
Backblaze B2, etc.
"""

import ipaddress
import logging
import socket
from typing import Optional, Tuple
from urllib.parse import urlparse

from core.services.encryption_service import EncryptionService, EncryptionError

logger = logging.getLogger(__name__)

# Longest life AWS SigV4 will sign for. Presigned URLs are the only way to serve
# from a private bucket, but they DO expire — a URL baked into a rendered page or
# an email 403s once it lapses. Connections with a `public_base_url` sidestep this.
MAX_PRESIGN_SECONDS = 7 * 24 * 3600
DEFAULT_PRESIGN_SECONDS = 3600

# `delete_objects` accepts at most 1000 keys per call.
_DELETE_CHUNK = 1000


def is_safe_endpoint_url(url: str) -> tuple[bool, str]:
    """
    Validate that an S3 endpoint URL doesn't point to internal/private resources.

    Blocks:
    - Private IP ranges (10.x, 172.16-31.x, 192.168.x)
    - Loopback addresses (127.x, localhost)
    - Link-local addresses (169.254.x - AWS metadata endpoint)
    - Internal hostnames

    Note this means a MinIO on localhost is NOT a usable target — the endpoint is
    user-editable, so it is an SSRF vector and the guard is correct. Local
    development against a real bucket needs a real (public) endpoint.

    Args:
        url: The endpoint URL to validate

    Returns:
        Tuple of (is_safe: bool, error_message: str)
    """
    if not url:
        return True, ""

    try:
        parsed = urlparse(url)
        hostname = parsed.hostname

        if not hostname:
            return False, "Invalid URL: no hostname found"

        # Block localhost variations
        if hostname.lower() in ("localhost", "localhost.localdomain"):
            return False, "Internal endpoints are not allowed (localhost)"

        # Try to resolve hostname to IP and check if it's private
        try:
            # Get all IP addresses for the hostname
            addr_info = socket.getaddrinfo(hostname, None)
            for _, _, _, _, sockaddr in addr_info:
                ip_str = sockaddr[0]
                ip = ipaddress.ip_address(ip_str)

                # Block private, loopback, and link-local addresses
                if ip.is_private:
                    return False, f"Private IP addresses are not allowed ({ip_str})"
                if ip.is_loopback:
                    return False, f"Loopback addresses are not allowed ({ip_str})"
                if ip.is_link_local:
                    return False, f"Link-local addresses are not allowed ({ip_str})"
                if ip.is_reserved:
                    return False, f"Reserved addresses are not allowed ({ip_str})"

        except socket.gaierror:
            # Hostname doesn't resolve - could be intentional for custom DNS
            # Allow it but log a warning
            logger.warning(f"S3 endpoint hostname does not resolve: {hostname}")

        return True, ""

    except Exception as e:
        return False, f"Invalid endpoint URL: {e}"


class S3ServiceError(Exception):
    """Raised when S3 operations fail."""

    pass


class S3Service:
    """
    S3 operations against a given ``StorageConnection``.

    Backups resolve ``for_backup()``; the plugin storage seam resolves
    ``for_assets()``. Passing the connection explicitly (rather than each method
    re-reading GlobalSettings, as this service used to) is what lets the two
    coexist without one silently writing into the other's bucket.
    """

    # ----------------------------------------------------------------- #
    # Connection resolution
    # ----------------------------------------------------------------- #

    @staticmethod
    def for_backup():
        """The connection backups use, or None when none is configured."""
        from core.models import StorageConnection

        return StorageConnection.get_default()

    @staticmethod
    def for_assets():
        """The connection the plugin storage seam uses, or None when unset.

        Deliberately does NOT fall back to the backup default — see
        ``GlobalSettings.assets_storage``.
        """
        from core.models import GlobalSettings

        return GlobalSettings.get_settings().assets_storage

    @classmethod
    def get_client(cls, connection):
        """
        Create and return a configured boto3 S3 client for ``connection``.

        Raises:
            S3ServiceError: If the connection is unusable or credentials invalid
        """
        try:
            import boto3
            from botocore.config import Config
        except ImportError:
            raise S3ServiceError(
                "boto3 is not installed. Install it with: pip install boto3"
            )

        if connection is None:
            raise S3ServiceError("No storage connection configured")

        if not connection.bucket:
            raise S3ServiceError("S3 bucket name is not configured")

        if not connection.access_key_encrypted or not connection.secret_key_encrypted:
            raise S3ServiceError("S3 credentials are not configured")

        # Decrypt credentials
        try:
            access_key = EncryptionService.decrypt(connection.access_key_encrypted)
            secret_key = EncryptionService.decrypt(connection.secret_key_encrypted)
        except EncryptionError as e:
            raise S3ServiceError(f"Failed to decrypt S3 credentials: {e}")

        # Build client config
        config = Config(
            signature_version="s3v4",
            s3={"addressing_style": "path" if connection.path_style else "auto"},
        )

        client_kwargs = {
            "service_name": "s3",
            "aws_access_key_id": access_key,
            "aws_secret_access_key": secret_key,
            "config": config,
            "use_ssl": connection.use_ssl,
        }

        if connection.endpoint_url:
            # Validate endpoint URL to prevent SSRF attacks
            is_safe, error_msg = is_safe_endpoint_url(connection.endpoint_url)
            if not is_safe:
                raise S3ServiceError(f"Invalid S3 endpoint: {error_msg}")
            client_kwargs["endpoint_url"] = connection.endpoint_url

        if connection.region:
            client_kwargs["region_name"] = connection.region

        return boto3.client(**client_kwargs)

    @staticmethod
    def _map_boto3_error(error_msg: str, bucket_name: str) -> str:
        """Map a boto3/botocore error string to a friendly, actionable message.

        Shared by both connection tests so their error handling can't drift.
        """
        if "NoSuchBucket" in error_msg:
            return f"Bucket '{bucket_name}' does not exist"
        if "AccessDenied" in error_msg or "403" in error_msg:
            return "Access denied. Check your credentials and bucket permissions"
        if "InvalidAccessKeyId" in error_msg:
            return "Invalid access key ID"
        if "SignatureDoesNotMatch" in error_msg:
            return "Invalid secret access key"
        if "EndpointConnectionError" in error_msg or "ConnectTimeoutError" in error_msg:
            return "Cannot connect to endpoint. Check URL and network connectivity"
        if "InvalidEndpoint" in error_msg:
            return "Invalid endpoint URL format"
        logger.exception("S3 connection test failed")
        return f"Connection failed: {error_msg}"

    @classmethod
    def test_connection(cls, connection) -> Tuple[bool, str]:
        """
        Test a saved connection by attempting to access its bucket.

        Returns:
            Tuple of (success: bool, message: str)
        """
        from django.utils import timezone

        if connection is None:
            return False, "No storage connection configured"

        try:
            client = cls.get_client(connection)

            # Try to head the bucket (checks existence and permissions)
            client.head_bucket(Bucket=connection.bucket)

            # Update last tested timestamp
            connection.last_tested_at = timezone.now()
            connection.save(update_fields=["last_tested_at"])

            return True, f"Successfully connected to bucket '{connection.bucket}'"

        except S3ServiceError as e:
            return False, str(e)
        except Exception as e:
            return False, cls._map_boto3_error(str(e), connection.bucket)

    @classmethod
    def test_connection_with_credentials(
        cls,
        bucket_name: str,
        access_key: str,
        secret_key: str,
        endpoint_url: str = "",
        region: str = "us-east-1",
        use_ssl: bool = True,
        path_style: bool = False,
    ) -> Tuple[bool, str]:
        """
        Test S3 connection with provided credentials (without saving).

        This is used to validate credentials before saving a connection.

        Args:
            bucket_name: S3 bucket name
            access_key: AWS access key ID
            secret_key: AWS secret access key
            endpoint_url: Custom endpoint URL (empty for AWS)
            region: AWS region
            use_ssl: Whether to use SSL
            path_style: Use path-style addressing

        Returns:
            Tuple of (success: bool, message: str)
        """
        # Validate required fields
        if not bucket_name:
            return False, "Bucket name is required"
        if not access_key:
            return False, "Access key is required"
        if not secret_key:
            return False, "Secret key is required"

        try:
            import boto3
            from botocore.config import Config
        except ImportError:
            return False, "boto3 is not installed. Install it with: pip install boto3"

        try:
            # Build client config
            config = Config(
                signature_version="s3v4",
                s3={"addressing_style": "path" if path_style else "auto"},
            )

            client_kwargs = {
                "service_name": "s3",
                "aws_access_key_id": access_key,
                "aws_secret_access_key": secret_key,
                "config": config,
                "use_ssl": use_ssl,
            }

            if endpoint_url:
                # Validate endpoint URL to prevent SSRF attacks
                is_safe, error_msg = is_safe_endpoint_url(endpoint_url)
                if not is_safe:
                    return False, f"Invalid S3 endpoint: {error_msg}"
                client_kwargs["endpoint_url"] = endpoint_url

            if region:
                client_kwargs["region_name"] = region

            client = boto3.client(**client_kwargs)

            # Try to head the bucket (checks existence and permissions)
            client.head_bucket(Bucket=bucket_name)

            return True, f"Successfully connected to bucket '{bucket_name}'"

        except Exception as e:
            return False, cls._map_boto3_error(str(e), bucket_name)

    @classmethod
    def is_configured(cls, connection=None) -> bool:
        """Whether ``connection`` (default: the backup connection) is usable."""
        if connection is None:
            connection = cls.for_backup()
        if connection is None:
            return False
        return connection.is_configured

    @classmethod
    def get_status(cls, connection=None) -> dict:
        """
        Get storage configuration status for UI display.

        Returns:
            Dict with status information
        """
        if connection is None:
            connection = cls.for_backup()

        if connection is None:
            return {
                "enabled": False,
                "configured": False,
                "bucket": None,
                "endpoint": "Not configured",
                "region": "",
                "use_ssl": True,
                "path_style": False,
                "last_tested": None,
            }

        return {
            "enabled": connection.enabled,
            "configured": connection.is_configured,
            "bucket": connection.bucket or None,
            "endpoint": connection.endpoint_url or "AWS S3 (default)",
            "region": connection.region or "us-east-1",
            "use_ssl": connection.use_ssl,
            "path_style": connection.path_style,
            "last_tested": connection.last_tested_at,
        }

    # ----------------------------------------------------------------- #
    # Object operations
    # ----------------------------------------------------------------- #

    @classmethod
    def upload_file(
        cls,
        connection,
        file_bytes: bytes,
        key: str,
        content_type: str = "application/gzip",
    ) -> dict:
        """
        Upload a file.

        Args:
            connection: StorageConnection to upload through
            file_bytes: File content as bytes
            key: S3 object key (path)
            content_type: MIME type. Defaults to gzip, which is what the backup
                flow (this method's original and only caller) always sends.

        Returns:
            dict: Upload result with 'success', 'key', 'size', optional 'error'
        """
        try:
            client = cls.get_client(connection)
            client.put_object(
                Bucket=connection.bucket,
                Key=key,
                Body=file_bytes,
                ContentType=content_type,
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
    def get_file(cls, connection, key: str) -> Optional[bytes]:
        """
        Fetch an object's bytes.

        Returns:
            bytes, or None when the key is missing / unreadable.
        """
        try:
            client = cls.get_client(connection)
            response = client.get_object(Bucket=connection.bucket, Key=key)
            return response["Body"].read()
        except S3ServiceError as e:
            logger.error(f"S3 get failed for key {key}: {e}")
            return None
        except Exception:
            # A miss is ordinary control flow for callers (exists-check, cache
            # lookup), so this stays quiet at info level rather than exception.
            logger.info("S3 get failed for key %s", key)
            return None

    @classmethod
    def list_files(cls, connection, prefix: str = "") -> list[dict]:
        """
        List objects under ``prefix``.

        Returns:
            list: List of dicts with 'key', 'size', 'last_modified', 'etag'
        """
        try:
            client = cls.get_client(connection)
            # Paginate so a bucket with >1000 objects (retention=0 + years of
            # backups) isn't silently truncated at the list_objects_v2 cap.
            paginator = client.get_paginator("list_objects_v2")

            files = []
            for page in paginator.paginate(Bucket=connection.bucket, Prefix=prefix):
                for obj in page.get("Contents", []):
                    files.append({
                        "key": obj["Key"],
                        "size": obj["Size"],
                        "last_modified": obj["LastModified"],
                        # Quoted by S3; strip so callers can compare it to an md5.
                        "etag": (obj.get("ETag") or "").strip('"'),
                    })

            return files
        except S3ServiceError as e:
            logger.error(f"S3 list failed for prefix {prefix}: {e}")
            return []
        except Exception as e:
            logger.exception(f"S3 list failed for prefix {prefix}")
            return []

    @classmethod
    def delete_file(cls, connection, key: str) -> bool:
        """
        Delete an object.

        Returns:
            bool: True if deleted successfully
        """
        try:
            client = cls.get_client(connection)
            client.delete_object(
                Bucket=connection.bucket,
                Key=key,
            )
            return True
        except Exception as e:
            logger.exception(f"S3 delete failed for key {key}")
            return False

    @classmethod
    def delete_files(cls, connection, keys: list[str]) -> int:
        """
        Delete multiple objects.

        Returns:
            int: Number of objects deleted
        """
        if not keys:
            return 0

        try:
            client = cls.get_client(connection)
            deleted = 0
            # delete_objects caps at 1000 keys per call — chunk so a big prefix
            # doesn't fail (or worse, silently delete only the first 1000).
            for i in range(0, len(keys), _DELETE_CHUNK):
                chunk = keys[i:i + _DELETE_CHUNK]
                response = client.delete_objects(
                    Bucket=connection.bucket,
                    Delete={
                        "Objects": [{"Key": key} for key in chunk],
                    },
                )
                deleted += len(response.get("Deleted", []))
            return deleted
        except Exception as e:
            logger.exception("S3 bulk delete failed")
            return 0

    @classmethod
    def delete_prefix(cls, connection, prefix: str) -> int:
        """
        Delete every object under ``prefix``.

        Guards against an empty prefix: that would mean "delete the whole bucket",
        which no caller ever legitimately wants here.

        Returns:
            int: Number of objects deleted
        """
        if not prefix:
            logger.warning("delete_prefix called with an empty prefix — refusing")
            return 0

        files = cls.list_files(connection, prefix)
        if not files:
            return 0

        return cls.delete_files(connection, [f["key"] for f in files])

    @classmethod
    def presigned_url(
        cls, connection, key: str, expires_in: int = DEFAULT_PRESIGN_SECONDS
    ) -> Optional[str]:
        """
        Build a temporary signed GET URL for a private object.

        ``expires_in`` is clamped to [1, MAX_PRESIGN_SECONDS]; SigV4 will not sign
        for longer than 7 days, and a silently-too-long request would just produce
        a URL that never works.

        Returns:
            str, or None when the URL could not be signed.
        """
        expires_in = max(1, min(int(expires_in), MAX_PRESIGN_SECONDS))

        try:
            client = cls.get_client(connection)
            return client.generate_presigned_url(
                "get_object",
                Params={"Bucket": connection.bucket, "Key": key},
                ExpiresIn=expires_in,
            )
        except S3ServiceError as e:
            logger.error(f"S3 presign failed for key {key}: {e}")
            return None
        except Exception:
            logger.exception(f"S3 presign failed for key {key}")
            return None

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

        from core.models import GlobalSettings

        settings = GlobalSettings.get_settings()

        if timestamp is None:
            timestamp = timezone.now()

        prefix = settings.s3_backup_prefix.rstrip("/")
        filename = f"backup_{timestamp.strftime('%Y%m%d_%H%M%S')}.json.gz"

        return f"{prefix}/{filename}" if prefix else filename
