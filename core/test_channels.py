"""
Channels subsystem (Phase 1) — outbound chat integrations.

Covers the Channel model (encrypted creds, fingerprint, one-bot-one-channel +
per-workspace name uniqueness), the provider registry + Telegram provider
(reply-ref/default-target routing, getUpdates chat-ID discovery), ChannelService
status bookkeeping, run-notification routing through notify_channels, the
loopback /internal/channels/send endpoint (auth, resolution, email, rate limit),
the ChannelForm, and the CRUD views.

Network is always mocked — no test hits Telegram.
"""

import json
from unittest import mock

from cryptography.fernet import Fernet
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.forms import ChannelForm
from core.models import (
    Channel,
    ChannelMember,
    ChannelMessage,
    Environment,
    GlobalSettings,
    Run,
    Script,
    User,
    Workspace,
    WorkspaceMembership,
)
from core.services import ChannelService, EncryptionService, NotificationService
from core.services.channels import ChannelError, get_provider, list_providers
from core.services.channels.inbound import dispatch_inbound
from core.services.channels.telegram import TelegramProvider
from core.services.datastore_token import mint_datastore_token

_TG_UPDATE = {
    "update_id": 10,
    "message": {
        "message_id": 1,
        "from": {"id": 7, "username": "ann", "first_name": "Ann"},
        "chat": {"id": 7, "type": "private"},
        "text": "hello bot",
    },
}

_TEST_KEY = Fernet.generate_key().decode()


def _setup_wizard_off(test):
    for target in (
        "core.services.setup_service.SetupService.is_setup_needed",
        "core.services.setup_service.SetupService.needs_admin_setup",
    ):
        p = mock.patch(target, return_value=False)
        p.start()
        test.addCleanup(p.stop)


@override_settings(ENCRYPTION_KEY=_TEST_KEY)
class _EncBase(TestCase):
    """Base with encryption configured (channel creds need a Fernet key)."""

    def setUp(self):
        EncryptionService.reset()
        self.addCleanup(EncryptionService.reset)
        self.ws = Workspace.get_default()

    def _channel(self, name="Ops", token="111:AAA", target="999", enabled=True, workspace=None):
        ch = Channel(
            workspace=workspace or self.ws,
            provider="telegram",
            name=name,
            enabled=enabled,
        )
        ch.set_credentials({"bot_token": token}, identity=token)
        ch.config = {"default_target": target}
        ch.save()
        return ch


class ChannelModelTests(_EncBase):
    def test_credentials_roundtrip_and_fingerprint(self):
        ch = self._channel(token="123:SECRET")
        self.assertEqual(ch.get_credentials(), {"bot_token": "123:SECRET"})
        self.assertTrue(ch.is_configured)
        self.assertEqual(len(ch.creds_fingerprint), 64)
        self.assertEqual(ch.default_target, "999")
        # ciphertext is not the plaintext
        self.assertNotIn("SECRET", ch.creds_encrypted)

    def test_fingerprint_for_is_deterministic_and_provider_scoped(self):
        a = Channel.fingerprint_for("telegram", "T")
        self.assertEqual(a, Channel.fingerprint_for("telegram", "T"))
        self.assertNotEqual(a, Channel.fingerprint_for("slack", "T"))
        self.assertEqual(Channel.fingerprint_for("telegram", ""), "")

    def test_one_bot_one_channel_enforced(self):
        self._channel(name="First", token="dup:TOKEN")
        with transaction.atomic():
            with self.assertRaises(IntegrityError):
                self._channel(name="Second", token="dup:TOKEN")

    def test_name_unique_per_workspace(self):
        self._channel(name="Dupe", token="a:1")
        with transaction.atomic():
            with self.assertRaises(IntegrityError):
                self._channel(name="Dupe", token="b:2")

    def test_same_name_other_workspace_ok(self):
        other = Workspace.objects.create(name="Other")
        self._channel(name="Shared", token="a:1")
        # Different workspace + different token → allowed.
        self._channel(name="Shared", token="b:2", workspace=other)
        self.assertEqual(Channel.objects.filter(name="Shared").count(), 2)


