"""
PyRunner object storage API for scripts.

Read and write files in your plugin's own storage space — server-side, so your
script never handles S3 credentials and never needs boto3 installed. Mirrors
``pyrunner_notify``: stdlib-only, talking to PyRunner's internal loopback API
authenticated by a signed per-run token.

Every key is confined to your plugin's own prefix (``apps/<your-plugin>/``),
which PyRunner derives from the run itself — you cannot name, read, or overwrite
another plugin's files, and you never write the prefix yourself.

v1 is available to PLUGIN-OWNED scripts only; a user-created script gets
``StorageError``. Requires an assets connection under Services → Object Storage.

Usage:
    import pyrunner_storage

    pyrunner_storage.put("avatars/abc.jpg", data, content_type="image/jpeg")
    raw = pyrunner_storage.get("avatars/abc.jpg")
    link = pyrunner_storage.url("avatars/abc.jpg")     # public or presigned
    for obj in pyrunner_storage.list("avatars/"):
        print(obj["key"], obj["size"])
    pyrunner_storage.delete("avatars/abc.jpg")
"""

import base64
import json
import os
import urllib.error
import urllib.request

# Matches the server's cap (core/views/api/storage_internal.py). Checked locally
# too so a large object fails immediately with a clear message instead of after
# uploading 25 MB just to be refused.
MAX_PUT_BYTES = 25 * 1024 * 1024


class StorageError(Exception):
    """Raised when a storage operation cannot be completed."""


def _post(endpoint: str, payload: dict) -> dict:
    base = os.environ.get("PYRUNNER_INTERNAL_URL")
    token = os.environ.get("PYRUNNER_INTERNAL_TOKEN")
    if not base or not token:
        raise StorageError("PyRunner storage is not available in this run context.")
    url = f"{base.rstrip('/')}/internal/storage/{endpoint}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            body = json.loads(raw) if raw else {}
            message = body.get("error", {}).get("message") or raw.decode("utf-8", "ignore")
        except ValueError:
            message = raw.decode("utf-8", "ignore")
        # 404 is ordinary control flow for get(); the caller turns it into None.
        if e.code == 404:
            raise FileNotFoundError(message)
        raise StorageError(f"Storage {endpoint} failed (HTTP {e.code}): {message}")
    except urllib.error.URLError as e:
        raise StorageError(f"Could not reach PyRunner: {e}")


def put(key: str, data, content_type: str = "application/octet-stream") -> str:
    """Store ``data`` (bytes or str) at ``key``. Returns the key.

    ``key`` is relative to your plugin's own space, e.g. "avatars/abc.jpg".
    """
    if isinstance(data, str):
        data = data.encode("utf-8")
    if not isinstance(data, (bytes, bytearray)):
        raise StorageError(f"data must be bytes or str, got {type(data).__name__}")
    if len(data) > MAX_PUT_BYTES:
        raise StorageError(
            f"Object is {len(data)} bytes, over the {MAX_PUT_BYTES} byte limit."
        )

    result = _post(
        "put",
        {
            "key": key,
            "data": base64.b64encode(bytes(data)).decode("ascii"),
            "content_type": content_type,
        },
    )
    return result.get("key", key)


def get(key: str):
    """Return the object's bytes, or None when it doesn't exist."""
    try:
        result = _post("get", {"key": key})
    except FileNotFoundError:
        return None
    encoded = result.get("data")
    if encoded is None:
        return None
    return base64.b64decode(encoded)


def delete(key: str) -> bool:
    """Delete one object. True when the delete was issued cleanly."""
    return bool(_post("delete", {"key": key}).get("ok"))


def list(prefix: str = "") -> "list[dict]":  # noqa: A001 - the natural name here
    """List your plugin's objects as dicts of key, size, last_modified, etag.

    Keys come back relative to your plugin's space, ready to pass to get()/url().
    """
    return _post("list", {"prefix": prefix}).get("objects", [])


def url(key: str, expires_in: int = 3600) -> str:
    """A URL for the object.

    If the instance's assets connection has a public base URL, this is permanent
    and hot-linkable. Otherwise it is a presigned URL that EXPIRES after
    ``expires_in`` seconds (7-day maximum) — don't bake it into a long-lived page
    or an email.
    """
    return _post("url", {"key": key, "expires_in": expires_in}).get("url")
