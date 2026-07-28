"""
Storage seam (object storage as a platform capability) — Stage 1 + Stage 2.

Covers the full seam without a real bucket (boto3 is mocked throughout; the
``is_safe_endpoint_url`` SSRF guard blocks localhost, so a local MinIO is not a
usable test target — see docs/PLAN_storage_seam.md):

- StorageConnection: single-default invariant, is_configured/is_public
- migration contract: connection config moved off GlobalSettings, backup policy
  did not
- StorageConnectionForm / AssetsStorageForm round-trip (creds encrypted, blank
  keeps stored, endpoint SSRF-validated at save time)
- StorageAPI prefix enforcement, including every traversal shape
- fail-closed unavailability (no assets connection => is_available() False)
- url() in both modes: permanent public vs expiring presigned (+ 7-day clamp)
- the internal loopback API: token auth, server-side owner derivation,
  plugin-only v1 scope, size cap, per-run write brake
- pyrunner_storage helper contract (no boto3, no credentials in the run env)
- backup regression: backups still resolve the default connection and still
  upload gzip
"""

import base64
import json
from datetime import datetime, timezone as dt_timezone
from unittest import mock

from cryptography.fernet import Fernet
from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import (
    Environment,
    GlobalSettings,
    Run,
    Script,
    StorageConnection,
    User,
    Workspace,
)
from core.plugins.api import StorageAPI, StorageError, StorageKeyError
from core.script_helpers import pyrunner_storage
from core.services.datastore_token import mint_datastore_token
from core.services.s3_service import MAX_PRESIGN_SECONDS, S3Service

_TEST_KEY = Fernet.generate_key().decode()

OWNER = "test_plugin"


def _default_ws():
    """The default workspace keys are scoped to (created by the tenancy backfill)."""
    return Workspace.get_default()


def _prefix(owner=OWNER, workspace=None):
    """The expected bucket prefix: plugin slug first, then workspace."""
    ws = workspace or _default_ws()
    return f"apps/{owner}/{ws.id}/"


def _auth(token):
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


def _make_connection(**kwargs):
    defaults = {
        "name": "Assets",
        "provider_type": StorageConnection.ProviderType.R2,
        "endpoint_url": "https://acct.r2.cloudflarestorage.com",
        "region": "auto",
        "bucket": "assets-bucket",
        "access_key_encrypted": "enc-access",
        "secret_key_encrypted": "enc-secret",
        "enabled": True,
    }
    defaults.update(kwargs)
    return StorageConnection.objects.create(**defaults)


def _use_for_assets(connection):
    settings = GlobalSettings.get_settings()
    settings.assets_storage = connection
    settings.save(update_fields=["assets_storage"])
    return settings


# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #

class StorageConnectionModelTests(TestCase):
    def test_only_one_default_survives(self):
        """The backup path resolves is_default on every run — two defaults would
        make the destination depend on row ordering."""
        a = _make_connection(name="A", is_default=True)
        b = _make_connection(name="B", is_default=True)

        a.refresh_from_db()
        b.refresh_from_db()
        self.assertFalse(a.is_default)
        self.assertTrue(b.is_default)
        self.assertEqual(StorageConnection.objects.filter(is_default=True).count(), 1)
        self.assertEqual(StorageConnection.get_default(), b)

    def test_resaving_the_default_keeps_it_default(self):
        a = _make_connection(name="A", is_default=True)
        a.region = "us-east-1"
        a.save()

        a.refresh_from_db()
        self.assertTrue(a.is_default)

    def test_is_configured_requires_bucket_and_both_keys(self):
        self.assertTrue(_make_connection(name="full").is_configured)
        self.assertFalse(_make_connection(name="nobucket", bucket="").is_configured)
        self.assertFalse(
            _make_connection(name="nokey", access_key_encrypted="").is_configured
        )
        self.assertFalse(
            _make_connection(name="nosecret", secret_key_encrypted="").is_configured
        )

    def test_is_public_tracks_public_base_url(self):
        self.assertFalse(_make_connection(name="private").is_public)
        self.assertTrue(
            _make_connection(name="public", public_base_url="https://cdn.example.com").is_public
        )


class GlobalSettingsFieldMoveTests(TestCase):
    """Migration 0053 moved connection config off GlobalSettings but deliberately
    left backup POLICY on it. Both halves are asserted so a later 'cleanup' can't
    quietly move the wrong one."""

    def test_connection_fields_are_gone(self):
        settings = GlobalSettings.get_settings()
        for field in (
            "s3_enabled",
            "s3_endpoint_url",
            "s3_region",
            "s3_bucket_name",
            "s3_access_key_encrypted",
            "s3_secret_key_encrypted",
            "s3_use_ssl",
            "s3_path_style",
            "s3_last_tested_at",
        ):
            self.assertFalse(hasattr(settings, field), f"{field} should have moved")

    def test_backup_policy_fields_remain(self):
        settings = GlobalSettings.get_settings()
        for field in (
            "s3_backup_enabled",
            "s3_backup_schedule",
            "s3_backup_prefix",
            "s3_backup_retention_count",
            "s3_backup_include_runs",
            "s3_backup_last_status",
        ):
            self.assertTrue(hasattr(settings, field), f"{field} should have stayed")


# --------------------------------------------------------------------------- #
# Forms (settings round-trip)
# --------------------------------------------------------------------------- #