class ProviderTests(_EncBase):
    def test_registry(self):
        self.assertIn("telegram", list_providers())
        self.assertIsInstance(get_provider("telegram"), TelegramProvider)
        with self.assertRaises(ChannelError):
            get_provider("nope")

    def test_send_prefers_reply_ref_then_default_target(self):
        ch = self._channel(target="555")
        provider = get_provider("telegram")
        from core.services.channels import OutboundMessage

        with mock.patch.object(TelegramProvider, "_call", return_value={"message_id": 7}) as m:
            provider.send(ch, OutboundMessage(text="hi", reply_ref={"chat_id": "888"}))
            self.assertEqual(m.call_args.args[2]["chat_id"], "888")

            provider.send(ch, OutboundMessage(text="hi"))
            self.assertEqual(m.call_args.args[2]["chat_id"], "555")

    def test_send_without_any_target_raises(self):
        ch = self._channel(target="")
        provider = get_provider("telegram")
        from core.services.channels import OutboundMessage

        with mock.patch.object(TelegramProvider, "_call", return_value={}):
            with self.assertRaises(ChannelError):
                provider.send(ch, OutboundMessage(text="hi"))

    def test_discover_chat_ids_parses_updates(self):
        ch = self._channel()
        updates = [
            {"message": {"chat": {"id": 42, "first_name": "Ann"}, "text": "hello"}},
            {"message": {"chat": {"id": 42, "first_name": "Ann"}, "text": "again"}},
            {"message": {"chat": {"id": 99, "title": "Ops Room", "type": "group"}}},
        ]
        with mock.patch.object(TelegramProvider, "_call", return_value=updates):
            chats = get_provider("telegram").discover_chat_ids(ch)
        by_id = {c["chat_id"]: c for c in chats}
        self.assertEqual(set(by_id), {42, 99})
        self.assertEqual(by_id[42]["name"], "Ann")
        self.assertEqual(by_id[99]["name"], "Ops Room")


class ChannelServiceTests(_EncBase):
    def test_test_updates_status(self):
        ch = self._channel()
        with mock.patch.object(TelegramProvider, "test_connection", return_value=(True, "ok")):
            ok, msg = ChannelService.test(ch)
        ch.refresh_from_db()
        self.assertTrue(ok)
        self.assertIsNotNone(ch.last_tested_at)
        self.assertEqual(ch.last_error, "")

    def test_send_failure_records_error(self):
        ch = self._channel()
        with mock.patch.object(TelegramProvider, "send", side_effect=ChannelError("boom")):
            with self.assertRaises(ChannelError):
                ChannelService.send(ch, "hi")
        ch.refresh_from_db()
        self.assertEqual(ch.last_error, "boom")


class NotificationRoutingTests(_EncBase):
    def setUp(self):
        super().setUp()
        self.env = Environment.objects.create(name="e", path="p")
        self.script = Script.objects.create(
            name="s", code="x", environment=self.env, workspace=self.ws,
            notify_on=Script.NotifyOn.BOTH,
        )
        self.run = Run.objects.create(
            script=self.script, workspace=self.ws, status=Run.Status.SUCCESS
        )

    def test_enabled_channel_notified_disabled_skipped(self):
        on = self._channel(name="On", token="a:1")
        off = self._channel(name="Off", token="b:2", enabled=False)
        self.script.notify_channels.add(on, off)
        with mock.patch.object(ChannelService, "send", return_value={"ok": True}) as m:
            out = NotificationService._send_channel_notifications(self.run)
        self.assertEqual(out["sent"], 1)
        self.assertEqual(m.call_count, 1)
        self.assertEqual(m.call_args.args[0].id, on.id)

    def test_send_notification_respects_notify_on(self):
        self.script.notify_on = Script.NotifyOn.NEVER
        self.script.save()
        self.script.notify_channels.add(self._channel(name="On", token="a:1"))
        with mock.patch.object(ChannelService, "send") as m:
            results = NotificationService.send_notification(self.run)
        self.assertEqual(results["channels_sent"], 0)
        m.assert_not_called()


