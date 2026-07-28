"""Google OAuth for the YouTube Comments AI plugin — web-process side.

The hosted flow: Settings hold a Google OAuth client (type **Web application**)
whose authorized redirect URI is this plugin's ``oauth/callback`` route.
"Connect YouTube" sends the browser to Google's consent screen; the callback
exchanges the code for a refresh token, which is stored as an owner-scoped
Secret (``YT_OAUTH_REFRESH_TOKEN``) and granted to the analyzer script. The
scope unlocks ``comments.insert`` (posting replies) and
``comments.setModerationStatus`` (spam handling).

Consent-mode reality (same as the Gmail Agent plugin, verified 2026-07-15): a
Google OAuth app left in *Testing* publishing status expires refresh tokens
after **7 days** — the setup card tells users to publish it to *Production*
(the unverified-app warning is fine for personal use). When YouTube later
returns ``invalid_grant`` (expired Testing token, password change, revoked
access), the worker records it and the page shows a **Reconnect** button.

No google-* libraries: code exchange, token refresh, channel probe and reply
posting are four plain HTTP calls via ``requests``.
"""

import requests
from django.core import signing
from django.utils.crypto import get_random_string

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
YT_API = "https://www.googleapis.com/youtube/v3"

# One scope: manage the authorized channel's YouTube data over SSL — covers
# posting replies and moderating comments. Reads keep using the plain API key.
SCOPE = "https://www.googleapis.com/auth/youtube.force-ssl"

_STATE_SALT = "yt_comments.oauth.state"
STATE_MAX_AGE = 600  # seconds a consent round-trip may take


class OAuthError(Exception):
    """A named, user-displayable OAuth failure. ``reason`` carries Google's
    machine error code (e.g. ``invalid_grant``) when one was returned."""

    def __init__(self, message, *, reason=""):
        super().__init__(message)
        self.reason = reason


def make_state() -> str:
    """A signed, single-purpose state token for the consent round-trip."""
    return signing.dumps({"n": get_random_string(12)}, salt=_STATE_SALT)


def check_state(state: str) -> bool:
    try:
        signing.loads(state or "", salt=_STATE_SALT, max_age=STATE_MAX_AGE)
        return True
    except signing.BadSignature:
        return False


def build_auth_url(client_id: str, redirect_uri: str, state: str) -> str:
    """The Google consent URL. ``prompt=consent`` + ``access_type=offline``
    force a refresh token even on re-connects (Google omits it otherwise)."""
    return (
        f"{AUTH_ENDPOINT}?"
        + requests.compat.urlencode(
            {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": SCOPE,
                "access_type": "offline",
                "prompt": "consent",
                "state": state,
            }
        )
    )


def _token_request(payload: dict) -> dict:
    try:
        resp = requests.post(TOKEN_ENDPOINT, data=payload, timeout=15)
    except requests.exceptions.RequestException as exc:
        raise OAuthError(
            f"Couldn't reach Google's token endpoint ({exc.__class__.__name__})."
        ) from exc
    body = {}
    try:
        body = resp.json()
    except ValueError:
        pass
    if resp.status_code != 200:
        reason = body.get("error", "")
        detail = body.get("error_description") or reason or f"HTTP {resp.status_code}"
        raise OAuthError(f"Google rejected the request: {detail}", reason=reason)
    return body


def exchange_code(client_id: str, client_secret: str, code: str, redirect_uri: str) -> dict:
    """Authorization code → token payload. Raises OAuthError without a refresh token."""
    body = _token_request(
        {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
        }
    )
    if not body.get("refresh_token"):
        raise OAuthError(
            "Google returned no refresh token. Remove the app's existing access at "
            "myaccount.google.com/permissions and connect again."
        )
    return body


def refresh_access_token(client_id: str, client_secret: str, refresh_token: str) -> str:
    """Refresh token → short-lived access token."""
    body = _token_request(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        }
    )
    token = body.get("access_token")
    if not token:
        raise OAuthError("Google's response carried no access token.")
    return token


def fetch_own_channel(access_token: str) -> tuple:
    """``channels.list(mine=true)`` → ``(channel_id, title)`` of the account
    that just consented — validates the token AND lets the caller warn when it
    isn't the channel the plugin monitors."""
    try:
        resp = requests.get(
            f"{YT_API}/channels",
            params={"part": "snippet", "mine": "true"},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
    except requests.exceptions.RequestException as exc:
        raise OAuthError(f"Couldn't reach the YouTube API ({exc.__class__.__name__}).") from exc
    if resp.status_code != 200:
        raise OAuthError(f"YouTube channel probe failed (HTTP {resp.status_code}).")
    items = resp.json().get("items") or []
    if not items:
        raise OAuthError(
            "The Google account you connected has no YouTube channel. "
            "Connect the account (or brand account) that owns the channel."
        )
    return items[0]["id"], items[0].get("snippet", {}).get("title", "")


def post_reply(access_token: str, parent_id: str, text: str) -> str:
    """``comments.insert`` — post ``text`` as a reply to the top-level comment
    ``parent_id``. Returns the new reply's YouTube id. 50 quota units."""
    try:
        resp = requests.post(
            f"{YT_API}/comments",
            params={"part": "snippet"},
            json={"snippet": {"parentId": parent_id, "textOriginal": text}},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
    except requests.exceptions.RequestException as exc:
        raise OAuthError(f"Couldn't reach the YouTube API ({exc.__class__.__name__}).") from exc
    if resp.status_code >= 400:
        reason, message = "", f"HTTP {resp.status_code}"
        try:
            err = resp.json().get("error", {})
            errors = err.get("errors", [])
            reason = errors[0].get("reason", "") if errors else ""
            message = err.get("message") or message
        except (ValueError, IndexError):
            pass
        raise OAuthError(f"YouTube refused the reply: {message}", reason=reason)
    return str(resp.json().get("id", ""))