@override_settings(PYRUNNER_ENCRYPTION_KEY=_TEST_KEY)
class StorageConnectionFormTests(TestCase):
    def _post_data(self, **overrides):
        data = {
            "provider_type": StorageConnection.ProviderType.R2,
            "name": "Assets",
            "endpoint_url": "https://acct.r2.cloudflarestorage.com",
            "region": "auto",
            "bucket": "assets-bucket",
            "access_key": "AKIA-EXAMPLE",
            "secret_key": "s3cr3t",
            "public_base_url": "https://cdn.example.com/",
            "use_ssl": True,
        }
        data.update(overrides)
        return data

    def test_create_encrypts_credentials_and_round_trips(self):
        from core.forms import StorageConnectionForm
        from core.services.encryption_service import EncryptionService

        form = StorageConnectionForm(self._post_data())
        self.assertTrue(form.is_valid(), form.errors)
        connection = form.save()

        self.assertEqual(connection.bucket, "assets-bucket")
        # Stored encrypted, not in the clear...
        self.assertNotIn("AKIA-EXAMPLE", connection.access_key_encrypted)
        self.assertNotIn("s3cr3t", connection.secret_key_encrypted)
        # ...and decryptable back to what was typed.
        self.assertEqual(
            EncryptionService.decrypt(connection.access_key_encrypted), "AKIA-EXAMPLE"
        )
        self.assertEqual(
            EncryptionService.decrypt(connection.secret_key_encrypted), "s3cr3t"
        )
        # Trailing slash normalized so url() never builds a double slash.
        self.assertEqual(connection.public_base_url, "https://cdn.example.com")

    def test_blank_credentials_on_edit_keep_the_stored_ones(self):
        from core.forms import StorageConnectionForm

        existing = _make_connection(name="Assets")
        form = StorageConnectionForm(
            self._post_data(access_key="", secret_key=""), instance=existing
        )
        self.assertTrue(form.is_valid(), form.errors)
        connection = form.save()

        self.assertEqual(connection.access_key_encrypted, "enc-access")
        self.assertEqual(connection.secret_key_encrypted, "enc-secret")

    def test_credentials_required_on_create(self):
        from core.forms import StorageConnectionForm

        form = StorageConnectionForm(self._post_data(access_key="", secret_key=""))
        self.assertFalse(form.is_valid())
        self.assertIn("access_key", form.errors)
        self.assertIn("secret_key", form.errors)

    def test_private_endpoint_rejected_at_save_time(self):
        """The SSRF guard runs in get_client, but catching it in the form turns a
        mystery backup failure into an immediate, fixable error."""
        from core.forms import StorageConnectionForm

        form = StorageConnectionForm(
            self._post_data(
                provider_type=StorageConnection.ProviderType.MINIO,
                endpoint_url="http://localhost:9000",
            )
        )
        self.assertFalse(form.is_valid())
        self.assertIn("endpoint_url", form.errors)

    def test_endpoint_required_except_for_aws(self):
        from core.forms import StorageConnectionForm

        form = StorageConnectionForm(self._post_data(endpoint_url=""))
        self.assertFalse(form.is_valid())
        self.assertIn("endpoint_url", form.errors)

        form = StorageConnectionForm(
            self._post_data(
                provider_type=StorageConnection.ProviderType.S3, endpoint_url=""
            )
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_duplicate_name_rejected(self):
        from core.forms import StorageConnectionForm

        _make_connection(name="Assets")
        form = StorageConnectionForm(self._post_data(name="assets"))
        self.assertFalse(form.is_valid())
        self.assertIn("name", form.errors)

    def test_public_base_url_must_be_absolute(self):
        from core.forms import StorageConnectionForm

        form = StorageConnectionForm(self._post_data(public_base_url="cdn.example.com"))
        self.assertFalse(form.is_valid())
        self.assertIn("public_base_url", form.errors)

    def test_assets_form_selects_and_clears(self):
        from core.forms import AssetsStorageForm

        connection = _make_connection(name="Assets")
        settings = GlobalSettings.get_settings()

        form = AssetsStorageForm({"assets_storage": str(connection.id)}, instance=settings)
        self.assertTrue(form.is_valid(), form.errors)
        form.save(settings)
        settings.refresh_from_db()
        self.assertEqual(settings.assets_storage_id, connection.id)

        # Blank => seam unavailable, which is a legitimate choice.
        form = AssetsStorageForm({"assets_storage": ""}, instance=settings)
        self.assertTrue(form.is_valid(), form.errors)
        form.save(settings)
        settings.refresh_from_db()
        self.assertIsNone(settings.assets_storage_id)

    def test_disabling_the_selected_connection_does_not_clear_the_selection(self):
        """A disabled connection makes storage unavailable — it must not silently
        erase WHICH connection was chosen the next time this card is saved."""
        from core.forms import AssetsStorageForm

        connection = _make_connection(name="Assets")
        settings = _use_for_assets(connection)

        connection.enabled = False
        connection.save(update_fields=["enabled"])

        # Still offered (so the card shows the real selection)...
        form = AssetsStorageForm(instance=settings)
        self.assertIn(connection, form.fields["assets_storage"].queryset)

        # ...and re-submitting it validates instead of "not a valid choice".
        form = AssetsStorageForm(
            {"assets_storage": str(connection.id)}, instance=settings
        )
        self.assertTrue(form.is_valid(), form.errors)
        form.save(settings)
        settings.refresh_from_db()
        self.assertEqual(settings.assets_storage_id, connection.id)


# --------------------------------------------------------------------------- #
# StorageAPI — prefix enforcement + availability
# --------------------------------------------------------------------------- #

class StorageAPIPrefixTests(TestCase):
    """The load-bearing property: a plugin cannot express another plugin's key —
    nor another workspace's."""

    def setUp(self):
        self.storage = StorageAPI(OWNER)

    def test_prefix_is_owner_and_workspace_scoped(self):
        self.assertEqual(self.storage.prefix, _prefix())

    def test_owner_root_spans_every_workspace(self):
        # Uninstall's level: plugin slug first, so one prefix delete clears all.
        self.assertEqual(self.storage.owner_root, f"apps/{OWNER}/")
        self.assertTrue(self.storage.prefix.startswith(self.storage.owner_root))

    def test_keys_are_forced_under_the_owner_prefix(self):
        self.assertEqual(
            self.storage._full_key("avatars/a.jpg"), f"{_prefix()}avatars/a.jpg"
        )

    def test_workspaces_do_not_share_a_prefix(self):
        """The reason storage is workspace-scoped: the same plugin writing the
        same key in two workspaces must not be one object."""
        other = Workspace.objects.create(name="Other")

        default_key = StorageAPI(OWNER)._full_key("avatars/a.jpg")
        other_key = StorageAPI(OWNER, workspace=other)._full_key("avatars/a.jpg")

        self.assertNotEqual(default_key, other_key)
        self.assertTrue(other_key.startswith(f"apps/{OWNER}/{other.id}/"))

    def test_workspace_accepts_an_instance_or_a_bare_id(self):
        # The loopback endpoint derives only an id from the signed run token.
        other = Workspace.objects.create(name="Other")

        self.assertEqual(
            StorageAPI(OWNER, workspace=other).prefix,
            StorageAPI(OWNER, workspace=other.id).prefix,
        )

    def test_no_resolvable_workspace_fails_closed(self):
        # Never build apps/<owner>/None/ — that would pool every workspace's
        # objects under one prefix.
        Workspace.objects.update(is_default=False)

        with self.assertRaises(StorageError):
            StorageAPI(OWNER).prefix
        self.assertFalse(StorageAPI(OWNER).is_available())

    def test_owner_is_mandatory(self):
        # No unowned lane: a prefix-less key IS the escape being prevented.
        with self.assertRaises(StorageKeyError):
            StorageAPI(None)
        with self.assertRaises(StorageKeyError):
            StorageAPI("")

    def test_traversal_attempts_are_rejected_loudly(self):
        # Rejected, never sanitized: silently rewriting turns an escape attempt
        # into a write to the wrong key.
        for bad in (
            "../other_plugin/secret.txt",
            "avatars/../../other_plugin/x",
            "..",
            "../",
            "a/../../b",
            "/etc/passwd",
            "/absolute",
            "..\\other",
            "windows\\path",
            "nul\x00byte",
        ):
            with self.subTest(key=bad):
                with self.assertRaises(StorageKeyError):
                    self.storage._full_key(bad)

    def test_empty_and_non_string_keys_rejected(self):
        for bad in ("", "   ", None, 42, b"bytes"):
            with self.subTest(key=bad):
                with self.assertRaises(StorageKeyError):
                    self.storage._full_key(bad)

    def test_dotdot_only_rejected_as_a_whole_segment(self):
        # A filename that merely CONTAINS ".." is legitimate.
        self.assertEqual(
            self.storage._full_key("weird..name.jpg"), f"{_prefix()}weird..name.jpg"
        )

    def test_relative_key_strips_the_prefix(self):
        self.assertEqual(
            self.storage._relative_key(f"{_prefix()}avatars/a.jpg"), "avatars/a.jpg"
        )


class StorageAPIAvailabilityTests(TestCase):
    """Fail-closed: no assets connection => unavailable, never a silent fallback."""

    def test_unavailable_without_an_assets_connection(self):
        self.assertFalse(StorageAPI(OWNER).is_available())

    def test_unavailable_when_assets_connection_is_disabled(self):
        _use_for_assets(_make_connection(enabled=False))
        self.assertFalse(StorageAPI(OWNER).is_available())

    def test_unavailable_when_assets_connection_is_incomplete(self):
        _use_for_assets(_make_connection(bucket=""))
        self.assertFalse(StorageAPI(OWNER).is_available())

    def test_available_with_a_configured_assets_connection(self):
        _use_for_assets(_make_connection())
        self.assertTrue(StorageAPI(OWNER).is_available())

    def test_does_not_fall_back_to_the_backup_default(self):
        """The whole reason assets_storage has no fallback: plugin objects must
        never land in the private backup bucket."""
        _make_connection(name="Backups", bucket="backup-bucket", is_default=True)

        self.assertIsNotNone(S3Service.for_backup())
        self.assertIsNone(S3Service.for_assets())
        self.assertFalse(StorageAPI(OWNER).is_available())

    def test_operations_raise_when_unavailable(self):
        storage = StorageAPI(OWNER)
        with self.assertRaises(StorageError):
            storage.put("a.txt", b"x")
        with self.assertRaises(StorageError):
            storage.get("a.txt")
        with self.assertRaises(StorageError):
            storage.list()
        with self.assertRaises(StorageError):
            storage.url("a.txt")

    def test_delete_all_is_a_noop_when_unavailable(self):
        # Called from plugin uninstall, which must never fail on a missing seam.
        self.assertEqual(StorageAPI(OWNER).delete_all(), 0)


# --------------------------------------------------------------------------- #
# StorageAPI — object operations
# --------------------------------------------------------------------------- #

class StorageAPIOperationTests(TestCase):
    def setUp(self):
        self.connection = _make_connection()
        _use_for_assets(self.connection)
        self.storage = StorageAPI(OWNER)

    def test_put_prefixes_the_key_and_passes_content_type(self):
        client = mock.Mock()
        with mock.patch.object(S3Service, "get_client", return_value=client):
            key = self.storage.put("avatars/a.jpg", b"bytes", content_type="image/jpeg")

        self.assertEqual(key, "avatars/a.jpg")  # caller sees its own relative key
        _, kwargs = client.put_object.call_args
        self.assertEqual(kwargs["Bucket"], "assets-bucket")
        self.assertEqual(kwargs["Key"], f"{_prefix()}avatars/a.jpg")
        self.assertEqual(kwargs["ContentType"], "image/jpeg")
        self.assertEqual(kwargs["Body"], b"bytes")

    def test_put_encodes_str_as_utf8(self):
        client = mock.Mock()
        with mock.patch.object(S3Service, "get_client", return_value=client):
            self.storage.put("note.txt", "héllo")
        self.assertEqual(client.put_object.call_args.kwargs["Body"], "héllo".encode())

    def test_put_rejects_bad_types(self):
        with mock.patch.object(S3Service, "get_client", return_value=mock.Mock()):
            with self.assertRaises(StorageError):
                self.storage.put("a.bin", 12345)

    def test_put_raises_rather_than_returning_false(self):
        # A silent failure would let a plugin believe it archived something.
        with mock.patch.object(
            S3Service, "upload_file", return_value={"success": False, "error": "boom"}
        ):
            with self.assertRaises(StorageError):
                self.storage.put("a.txt", b"x")

    def test_put_rejects_traversal_before_touching_s3(self):
        client = mock.Mock()
        with mock.patch.object(S3Service, "get_client", return_value=client):
            with self.assertRaises(StorageKeyError):
                self.storage.put("../escape.txt", b"x")
        client.put_object.assert_not_called()

    def test_get_returns_bytes_and_prefixes(self):
        client = mock.Mock()
        client.get_object.return_value = {"Body": mock.Mock(read=lambda: b"data")}
        with mock.patch.object(S3Service, "get_client", return_value=client):
            self.assertEqual(self.storage.get("a.txt"), b"data")
        self.assertEqual(
            client.get_object.call_args.kwargs["Key"], f"{_prefix()}a.txt"
        )

    def test_get_returns_none_when_missing(self):
        client = mock.Mock()
        client.get_object.side_effect = Exception("NoSuchKey")
        with mock.patch.object(S3Service, "get_client", return_value=client):
            self.assertIsNone(self.storage.get("missing.txt"))

    def test_delete_prefixes(self):
        client = mock.Mock()
        with mock.patch.object(S3Service, "get_client", return_value=client):
            self.assertTrue(self.storage.delete("a.txt"))
        self.assertEqual(
            client.delete_object.call_args.kwargs["Key"], f"{_prefix()}a.txt"
        )

    def test_list_returns_relative_keys_with_metadata(self):
        client = mock.Mock()
        paginator = mock.Mock()
        when = datetime(2026, 7, 17, 12, 0, tzinfo=dt_timezone.utc)
        paginator.paginate.return_value = [
            {
                "Contents": [
                    {
                        "Key": f"{_prefix()}avatars/a.jpg",
                        "Size": 10,
                        "LastModified": when,
                        "ETag": '"abc123"',
                    }
                ]
            }
        ]
        client.get_paginator.return_value = paginator
        with mock.patch.object(S3Service, "get_client", return_value=client):
            objects = self.storage.list()

        self.assertEqual(
            objects,
            [{"key": "avatars/a.jpg", "size": 10, "last_modified": when, "etag": "abc123"}],
        )
        # Empty prefix means "everything of MINE", not "everything".
        self.assertEqual(
            paginator.paginate.call_args.kwargs["Prefix"], f"{_prefix()}"
        )

    def test_list_prefix_cannot_escape(self):
        with mock.patch.object(S3Service, "get_client", return_value=mock.Mock()):
            with self.assertRaises(StorageKeyError):
                self.storage.list("../")

    def test_delete_all_spans_every_workspace(self):
        """Uninstall is instance-wide (the owned-row cleanup beside it has no
        workspace clause either), so delete_all uses owner_root — leaving one
        workspace's files behind would strand them unreachable."""
        client = mock.Mock()
        paginator = mock.Mock()
        paginator.paginate.return_value = [
            {"Contents": [{"Key": f"apps/{OWNER}/a", "Size": 1, "LastModified": "t"}]}
        ]
        client.get_paginator.return_value = paginator
        client.delete_objects.return_value = {"Deleted": [{"Key": f"apps/{OWNER}/a"}]}

        with mock.patch.object(S3Service, "get_client", return_value=client):
            self.assertEqual(self.storage.delete_all(), 1)

        self.assertEqual(paginator.paginate.call_args.kwargs["Prefix"], f"apps/{OWNER}/")

    def test_delete_all_needs_no_default_workspace(self):
        # Uninstall must work on an instance with no default workspace: it is
        # workspace-independent, so resolution stays lazy.
        Workspace.objects.update(is_default=False)
        client = mock.Mock()
        paginator = mock.Mock()
        paginator.paginate.return_value = [{"Contents": []}]
        client.get_paginator.return_value = paginator

        with mock.patch.object(S3Service, "get_client", return_value=client):
            self.assertEqual(StorageAPI(OWNER).delete_all(), 0)


class StorageAPIUrlTests(TestCase):
    """url() has two modes and the difference matters: presigned URLs rot."""

    def test_public_mode_returns_a_permanent_url(self):
        _use_for_assets(_make_connection(public_base_url="https://cdn.example.com"))

        url = StorageAPI(OWNER).url("avatars/a.jpg")

        self.assertEqual(url, f"https://cdn.example.com/{_prefix()}avatars/a.jpg")

    def test_public_mode_never_doubles_the_slash(self):
        connection = _make_connection(public_base_url="https://cdn.example.com")
        # Belt and braces: the form strips it, but a row edited elsewhere may not.
        connection.public_base_url = "https://cdn.example.com/"
        connection.save()
        _use_for_assets(connection)

        self.assertEqual(
            StorageAPI(OWNER).url("a.jpg"), f"https://cdn.example.com/{_prefix()}a.jpg"
        )

    def test_private_mode_presigns_with_the_prefixed_key(self):
        _use_for_assets(_make_connection())
        client = mock.Mock()
        client.generate_presigned_url.return_value = "https://signed.example/x"

        with mock.patch.object(S3Service, "get_client", return_value=client):
            url = StorageAPI(OWNER).url("avatars/a.jpg", expires_in=60)

        self.assertEqual(url, "https://signed.example/x")
        _, kwargs = client.generate_presigned_url.call_args
        self.assertEqual(kwargs["Params"]["Bucket"], "assets-bucket")
        self.assertEqual(kwargs["Params"]["Key"], f"{_prefix()}avatars/a.jpg")
        self.assertEqual(kwargs["ExpiresIn"], 60)

    def test_presign_expiry_is_clamped_to_the_sigv4_ceiling(self):
        # SigV4 refuses to sign past 7 days; an unclamped request would just make
        # a URL that never works.
        _use_for_assets(_make_connection())
        client = mock.Mock()
        with mock.patch.object(S3Service, "get_client", return_value=client):
            StorageAPI(OWNER).url("a.jpg", expires_in=999_999_999)
        self.assertEqual(
            client.generate_presigned_url.call_args.kwargs["ExpiresIn"],
            MAX_PRESIGN_SECONDS,
        )

    def test_presign_expiry_floor(self):
        _use_for_assets(_make_connection())
        client = mock.Mock()
        with mock.patch.object(S3Service, "get_client", return_value=client):
            StorageAPI(OWNER).url("a.jpg", expires_in=0)
        self.assertEqual(client.generate_presigned_url.call_args.kwargs["ExpiresIn"], 1)


# --------------------------------------------------------------------------- #
# S3Service helpers
# --------------------------------------------------------------------------- #

class S3ServiceHelperTests(TestCase):
    def test_delete_files_chunks_at_the_api_cap(self):
        # delete_objects caps at 1000 keys; an unchunked call would fail, or worse
        # silently delete only the first 1000.
        connection = _make_connection()
        client = mock.Mock()
        client.delete_objects.side_effect = lambda **kw: {
            "Deleted": kw["Delete"]["Objects"]
        }

        with mock.patch.object(S3Service, "get_client", return_value=client):
            deleted = S3Service.delete_files(connection, [f"k{i}" for i in range(2500)])

        self.assertEqual(deleted, 2500)
        self.assertEqual(client.delete_objects.call_count, 3)

    def test_delete_prefix_refuses_an_empty_prefix(self):
        # An empty prefix would mean "delete the whole bucket".
        connection = _make_connection()
        client = mock.Mock()
        with mock.patch.object(S3Service, "get_client", return_value=client):
            self.assertEqual(S3Service.delete_prefix(connection, ""), 0)
        client.delete_objects.assert_not_called()

    def test_get_client_requires_a_connection(self):
        from core.services.s3_service import S3ServiceError

        with self.assertRaises(S3ServiceError):
            S3Service.get_client(None)


# --------------------------------------------------------------------------- #
# Internal loopback API
# --------------------------------------------------------------------------- #

@override_settings(ALLOWED_HOSTS=["*"])
class InternalStorageAPITests(TestCase):
    """Auth, server-side owner derivation, v1 scope, size cap, per-run brake."""

    def setUp(self):
        self.env = Environment.objects.create(name="test-env", path="/tmp/env")
        self.plugin_script = Script.objects.create(
            name="worker", code="print(1)", environment=self.env, owner_plugin=OWNER
        )
        self.user_script = Script.objects.create(
            name="mine", code="print(1)", environment=self.env
        )
        self.plugin_run = Run.objects.create(script=self.plugin_script)
        self.user_run = Run.objects.create(script=self.user_script)

        self.connection = _make_connection()
        _use_for_assets(self.connection)

    def _post(self, endpoint, payload, run=None, **extra):
        run = run or self.plugin_run
        return self.client.post(
            reverse(f"internal:storage_{endpoint}"),
            data=json.dumps(payload),
            content_type="application/json",
            REMOTE_ADDR="127.0.0.1",
            **_auth(mint_datastore_token(run.id)),
            **extra,
        )

    # ---- auth ----

    def test_requires_a_token(self):
        response = self.client.post(
            reverse("internal:storage_put"),
            data=json.dumps({"key": "a", "data": ""}),
            content_type="application/json",
            REMOTE_ADDR="127.0.0.1",
        )
        self.assertEqual(response.status_code, 401)

    def test_rejects_an_invalid_token(self):
        response = self.client.post(
            reverse("internal:storage_put"),
            data=json.dumps({"key": "a", "data": ""}),
            content_type="application/json",
            REMOTE_ADDR="127.0.0.1",
            **_auth("not-a-real-token"),
        )
        self.assertEqual(response.status_code, 401)

    def test_rejects_non_loopback_callers(self):
        response = self.client.post(
            reverse("internal:storage_put"),
            data=json.dumps({"key": "a", "data": ""}),
            content_type="application/json",
            REMOTE_ADDR="10.0.0.5",
            **_auth(mint_datastore_token(self.plugin_run.id)),
        )
        self.assertEqual(response.status_code, 403)

    # ---- v1 scope + owner derivation ----

    def test_user_script_run_is_refused(self):
        """v1 is plugin-only, enforced server-side from the signed run id."""
        response = self._post("put", {"key": "a.txt", "data": ""}, run=self.user_run)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "STORAGE_PLUGIN_ONLY")

    def test_owner_comes_from_the_run_not_the_request(self):
        """A script cannot name another plugin's prefix: the owner is derived from
        the signed run id, and any owner-ish field in the body is ignored."""
        client = mock.Mock()
        with mock.patch.object(S3Service, "get_client", return_value=client):
            response = self._post(
                "put",
                {
                    "key": "a.txt",
                    "data": base64.b64encode(b"x").decode(),
                    "owner": "other_plugin",  # ignored
                    "prefix": "apps/other_plugin/",  # ignored
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            client.put_object.call_args.kwargs["Key"], f"{_prefix()}a.txt"
        )

    def test_workspace_comes_from_the_run_not_the_request(self):
        """The second scoping axis, derived server-side like the first: a run in
        workspace B cannot address workspace A's space by asking."""
        other = Workspace.objects.create(name="Other")
        self.plugin_script.workspace = other
        self.plugin_script.save(update_fields=["workspace"])
        run = Run.objects.create(script=self.plugin_script, workspace=other)

        client = mock.Mock()
        with mock.patch.object(S3Service, "get_client", return_value=client):
            response = self._post(
                "put",
                {
                    "key": "a.txt",
                    "data": base64.b64encode(b"x").decode(),
                    "workspace": str(_default_ws().id),  # ignored
                },
                run=run,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            client.put_object.call_args.kwargs["Key"], f"apps/{OWNER}/{other.id}/a.txt"
        )

    def test_traversal_key_is_a_400(self):
        response = self._post(
            "put", {"key": "../other/a.txt", "data": base64.b64encode(b"x").decode()}
        )
        self.assertEqual(response.status_code, 400)

    def test_unavailable_seam_is_503(self):
        settings = GlobalSettings.get_settings()
        settings.assets_storage = None
        settings.save(update_fields=["assets_storage"])

        response = self._post("put", {"key": "a.txt", "data": ""})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "STORAGE_UNAVAILABLE")

    # ---- size cap ----

    def test_oversized_payload_is_413(self):
        from core.views.api.storage_internal import MAX_PUT_BYTES

        oversized = base64.b64encode(b"x" * (MAX_PUT_BYTES + 1)).decode()
        response = self._post("put", {"key": "big.bin", "data": oversized})
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["error"]["code"], "TOO_LARGE")

    def test_payload_at_the_cap_is_accepted(self):
        from core.views.api.storage_internal import MAX_PUT_BYTES

        client = mock.Mock()
        at_cap = base64.b64encode(b"x" * MAX_PUT_BYTES).decode()
        with mock.patch.object(S3Service, "get_client", return_value=client):
            response = self._post("put", {"key": "big.bin", "data": at_cap})
        self.assertEqual(response.status_code, 200)

    def test_invalid_base64_is_400(self):
        response = self._post("put", {"key": "a.txt", "data": "not!base64!"})
        self.assertEqual(response.status_code, 400)

    def test_malformed_json_is_400(self):
        response = self.client.post(
            reverse("internal:storage_put"),
            data="{not json",
            content_type="application/json",
            REMOTE_ADDR="127.0.0.1",
            **_auth(mint_datastore_token(self.plugin_run.id)),
        )
        self.assertEqual(response.status_code, 400)

    # ---- per-run brake ----

    def test_write_brake_limits_a_runaway_run(self):
        from core.views.api.storage_internal import _RUN_WRITE_LIMIT

        client = mock.Mock()
        payload = {"key": "a.txt", "data": base64.b64encode(b"x").decode()}
        with mock.patch.object(S3Service, "get_client", return_value=client):
            for _ in range(_RUN_WRITE_LIMIT):
                self.assertEqual(self._post("put", payload).status_code, 200)

            response = self._post("put", payload)

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["error"]["code"], "RATE_LIMITED")

    def test_reads_are_not_braked(self):
        from core.views.api.storage_internal import _RUN_WRITE_LIMIT

        client = mock.Mock()
        client.get_object.return_value = {"Body": mock.Mock(read=lambda: b"d")}
        with mock.patch.object(S3Service, "get_client", return_value=client):
            for _ in range(_RUN_WRITE_LIMIT + 5):
                response = self._post("get", {"key": "a.txt"})
        self.assertEqual(response.status_code, 200)

    # ---- round trip ----

    def test_get_round_trips_base64(self):
        client = mock.Mock()
        client.get_object.return_value = {"Body": mock.Mock(read=lambda: b"\x00\x01binary")}
        with mock.patch.object(S3Service, "get_client", return_value=client):
            response = self._post("get", {"key": "a.bin"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(base64.b64decode(response.json()["data"]), b"\x00\x01binary")

    def test_get_missing_is_404(self):
        client = mock.Mock()
        client.get_object.side_effect = Exception("NoSuchKey")
        with mock.patch.object(S3Service, "get_client", return_value=client):
            response = self._post("get", {"key": "missing"})
        self.assertEqual(response.status_code, 404)

    def test_list_serializes_last_modified(self):
        client = mock.Mock()
        paginator = mock.Mock()
        paginator.paginate.return_value = [
            {
                "Contents": [
                    {
                        "Key": f"{_prefix()}a.jpg",
                        "Size": 3,
                        "LastModified": datetime(2026, 7, 17, tzinfo=dt_timezone.utc),
                        "ETag": '"e"',
                    }
                ]
            }
        ]
        client.get_paginator.return_value = paginator
        with mock.patch.object(S3Service, "get_client", return_value=client):
            response = self._post("list", {})

        objects = response.json()["objects"]
        self.assertEqual(objects[0]["key"], "a.jpg")
        self.assertEqual(objects[0]["last_modified"], "2026-07-17T00:00:00+00:00")
        self.assertEqual(objects[0]["etag"], "e")

    def test_url_returns_the_public_url(self):
        self.connection.public_base_url = "https://cdn.example.com"
        self.connection.save()

        response = self._post("url", {"key": "a.jpg"})

        self.assertEqual(
            response.json()["url"], f"https://cdn.example.com/{_prefix()}a.jpg"
        )

    def test_delete_issues_the_prefixed_delete(self):
        client = mock.Mock()
        with mock.patch.object(S3Service, "get_client", return_value=client):
            response = self._post("delete", {"key": "a.txt"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            client.delete_object.call_args.kwargs["Key"], f"{_prefix()}a.txt"
        )


# --------------------------------------------------------------------------- #
# Script helper
# --------------------------------------------------------------------------- #

class PyRunnerStorageHelperTests(TestCase):
    """The helper is stdlib-only by contract: no boto3, no credentials in a run."""

    def test_helper_imports_only_stdlib(self):
        """Asserts the IMPORTS, not the prose: the module docstring legitimately
        mentions boto3 to explain that scripts don't need it."""
        import ast
        import inspect
        import sys

        tree = ast.parse(inspect.getsource(pyrunner_storage))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])

        self.assertNotIn("boto3", imported)
        self.assertNotIn("botocore", imported)
        self.assertLessEqual(imported, set(sys.stdlib_module_names))

    def test_helper_never_reads_credentials_from_the_environment(self):
        import inspect

        source = inspect.getsource(pyrunner_storage)
        for forbidden in ("AWS_ACCESS", "AWS_SECRET", "S3_BUCKET", "S3_ENDPOINT"):
            self.assertNotIn(forbidden, source)

    def test_errors_when_not_in_a_run_context(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(pyrunner_storage.StorageError):
                pyrunner_storage.put("a.txt", b"x")

    def test_put_rejects_oversized_locally(self):
        # Fails fast rather than uploading 25 MB just to be refused.
        with mock.patch.dict(
            "os.environ",
            {"PYRUNNER_INTERNAL_URL": "http://127.0.0.1:8000", "PYRUNNER_INTERNAL_TOKEN": "t"},
        ):
            with self.assertRaises(pyrunner_storage.StorageError):
                pyrunner_storage.put(
                    "big.bin", b"x" * (pyrunner_storage.MAX_PUT_BYTES + 1)
                )

    def test_put_base64_encodes_and_posts(self):
        with mock.patch.object(
            pyrunner_storage, "_post", return_value={"key": "a.txt"}
        ) as post:
            pyrunner_storage.put("a.txt", b"data", content_type="text/plain")

        endpoint, payload = post.call_args.args
        self.assertEqual(endpoint, "put")
        self.assertEqual(base64.b64decode(payload["data"]), b"data")
        self.assertEqual(payload["content_type"], "text/plain")

    def test_get_decodes_base64(self):
        with mock.patch.object(
            pyrunner_storage,
            "_post",
            return_value={"data": base64.b64encode(b"data").decode()},
        ):
            self.assertEqual(pyrunner_storage.get("a.txt"), b"data")

    def test_get_returns_none_on_missing(self):
        with mock.patch.object(
            pyrunner_storage, "_post", side_effect=FileNotFoundError("nope")
        ):
            self.assertIsNone(pyrunner_storage.get("a.txt"))

    def test_put_encodes_str(self):
        with mock.patch.object(pyrunner_storage, "_post", return_value={}) as post:
            pyrunner_storage.put("a.txt", "héllo")
        self.assertEqual(
            base64.b64decode(post.call_args.args[1]["data"]), "héllo".encode()
        )


class RunEnvironmentTests(TestCase):
    """S3 credentials must never reach a script's environment."""

    def test_storage_credentials_are_absent_from_the_run_env(self):
        from core.executor import _build_script_environment

        env_model = Environment.objects.create(name="e", path="/tmp/env")
        script = Script.objects.create(
            name="s", code="print(1)", environment=env_model, owner_plugin=OWNER
        )
        run = Run.objects.create(script=script)
        _use_for_assets(_make_connection())

        env = _build_script_environment(run=run)

        joined = " ".join(f"{k}={v}" for k, v in env.items())
        for forbidden in ("enc-access", "enc-secret", "assets-bucket", "r2.cloudflarestorage.com"):
            self.assertNotIn(forbidden, joined)
        for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "PYRUNNER_S3_BUCKET"):
            self.assertNotIn(key, env)
        # The loopback seam IS present — that's how the helper reaches storage.
        self.assertIn("PYRUNNER_INTERNAL_URL", env)
        self.assertIn("PYRUNNER_INTERNAL_TOKEN", env)


# --------------------------------------------------------------------------- #
# Backup regression
# --------------------------------------------------------------------------- #

class BackupRegressionTests(TestCase):
    """Backups moved from GlobalSettings.s3_* to the is_default connection. The
    behavior must be identical — that is the whole promise of the migration."""

    def test_for_backup_resolves_the_default_connection(self):
        _make_connection(name="Assets", bucket="assets-bucket")
        backup = _make_connection(name="Backups", bucket="backup-bucket", is_default=True)

        self.assertEqual(S3Service.for_backup(), backup)

    def test_for_backup_is_none_when_unconfigured(self):
        self.assertIsNone(S3Service.for_backup())
        self.assertFalse(S3Service.is_configured())

    def test_backup_upload_still_defaults_to_gzip(self):
        # upload_file's content_type default exists so backup callers, which never
        # pass one, keep sending exactly what they always sent.
        connection = _make_connection(is_default=True)
        client = mock.Mock()
        with mock.patch.object(S3Service, "get_client", return_value=client):
            S3Service.upload_file(connection, b"gz", "pyrunner-backups/b.json.gz")

        self.assertEqual(
            client.put_object.call_args.kwargs["ContentType"], "application/gzip"
        )

    def test_scheduled_backup_task_skips_without_a_connection(self):
        from core.tasks import scheduled_backup_task

        result = scheduled_backup_task()
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "S3 not enabled")

    def test_scheduled_backup_task_skips_a_disabled_connection(self):
        # The old `s3_enabled` "configured but paused" state now lives on the row.
        from core.tasks import scheduled_backup_task

        _make_connection(is_default=True, enabled=False)

        result = scheduled_backup_task()
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "S3 not enabled")

    def test_scheduled_backup_task_uploads_through_the_default_connection(self):
        from core.tasks import scheduled_backup_task

        _make_connection(name="Backups", bucket="backup-bucket", is_default=True)

        client = mock.Mock()
        # The task also applies retention, which lists the backup prefix. Give the
        # paginator a real (empty) page so that path runs for real instead of
        # dying into list_files' except-and-log.
        paginator = mock.Mock()
        paginator.paginate.return_value = [{"Contents": []}]
        client.get_paginator.return_value = paginator

        with mock.patch.object(S3Service, "get_client", return_value=client):
            result = scheduled_backup_task()

        self.assertTrue(result["success"], result)
        self.assertEqual(client.put_object.call_args.kwargs["Bucket"], "backup-bucket")
        self.assertEqual(
            client.put_object.call_args.kwargs["ContentType"], "application/gzip"
        )
        self.assertTrue(
            client.put_object.call_args.kwargs["Key"].startswith("pyrunner-backups/")
        )

    def test_backup_key_still_uses_the_settings_prefix(self):
        settings = GlobalSettings.get_settings()
        settings.s3_backup_prefix = "custom-prefix/"
        settings.save()

        self.assertTrue(
            S3Service.generate_backup_key().startswith("custom-prefix/backup_")
        )

    def test_list_backups_requires_an_enabled_default(self):
        from core.services.backup_schedule_service import BackupScheduleService

        _make_connection(is_default=True, enabled=False)
        self.assertEqual(BackupScheduleService.list_backups(), [])


class PluginDeleteStorageCleanupTests(TestCase):
    """remove_data must also clear the plugin's objects — and must not fail the
    delete when the bucket is unreachable."""

    def test_cleanup_deletes_the_owner_prefix(self):
        from core.services.plugin_service import PluginService

        _use_for_assets(_make_connection())
        with mock.patch.object(StorageAPI, "delete_all", return_value=3) as delete_all:
            counts = PluginService._cleanup_owned_resources(OWNER)

        delete_all.assert_called_once()
        self.assertEqual(counts.get("storage objects"), 3)

    def test_cleanup_survives_a_storage_failure(self):
        from core.services.plugin_service import PluginService

        _use_for_assets(_make_connection())
        with mock.patch.object(
            StorageAPI, "delete_all", side_effect=Exception("bucket unreachable")
        ):
            counts = PluginService._cleanup_owned_resources(OWNER)

        self.assertNotIn("storage objects", counts)