class InternalSendEndpointTests(_EncBase):
    def setUp(self):
        super().setUp()
        self.env = Environment.objects.create(name="e", path="p")
        self.script = Script.objects.create(
            name="s", code="x", environment=self.env, workspace=self.ws
        )
        self.run = Run.objects.create(script=self.script, workspace=self.ws)
        self.url = reverse("internal:channels_send")
        self.token = mint_datastore_token(self.run.id)
        cache.clear()

    def _post(self, body, token=None, remote="127.0.0.1"):
        headers = {}
        if token is not False:
            headers["HTTP_AUTHORIZATION"] = f"Bearer {token or self.token}"
        return self.client.post(
            self.url,
            data=json.dumps(body),
            content_type="application/json",
            REMOTE_ADDR=remote,
            **headers,
        )

    def test_requires_token(self):
        self.assertEqual(self._post({"target": "x", "text": "y"}, token=False).status_code, 401)

    def test_non_loopback_forbidden(self):
        resp = self._post({"target": "x", "text": "y"}, remote="8.8.8.8")
        self.assertEqual(resp.status_code, 403)

    def test_missing_text_is_400(self):
        self.assertEqual(self._post({"target": "x"}).status_code, 400)

    def test_unknown_channel_404(self):
        resp = self._post({"target": "ghost", "text": "hi"})
        self.assertEqual(resp.status_code, 404)

    def test_send_to_channel(self):
        self._channel(name="Ops", token="a:1")
        with mock.patch.object(ChannelService, "send", return_value={"ok": True}) as m:
            resp = self._post({"target": "Ops", "text": "hello"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(m.call_count, 1)
        self.assertEqual(m.call_args.args[1], "hello")

    def test_email_target_delegates_to_core(self):
        with mock.patch.object(NotificationService, "send_email", return_value=True) as m:
            resp = self._post({"target": "email", "text": "body", "subject": "Hi"})
        self.assertEqual(resp.status_code, 200)
        m.assert_called_once()

    def test_email_html_passes_through(self):
        # pyrunner_notify.email(html=...) → an HTML alternative on the core email;
        # the plain text stays the fallback body.
        with mock.patch.object(NotificationService, "send_email", return_value=True) as m:
            resp = self._post(
                {"target": "email", "text": "plain", "subject": "Hi", "html": "<b>rich</b>"}
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(m.call_args.kwargs.get("html"), "<b>rich</b>")

    def test_email_without_html_passes_none(self):
        with mock.patch.object(NotificationService, "send_email", return_value=True) as m:
            self._post({"target": "email", "text": "plain"})
        self.assertIsNone(m.call_args.kwargs.get("html"))

    def test_email_html_must_be_a_string(self):
        resp = self._post({"target": "email", "text": "plain", "html": {"not": "str"}})
        self.assertEqual(resp.status_code, 400)

    def test_channel_send_logs_outbound_message(self):
        # Review 4b.1: a script handler's async reply (via this endpoint) must be
        # recorded as an OUT ChannelMessage — otherwise it's absent from history
        # and never counts toward the daily cap.
        ch = self._channel(name="Ops", token="a:1")
        with mock.patch.object(ChannelService, "send", return_value={"ok": True}):
            self._post({"target": "Ops", "text": "hello", "reply_ref": {"chat_id": "5"}})
        out = ChannelMessage.objects.filter(channel=ch, direction=ChannelMessage.Direction.OUT)
        self.assertEqual(out.count(), 1)
        row = out.first()
        self.assertEqual(row.text, "hello")
        self.assertEqual(row.status, "ok")
        self.assertEqual(row.reply_ref_json, {"chat_id": "5"})

    def test_outbound_send_counts_toward_daily_cap(self):
        ch = self._channel(name="Ops", token="a:1")
        ch.daily_reply_cap = 1
        ch.save()
        self.assertFalse(ch.daily_cap_reached())
        with mock.patch.object(ChannelService, "send", return_value={"ok": True}):
            self._post({"target": "Ops", "text": "one"})
        self.assertEqual(ch.replies_today(), 1)
        self.assertTrue(ch.daily_cap_reached())

    def test_failed_send_logs_error_row(self):
        ch = self._channel(name="Ops", token="a:1")
        with mock.patch.object(ChannelService, "send", side_effect=ChannelError("boom")):
            resp = self._post({"target": "Ops", "text": "hi"})
        self.assertEqual(resp.status_code, 502)
        err = ChannelMessage.objects.filter(
            channel=ch, direction=ChannelMessage.Direction.OUT, status="error"
        )
        self.assertEqual(err.count(), 1)
        self.assertIn("boom", err.first().error)

    def test_email_send_logs_no_channel_message(self):
        with mock.patch.object(NotificationService, "send_email", return_value=True):
            self._post({"target": "email", "text": "body"})
        self.assertEqual(ChannelMessage.objects.count(), 0)

    def test_rate_limited(self):
        # Exercise the real limiter (fixed-window helper) instead of seeding
        # cache internals: with the per-run cap patched to 1, the second send
        # in the window must 429.
        with mock.patch("core.views.api.channels_internal._RUN_SEND_LIMIT", 1):
            with mock.patch.object(NotificationService, "send_email", return_value=True):
                first = self._post({"target": "email", "text": "x"})
            second = self._post({"target": "email", "text": "x"})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)


class ChannelFormTests(_EncBase):
    def _data(self, **over):
        data = {
            "name": "Ops",
            "provider": "telegram",
            "bot_token": "123:TOKEN",
            "default_target": "999",
            "enabled": "on",
        }
        data.update(over)
        return data

    def test_valid_create_persists(self):
        form = ChannelForm(self._data(), workspace=self.ws)
        self.assertTrue(form.is_valid(), form.errors)
        ch = form.save()
        self.assertEqual(ch.get_credentials()["bot_token"], "123:TOKEN")
        self.assertEqual(ch.default_target, "999")

    def test_missing_token_on_create_invalid(self):
        form = ChannelForm(self._data(bot_token=""), workspace=self.ws)
        self.assertFalse(form.is_valid())
        self.assertIn("bot_token", form.errors)

    def test_duplicate_name_invalid(self):
        self._channel(name="Ops", token="x:1")
        form = ChannelForm(self._data(name="Ops", bot_token="y:2"), workspace=self.ws)
        self.assertFalse(form.is_valid())
        self.assertIn("name", form.errors)

    def test_duplicate_token_invalid(self):
        self._channel(name="First", token="123:TOKEN")
        form = ChannelForm(self._data(name="Second"), workspace=self.ws)
        self.assertFalse(form.is_valid())
        self.assertIn("bot_token", form.errors)


@override_settings(ENCRYPTION_KEY=_TEST_KEY)
class ChannelViewTests(TestCase):
    def setUp(self):
        EncryptionService.reset()
        self.addCleanup(EncryptionService.reset)
        _setup_wizard_off(self)
        self.ws = Workspace.get_default()
        self.user = User.objects.create(email="u@example.com")
        WorkspaceMembership.ensure(self.user, self.ws, role=WorkspaceMembership.ROLE_OWNER)
        self.client.force_login(self.user)

    def test_list_renders(self):
        resp = self.client.get(reverse("cpanel:channel_list"))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Channels", resp.content.decode())

    def test_create_persists_and_redirects(self):
        resp = self.client.post(
            reverse("cpanel:channel_create"),
            {
                "name": "Ops",
                "provider": "telegram",
                "bot_token": "123:TOKEN",
                "default_target": "555",
                "enabled": "on",
            },
        )
        self.assertEqual(resp.status_code, 302)
        ch = Channel.objects.get(name="Ops")
        self.assertEqual(ch.workspace_id, self.ws.id)
        self.assertEqual(ch.default_target, "555")

    def test_edit_renders(self):
        ch = Channel(workspace=self.ws, provider="telegram", name="Ops")
        ch.set_credentials({"bot_token": "1:2"}, identity="1:2")
        ch.save()
        resp = self.client.get(reverse("cpanel:channel_edit", args=[ch.pk]))
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn("Find chat ID", body)
        self.assertIn("Test connection", body)
        self.assertIn("Approval inbox", body)  # Phase 2 inbound section


# --------------------------------------------------------------------------- #
# Phase 2 — inbound                                                            #
# --------------------------------------------------------------------------- #


class TelegramInboundTests(_EncBase):
    def _inbound_channel(self):
        ch = Channel(workspace=self.ws, provider="telegram", name="Bot", inbound_enabled=True)
        ch.set_credentials({"bot_token": "1:2"}, identity="1:2")
        ch.ensure_inbound_token()
        ch.ensure_inbound_secret()
        ch.save()
        return ch

    def test_verify_inbound_matches_secret(self):
        ch = self._inbound_channel()
        secret = ch.get_inbound_secret()
        provider = get_provider("telegram")
        good = mock.Mock(headers={"X-Telegram-Bot-Api-Secret-Token": secret})
        bad = mock.Mock(headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"})
        self.assertTrue(provider.verify_inbound(ch, good))
        self.assertFalse(provider.verify_inbound(ch, bad))

    def test_dedup_and_parse(self):
        ch = self._inbound_channel()
        provider = get_provider("telegram")
        self.assertEqual(provider.dedup_id(_TG_UPDATE), "10")
        msg = provider.parse_inbound(ch, _TG_UPDATE)
        self.assertEqual(msg.text, "hello bot")
        self.assertEqual(msg.sender["id"], "7")
        self.assertEqual(msg.reply_ref, {"chat_id": 7})

    def test_set_inbound_webhook_calls_api(self):
        ch = self._inbound_channel()
        with mock.patch.object(TelegramProvider, "_call", return_value=True) as m:
            get_provider("telegram").set_inbound_webhook(ch, "https://host/channels/tok/")
        self.assertEqual(m.call_args.args[1], "setWebhook")
        self.assertEqual(m.call_args.args[2]["url"], "https://host/channels/tok/")
        self.assertEqual(m.call_args.args[2]["secret_token"], ch.get_inbound_secret())


class InboundDispatchTests(_EncBase):
    def setUp(self):
        super().setUp()
        self.env = Environment.objects.create(name="e", path="p")
        self.script = Script.objects.create(
            name="s", code="x", environment=self.env, workspace=self.ws, is_enabled=True
        )
        self.ch = Channel(
            workspace=self.ws, provider="telegram", name="Bot",
            inbound_enabled=True, inbound_handler="script",
            inbound_target_id=self.script.id,
        )
        self.ch.set_credentials({"bot_token": "1:2"}, identity="1:2")
        self.ch.save()

    def test_script_handler_queues_run_and_logs_inbound(self):
        with mock.patch("core.tasks.queue_script_run") as q:
            dispatch_inbound(str(self.ch.id), _TG_UPDATE)
        self.assertEqual(q.call_count, 1)
        self.assertEqual(Run.objects.filter(script=self.script).count(), 1)
        self.assertEqual(
            ChannelMessage.objects.filter(channel=self.ch, direction="in").count(), 1
        )

    def test_daily_cap_blocks_and_replies(self):
        self.ch.daily_reply_cap = 1
        self.ch.save()
        ChannelMessage.objects.create(channel=self.ch, direction="out", text="prev")
        with mock.patch.object(TelegramProvider, "send", return_value={}) as send, \
                mock.patch("core.tasks.queue_script_run") as q:
            dispatch_inbound(str(self.ch.id), _TG_UPDATE)
        q.assert_not_called()
        self.assertIn("Daily limit", send.call_args.args[1].text)

    def test_handler_exception_sends_friendly_error(self):
        with mock.patch("core.services.channels.get_handler") as gh, \
                mock.patch.object(TelegramProvider, "send", return_value={}) as send:
            gh.return_value = mock.Mock(side_effect=RuntimeError("boom"))
            dispatch_inbound(str(self.ch.id), _TG_UPDATE)
        self.assertIn("something went wrong", send.call_args.args[1].text)
        self.assertTrue(
            ChannelMessage.objects.filter(channel=self.ch, direction="out", status="error").exists()
        )

    def test_notify_only_handler_does_nothing(self):
        self.ch.inbound_handler = ""
        self.ch.save()
        with mock.patch("core.tasks.queue_script_run") as q:
            dispatch_inbound(str(self.ch.id), _TG_UPDATE)
        q.assert_not_called()

    def test_executor_injects_inbound_env(self):
        from core.executor import _build_script_environment

        run = Run.objects.create(script=self.script, workspace=self.ws)
        env = _build_script_environment(
            webhook_data={
                "inbound": {
                    "channel": "Bot", "text": "hi",
                    "reply_ref": {"chat_id": 7}, "sender": {"id": "7"},
                }
            },
            run=run,
        )
        self.assertEqual(env["INBOUND_CHANNEL"], "Bot")
        self.assertEqual(env["INBOUND_TEXT"], "hi")
        self.assertEqual(json.loads(env["INBOUND_REPLY_REF"]), {"chat_id": 7})


class ChannelWebhookTests(_EncBase):
    def setUp(self):
        super().setUp()
        _setup_wizard_off(self)  # the public webhook must not 302 to /setup/
        p = mock.patch.object(TelegramProvider, "send", return_value={})
        self.send = p.start()
        self.addCleanup(p.stop)
        from django.core.cache import cache

        cache.clear()
        self.ch = Channel(
            workspace=self.ws, provider="telegram", name="Bot",
            enabled=True, inbound_enabled=True, inbound_handler="script",
            inbound_access=Channel.InboundAccess.APPROVAL,
        )
        self.ch.set_credentials({"bot_token": "1:2"}, identity="1:2")
        self.ch.ensure_inbound_token()
        self.ch.ensure_inbound_secret()
        self.ch.save()
        self.secret = self.ch.get_inbound_secret()

    def _hook(self, payload=None, secret=None, remote="1.2.3.4"):
        headers = {}
        if secret is not False:
            headers["HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN"] = secret or self.secret
        return self.client.post(
            reverse("channel_webhook", args=[self.ch.inbound_token]),
            data=json.dumps(payload if payload is not None else _TG_UPDATE),
            content_type="application/json",
            REMOTE_ADDR=remote,
            **headers,
        )

    def _alive(self):
        s = GlobalSettings.get_settings()
        s.worker_heartbeat_at = timezone.now()
        s.save(update_fields=["worker_heartbeat_at"])

    def test_unknown_token_404(self):
        resp = self.client.post(reverse("channel_webhook", args=["nope"]),
                                data="{}", content_type="application/json")
        self.assertEqual(resp.status_code, 404)

    def test_bad_signature_401(self):
        self.assertEqual(self._hook(secret="wrong").status_code, 401)

    def test_unknown_sender_creates_pending_and_replies(self):
        resp = self._hook()
        self.assertEqual(resp.json()["status"], "pending")
        self.assertTrue(
            ChannelMember.objects.filter(channel=self.ch, sender_id="7", status="pending").exists()
        )
        self.send.assert_called_once()

    def test_approved_sender_enqueues_when_worker_alive(self):
        ChannelMember.objects.create(channel=self.ch, sender_id="7", status="approved")
        self._alive()
        with mock.patch("core.views.channel_webhooks.async_task") as at:
            resp = self._hook()
        self.assertEqual(resp.json()["status"], "queued")
        at.assert_called_once()

    def test_blocked_sender_silently_dropped(self):
        ChannelMember.objects.create(channel=self.ch, sender_id="7", status="blocked")
        with mock.patch("core.views.channel_webhooks.async_task") as at:
            resp = self._hook()
        self.assertEqual(resp.json()["status"], "blocked")
        at.assert_not_called()

    def test_worker_down_replies_asleep(self):
        ChannelMember.objects.create(channel=self.ch, sender_id="7", status="approved")
        # no heartbeat set → worker considered down
        with mock.patch("core.views.channel_webhooks.async_task") as at:
            resp = self._hook()
        self.assertEqual(resp.json()["status"], "worker_unavailable")
        at.assert_not_called()
        self.assertIn("asleep", self.send.call_args.args[1].text)

    def test_dedup_drops_retry(self):
        ChannelMember.objects.create(channel=self.ch, sender_id="7", status="approved")
        self._alive()
        with mock.patch("core.views.channel_webhooks.async_task"):
            self.assertEqual(self._hook().json()["status"], "queued")
            self.assertEqual(self._hook().json()["status"], "duplicate")

    def test_open_access_enqueues_without_member(self):
        self.ch.inbound_access = Channel.InboundAccess.OPEN
        self.ch.save()
        self._alive()
        with mock.patch("core.views.channel_webhooks.async_task") as at:
            resp = self._hook()
        self.assertEqual(resp.json()["status"], "queued")
        at.assert_called_once()


class InboundConfigViewTests(TestCase):
    def setUp(self):
        EncryptionService.reset()
        self.addCleanup(EncryptionService.reset)
        _setup_wizard_off(self)
        self.override = override_settings(ENCRYPTION_KEY=_TEST_KEY)
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.ws = Workspace.get_default()
        self.env = Environment.objects.create(name="e", path="p")
        self.script = Script.objects.create(
            name="s", code="x", environment=self.env, workspace=self.ws, is_enabled=True
        )
        self.user = User.objects.create(email="u@example.com")
        WorkspaceMembership.ensure(self.user, self.ws, role=WorkspaceMembership.ROLE_OWNER)
        self.client.force_login(self.user)
        self.ch = Channel(workspace=self.ws, provider="telegram", name="Bot")
        self.ch.set_credentials({"bot_token": "1:2"}, identity="1:2")
        self.ch.save()

    def test_enable_inbound_registers_webhook(self):
        with mock.patch.object(TelegramProvider, "set_inbound_webhook") as sw:
            resp = self.client.post(
                reverse("cpanel:channel_inbound", args=[self.ch.pk]),
                {
                    "inbound_enabled": "on",
                    "inbound_handler": "script",
                    "inbound_target_id": str(self.script.id),
                    "inbound_access": "approval",
                    "daily_reply_cap": "0",
                },
            )
        self.assertEqual(resp.status_code, 302)
        self.ch.refresh_from_db()
        self.assertTrue(self.ch.inbound_enabled)
        self.assertTrue(self.ch.inbound_token)
        self.assertEqual(str(self.ch.inbound_target_id), str(self.script.id))
        sw.assert_called_once()

    def test_enable_script_without_target_errors(self):
        resp = self.client.post(
            reverse("cpanel:channel_inbound", args=[self.ch.pk]),
            {"inbound_enabled": "on", "inbound_handler": "script", "inbound_access": "approval"},
        )
        self.assertEqual(resp.status_code, 302)
        self.ch.refresh_from_db()
        self.assertFalse(self.ch.inbound_enabled)

    def test_member_approve_and_block(self):
        member = ChannelMember.objects.create(channel=self.ch, sender_id="7", status="pending")
        self.client.post(
            reverse("cpanel:channel_member_action", args=[self.ch.pk, member.pk, "approve"])
        )
        member.refresh_from_db()
        self.assertEqual(member.status, "approved")
        self.client.post(
            reverse("cpanel:channel_member_action", args=[self.ch.pk, member.pk, "block"])
        )
        member.refresh_from_db()
        self.assertEqual(member.status, "blocked")
