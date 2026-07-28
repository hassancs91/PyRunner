"""
PyRunner notifications API for scripts.

Send a message to a configured Channel, reply to an inbound chat message, or send
an email through PyRunner's core email config — all server-side, so your script
never handles credentials. Mirrors ``pyrunner_datastore``: stdlib-only, talking
to PyRunner's internal loopback API authenticated by a signed per-run token.

Usage:
    import pyrunner_notify

    pyrunner_notify.send("Ops Alerts", "Backup finished ✅")
    pyrunner_notify.email("Report ready", "See the dashboard.", to="me@example.com")
    pyrunner_notify.reply("On it!")   # only inside an inbound-triggered run (Phase 2)
"""

import json
import os
import urllib.error
import urllib.request


class NotifyError(Exception):
    """Raised when a notification cannot be delivered."""


def _post(payload: dict) -> dict:
    base = os.environ.get("PYRUNNER_INTERNAL_URL")
    token = os.environ.get("PYRUNNER_INTERNAL_TOKEN")
    if not base or not token:
        raise NotifyError(
            "PyRunner notifications are not available in this run context."
        )
    url = f"{base.rstrip('/')}/internal/channels/send"
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
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            body = json.loads(raw) if raw else {}
            message = body.get("error", {}).get("message") or raw.decode("utf-8", "ignore")
        except ValueError:
            message = raw.decode("utf-8", "ignore")
        raise NotifyError(f"Send failed (HTTP {e.code}): {message}")
    except urllib.error.URLError as e:
        raise NotifyError(f"Could not reach PyRunner: {e}")


def send(channel: str, text: str, reply_ref: dict | None = None) -> dict:
    """Send ``text`` to the named Channel (resolved in this run's workspace)."""
    payload = {"target": channel, "text": text}
    if reply_ref:
        payload["reply_ref"] = reply_ref
    return _post(payload)


def email(
    subject: str, body: str, to: str | None = None, html: str | None = None
) -> dict:
    """Send an email via PyRunner's configured core email backend.

    ``to`` defaults to the instance's default notification email when omitted.
    ``html`` optionally adds an HTML alternative; ``body`` stays the plain-text
    fallback for clients that don't render HTML.
    """
    payload = {"target": "email", "subject": subject, "text": body, "to": to}
    if html:
        payload["html"] = html
    return _post(payload)


def reply(text: str) -> dict:
    """Reply to the inbound chat message that triggered this run.

    Available only when the run was triggered by an inbound channel message
    (Phase 2); PyRunner injects ``INBOUND_CHANNEL`` + ``INBOUND_REPLY_REF``.
    """
    channel = os.environ.get("INBOUND_CHANNEL")
    if not channel:
        raise NotifyError("reply() is only available inside an inbound-triggered run.")
    reply_ref = None
    raw = os.environ.get("INBOUND_REPLY_REF")
    if raw:
        try:
            reply_ref = json.loads(raw)
        except ValueError:
            reply_ref = None
    return send(channel, text, reply_ref=reply_ref)
