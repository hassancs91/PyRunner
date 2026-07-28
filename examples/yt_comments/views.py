"""
YouTube Comments AI plugin — views (web process).

Superuser-only. One page with three tabs: Inbox (filterable comment feed read
straight from the plugin's managed Postgres database), Testimonials (collector +
Markdown/CSV export) and Settings (the config form that provisions everything).

All persistence/orchestration goes through ``provisioning`` (which uses
``core.plugins.api``); the dashboard reads the plugin's OWN tables via the
database's scoped DSN — never core models.
"""

import csv
import json
from datetime import datetime

from django.contrib import messages
from django.contrib.auth.decorators import user_passes_test
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from . import oauth
from . import provisioning as prov
from .forms import BrainForm, YtCommentsConfigForm, guidance_to_text

superuser_required = user_passes_test(lambda u: u.is_superuser)

INBOX_LIMIT = 100     # comments per inbox page
TESTIMONIAL_LIMIT = 200
HISTORY_LIMIT = 25    # rows in the run-history table
QUEUE_LIMIT = 100     # pending drafts shown on the Replies tab
AUDIT_LIMIT = 50      # decided/posted rows shown below the queue

_RUN_BADGE = {
    "success": ("Success", "bg-ok/10 text-ok", "bg-ok"),
    "partial": ("Partial", "bg-warn/10 text-warn", "bg-warn"),
    "failed": ("Failed", "bg-fail/10 text-fail", "bg-fail"),
}

# tag -> hex accent for the pill. Unknown/custom tags fall back to slate.
_TAG_COLORS = {
    "urgent": "#f87171",
    "testimonial": "#34d399",
    "question": "#60a5fa",
    "spam": "#94a3b8",
    "negative": "#fb923c",
    "positive": "#34d399",
    "content_idea": "#a78bfa",
    "feature_request": "#a78bfa",
    "brand_mention": "#22d3ee",
    "collaboration": "#e879f9",
}


def _comment_link(video_id, comment_id):
    return f"https://www.youtube.com/watch?v={video_id}&lc={comment_id}"


def _sentiment_view(value):
    if value is None:
        return None
    v = float(value)
    if v > 0.2:
        return {"label": f"+{v:.1f}", "color": "#34d399"}
    if v < -0.2:
        return {"label": f"{v:.1f}", "color": "#f87171"}
    return {"label": f"{v:+.1f}", "color": "#94a3b8"}


# Publish-worthiness grade -> badge (Stage 5). Ungraded rows get no badge.
_GRADE_BADGE = {
    "feature": ("⭐ Feature", "bg-warn/10 text-warn"),
    "solid": ("Solid", "bg-ok/10 text-ok"),
    "generic": ("Generic", "bg-panel-hi text-faint"),
}
_GRADES = ("feature", "solid", "generic")


def _comment_view(row):
    tags = row.get("tags") or []
    if isinstance(tags, str):  # jsonb arrives as list; be tolerant anyway
        try:
            tags = json.loads(tags)
        except ValueError:
            tags = []
    published = row.get("published_at")
    grade = row.get("testimonial_grade") or ""
    badge = _GRADE_BADGE.get(grade)
    return {
        "comment_id": row["comment_id"],
        "is_reply": bool(row.get("parent_id")),
        "author": row.get("author") or "—",
        "text": row.get("text_original") or "",
        "video_title": row.get("video_title") or row.get("video_id") or "—",
        "video_id": row.get("video_id") or "",
        "like_count": row.get("like_count") or 0,
        "status": row.get("status") or "",
        "tags": [{"name": t, "color": _TAG_COLORS.get(t, "#94a3b8")} for t in tags],
        "sentiment": _sentiment_view(row.get("sentiment")),
        "reasoning": row.get("reasoning") or "",
        "grade": grade,
        "grade_badge": {"label": badge[0], "cls": badge[1]} if badge else None,
        "grade_note": row.get("testimonial_note") or "",
        "avatar_key": row.get("avatar_key") or "",
        "published": published.strftime("%Y-%m-%d %H:%M") if isinstance(published, datetime) else str(published or "")[:16],
        "link": _comment_link(row.get("video_id") or "", row["comment_id"]),
    }


def _run_view(raw):
    status = raw.get("status", "unknown")
    label, cls, dot = _RUN_BADGE.get(status, ("Unknown", "bg-panel-hi text-muted", "bg-muted"))
    return {
        "ts": raw.get("ts", "—"),
        "badge": {"label": label, "cls": cls, "dot": dot},
        "new_comments": raw.get("new_comments", 0),
        "analyzed": raw.get("analyzed", 0),
        "urgent": raw.get("urgent", 0),
        "testimonials": raw.get("testimonials", 0),
        "quota_units": raw.get("quota_units", 0),
        "capped": raw.get("capped", False),
        "seeded_run": raw.get("seeded_run", False),
        "errors": raw.get("errors", []),
        "drafted": raw.get("drafted", 0),
        "posted": raw.get("posted", 0),
    }


# {extra} is the optional grade filter; best grades first, then newest.
_TESTIMONIAL_SELECT = """
SELECT c.comment_id, c.parent_id, c.video_id, c.author, c.text_original,
       c.published_at, c.like_count, c.status, c.tags, c.sentiment,
       c.reasoning, c.testimonial_grade, c.testimonial_note, a.avatar_key,
       COALESCE(NULLIF(v.title, ''), c.video_id) AS video_title
FROM comments c
LEFT JOIN videos v USING (video_id)
LEFT JOIN authors a ON a.author_channel_id = c.author_channel_id
WHERE c.tags @> %s::jsonb{extra}
ORDER BY CASE c.testimonial_grade
             WHEN 'feature' THEN 0 WHEN 'solid' THEN 1
             WHEN 'generic' THEN 3 ELSE 2 END,
         c.published_at DESC
LIMIT %s
"""


def _testimonial_rows(grade=""):
    """Testimonial rows with grade + avatar info, most publish-worthy first.

    Falls back to the pre-0.5.0 shape when the grade columns / authors table
    don't exist yet (upgraded install before the worker's first 0.5.0 run) —
    the tab must keep showing the old collection, not blank on an error banner.
    """
    params = [json.dumps(["testimonial"])]
    extra = ""
    if grade == "ungraded":
        extra = " AND c.testimonial_grade IS NULL"
    elif grade in _GRADES:
        extra = " AND c.testimonial_grade = %s"
        params.append(grade)
    try:
        return prov.db_rows(_TESTIMONIAL_SELECT.format(extra=extra),
                            tuple(params) + (TESTIMONIAL_LIMIT,))
    except Exception as exc:
        import psycopg

        if not isinstance(exc, (psycopg.errors.UndefinedColumn,
                                psycopg.errors.UndefinedTable)):
            raise
    return prov.db_rows(  # legacy shape (no grades to filter by yet)
        """
        SELECT c.comment_id, c.parent_id, c.video_id, c.author, c.text_original,
               c.published_at, c.like_count, c.status, c.tags, c.sentiment,
               c.reasoning, COALESCE(NULLIF(v.title, ''), c.video_id) AS video_title
        FROM comments c LEFT JOIN videos v USING (video_id)
        WHERE c.tags @> %s::jsonb
        ORDER BY c.published_at DESC
        LIMIT %s
        """,
        (json.dumps(["testimonial"]), TESTIMONIAL_LIMIT),
    )


def _grade_counts():
    """{grade|'ungraded': n} for the filter chips; {} pre-0.5.0-columns."""
    try:
        rows = prov.db_rows(
            "SELECT testimonial_grade AS grade, count(*) AS n FROM comments "
            "WHERE tags @> %s::jsonb GROUP BY testimonial_grade",
            (json.dumps(["testimonial"]),),
        )
        return {(r["grade"] or "ungraded"): r["n"] for r in rows}
    except LookupError:
        return {}
    except Exception as exc:
        import psycopg

        if isinstance(exc, psycopg.Error):
            return {}
        raise


def _testimonial_views(grade=""):
    """Testimonial view-dicts with archived-avatar URLs attached (used by both
    the tab and the exports so they can never disagree)."""
    rows = [_comment_view(r) for r in _testimonial_rows(grade=grade)]
    urls = prov.storage_urls([c["avatar_key"] for c in rows])
    for c in rows:
        c["avatar"] = urls.get(c["avatar_key"], "")
    return rows


_REPLY_BADGE = {
    "posted": ("Posted", "bg-ok/10 text-ok"),
    "approved": ("Approved — posting", "bg-warn/10 text-warn"),
    "rejected": ("Rejected", "bg-panel-hi text-muted"),
    "failed": ("Failed", "bg-fail/10 text-fail"),
    "pending_approval": ("Awaiting approval", "bg-warn/10 text-warn"),
}

_REPLY_SELECT = """
SELECT r.id, r.comment_id, r.draft_text, r.final_text, r.mode, r.status,
       r.guard_note, r.yt_reply_id, r.error, r.created_at, r.decided_at,
       r.decided_by, r.posted_at, c.author, c.text_original, c.video_id,
       c.tags, c.published_at,
       COALESCE(NULLIF(v.title, ''), c.video_id) AS video_title
FROM replies r
JOIN comments c USING (comment_id)
LEFT JOIN videos v ON v.video_id = c.video_id
"""


def _reply_view(row):
    comment = _comment_view(row)
    label, cls = _REPLY_BADGE.get(row["status"], (row["status"], "bg-panel-hi text-muted"))
    posted = row.get("posted_at")
    decided = row.get("decided_at")
    return {
        "id": row["id"],
        "comment": comment,
        "text": row.get("final_text") or row.get("draft_text") or "",
        "mode": row.get("mode") or "draft",
        "status": row.get("status") or "",
        "badge": {"label": label, "cls": cls},
        "guard_note": row.get("guard_note") or "",
        "error": row.get("error") or "",
        "decided_by": row.get("decided_by") or "",
        "when": (posted or decided or row.get("created_at")),
        "yt_link": _comment_link(row.get("video_id") or "", row["yt_reply_id"])
                   if row.get("yt_reply_id") else "",
    }


def _reply_data():
    """Queue + audit for the Replies tab, guarded separately from the comment
    queries: on an upgraded install the `replies` table doesn't exist until the
    worker's first v0.3 run, and that must not blank the Inbox."""
    data = {"queue": [], "reply_audit": [], "queue_total": 0}
    try:
        data["queue"] = [_reply_view(r) for r in prov.db_rows(
            _REPLY_SELECT + "WHERE r.status = 'pending_approval' "
                            "ORDER BY r.created_at DESC LIMIT %s",
            (QUEUE_LIMIT,),
        )]
        data["reply_audit"] = [_reply_view(r) for r in prov.db_rows(
            _REPLY_SELECT + "WHERE r.status <> 'pending_approval' "
                            "ORDER BY COALESCE(r.posted_at, r.decided_at, r.created_at) DESC "
                            "LIMIT %s",
            (AUDIT_LIMIT,),
        )]
        count = prov.db_rows("SELECT count(*) AS n FROM replies WHERE status = 'pending_approval'")
        data["queue_total"] = (count[0] or {}).get("n", 0) if count else 0
    except LookupError:
        pass
    except Exception as exc:
        import psycopg

        if not isinstance(exc, psycopg.Error):
            raise  # UndefinedTable et al. → honest empty queue
    return data


def _dashboard_data(request):
    """Everything the Inbox/Testimonials tabs need, from the plugin's database.

    ``db_state`` distinguishes the honest empty states: 'ok', 'unprovisioned'
    (no database yet — save settings first), 'empty' (tables not created — the
    worker hasn't run), or an error banner text.
    """
    data = {
        "db_state": "ok",
        "totals": {}, "tag_counts": [], "videos": [],
        "inbox": [], "testimonials": [], "inbox_total": 0,
        "filter_tag": (request.GET.get("tag") or "").strip(),
        "filter_video": (request.GET.get("video") or "").strip(),
        "filter_status": (request.GET.get("status") or "").strip(),
        "filter_grade": (request.GET.get("grade") or "").strip(),
        "grade_counts": {}, "avatar_links_expire": False,
        "page": max(0, int(request.GET.get("page") or 0)),
    }
    try:
        totals = prov.db_rows(
            """
            SELECT count(*) AS total,
                   count(*) FILTER (WHERE status = 'pending_analysis') AS pending,
                   count(*) FILTER (WHERE tags @> '["urgent"]'::jsonb
                                    AND published_at > now() - interval '7 days') AS urgent_7d,
                   count(*) FILTER (WHERE tags @> '["testimonial"]'::jsonb) AS testimonials
            FROM comments
            """
        )
        data["totals"] = totals[0] if totals else {}

        data["tag_counts"] = prov.db_rows(
            """
            SELECT t.tag AS tag, count(*) AS n
            FROM comments c, jsonb_array_elements_text(c.tags) AS t(tag)
            WHERE c.published_at > now() - interval '30 days'
            GROUP BY t.tag ORDER BY n DESC
            """
        )

        data["videos"] = prov.db_rows(
            """
            SELECT c.video_id, COALESCE(NULLIF(v.title, ''), c.video_id) AS title,
                   count(*) AS n, max(c.published_at) AS latest
            FROM comments c LEFT JOIN videos v USING (video_id)
            GROUP BY c.video_id, v.title
            ORDER BY latest DESC
            LIMIT 100
            """
        )

        where, params = ["true"], []
        if data["filter_tag"]:
            where.append("c.tags @> %s::jsonb")
            params.append(json.dumps([data["filter_tag"]]))
        if data["filter_video"]:
            where.append("c.video_id = %s")
            params.append(data["filter_video"])
        if data["filter_status"]:
            where.append("c.status = %s")
            params.append(data["filter_status"])

        count_row = prov.db_rows(
            f"SELECT count(*) AS n FROM comments c WHERE {' AND '.join(where)}", tuple(params)
        )
        data["inbox_total"] = (count_row[0] or {}).get("n", 0) if count_row else 0

        rows = prov.db_rows(
            f"""
            SELECT c.comment_id, c.parent_id, c.video_id, c.author, c.text_original,
                   c.published_at, c.like_count, c.status, c.tags, c.sentiment,
                   c.reasoning, COALESCE(NULLIF(v.title, ''), c.video_id) AS video_title
            FROM comments c LEFT JOIN videos v USING (video_id)
            WHERE {' AND '.join(where)}
            ORDER BY c.published_at DESC
            LIMIT %s OFFSET %s
            """,
            tuple(params) + (INBOX_LIMIT, data["page"] * INBOX_LIMIT),
        )
        data["inbox"] = [_comment_view(r) for r in rows]
        data["testimonials"] = _testimonial_views(grade=data["filter_grade"])
        data["grade_counts"] = _grade_counts()
        # Presigned URLs expire — worth a hint when the user means to publish.
        data["avatar_links_expire"] = any(
            "X-Amz-" in (c.get("avatar") or "") for c in data["testimonials"]
        )
    except LookupError:
        data["db_state"] = "unprovisioned"
    except Exception as exc:
        import psycopg

        if isinstance(exc, psycopg.errors.UndefinedTable):
            data["db_state"] = "empty"  # worker hasn't created tables yet
        elif isinstance(exc, psycopg.Error):
            data["db_state"] = f"Database error: {exc}"
        else:
            raise
    return data


def _dashboard_context(request):
    cfg = prov.get_config()
    stats = prov.get_stats()
    script = prov.get_script()
    runs = [r for r in prov.get_runs() if isinstance(r, dict)]
    history = [_run_view(r) for r in reversed(runs)][:HISTORY_LIMIT]

    ctx = {
        "is_configured": script is not None,
        "channel_title": cfg.get("channel_title") or cfg.get("channel_id") or "",
        "tag_names": list((cfg.get("tags") or {}).keys()),
        "stats": stats,
        "last_run": (stats.get("last_run", "") or "")[:16].replace("T", " "),
        "history": history,
        "can_run": bool(script and script.can_run),
        "db_available": prov.database_available(),
        "has_more": False,
    }
    ctx.update(_dashboard_data(request))
    ctx.update(_reply_data())
    ctx["reply_policy_on"] = any(
        m in ("draft", "auto") for m in (cfg.get("reply_policies") or {}).values()
    )
    ctx["has_more"] = ctx["inbox_total"] > (ctx["page"] + 1) * INBOX_LIMIT
    ctx["has_data"] = bool(ctx["inbox"]) or bool(ctx["totals"].get("total"))
    return ctx


def _build_form(request, data=None):
    return YtCommentsConfigForm(
        data,
        initial=None if data else prov.initial_from_config(),
        environments=prov.list_environments(),
        configured_secrets=prov.configured_secret_keys(),
        channels=prov.list_channels(),
    )


def _redirect_uri(request):
    """The absolute OAuth callback URL — shown in Settings (it must be added to
    the Google OAuth client as an authorized redirect URI) and sent with every
    consent round-trip. Honors X-Forwarded-Proto behind a proxy."""
    return request.build_absolute_uri(reverse("yt_comments:oauth_callback"))


def _build_brain_form(data=None):
    if data is not None:
        return BrainForm(data)
    brain = prov.get_brain()
    return BrainForm(initial={
        "voice": brain["voice"],
        "knowledge": brain["knowledge"],
        "rules": brain["rules"],
        "tag_guidance": guidance_to_text(brain["tag_guidance"]),
    })


def _render(request, form, brain_form=None, force_tab=""):
    oauth_state = prov.get_oauth()
    client_id, client_secret = prov.oauth_client()
    ctx = {
        "form": form,
        "brain_form": brain_form or _build_brain_form(),
        "force_tab": force_tab,
        "has_environments": bool(prov.list_environments()),
        "schedule": prov.schedule_summary(),
        "is_active": prov.live_status()["active"],
        "oauth": oauth_state,
        "oauth_connected": bool(oauth_state.get("connected")),
        "oauth_needs_reconnect": oauth_state.get("status") == "invalid_grant",
        "oauth_client_configured": bool(client_id and client_secret),
        "redirect_uri": _redirect_uri(request),
    }
    ctx.update(_dashboard_context(request))
    return render(request, "yt_comments/index.html", ctx)


@superuser_required
def index(request):
    return _render(request, _build_form(request))


@superuser_required
@require_POST
def save(request):
    form = _build_form(request, data=request.POST)
    if not form.is_valid():
        messages.error(request, "Please fix the errors below.")
        return _render(request, form, force_tab="settings")

    try:
        _, warnings = prov.provision(form.cleaned_data, created_by=request.user)
    except Exception as exc:
        messages.error(request, f"Could not save settings: {exc}")
        return _render(request, form, force_tab="settings")

    messages.success(
        request,
        "Settings saved — the analyzer script, secret, database and daily schedule are provisioned.",
    )
    for w in warnings:
        messages.warning(request, w)
    return redirect(reverse("yt_comments:index") + "#settings")


@superuser_required
@require_POST
def run(request):
    _, error = prov.queue_run(triggered_by=request.user)
    if error:
        messages.error(request, error)
    else:
        messages.info(request, "Analyzer queued — watch it run below.")
    return redirect(reverse("yt_comments:index") + "#inbox")


@superuser_required
def status(request):
    """JSON snapshot for the page's live-status poller (run state + progress)."""
    return JsonResponse(prov.live_status())


@superuser_required
@require_POST
def stop(request):
    if prov.cancel_running():
        messages.info(request, "Stopping the running analyzer…")
    else:
        messages.error(request, "There's no running analyzer to stop.")
    return redirect(reverse("yt_comments:index") + "#inbox")


@superuser_required
@require_POST
def test_youtube(request):
    """Probe the YouTube key + channel with the submitted (or saved) values."""
    return JsonResponse(prov.test_youtube(request.POST))


# --------------------------------------------------------------------------- #
# OAuth connect flow (Connect → Google consent → callback → token stored)
# --------------------------------------------------------------------------- #

@superuser_required
@require_POST
def oauth_start(request):
    """Kick off the consent round-trip (also serves as Reconnect)."""
    if not prov.oauth_ready():
        messages.error(
            request,
            "Save the settings with a Google OAuth client ID + secret first — "
            "then Connect.",
        )
        return redirect(reverse("yt_comments:index") + "#settings")
    client_id, _ = prov.oauth_client()
    url = oauth.build_auth_url(client_id, _redirect_uri(request), oauth.make_state())
    return redirect(url)


@superuser_required
def oauth_callback(request):
    """The browser arrives here FROM Google carrying our signed state + a code."""
    error = request.GET.get("error")
    if error:
        messages.error(request, f"Google returned an error: {error}")
        return redirect(reverse("yt_comments:index") + "#settings")
    if not oauth.check_state(request.GET.get("state", "")):
        messages.error(request, "The sign-in link expired or was tampered with — try again.")
        return redirect(reverse("yt_comments:index") + "#settings")
    code = request.GET.get("code", "")
    if not code:
        messages.error(request, "Google sent no authorization code.")
        return redirect(reverse("yt_comments:index") + "#settings")

    client_id, client_secret = prov.oauth_client()
    try:
        tokens = oauth.exchange_code(client_id, client_secret, code, _redirect_uri(request))
        channel_id, channel_title = oauth.fetch_own_channel(tokens["access_token"])
        prov.store_refresh_token(tokens["refresh_token"], channel_id, channel_title)
    except (oauth.OAuthError, ValueError) as exc:
        messages.error(request, str(exc))
        return redirect(reverse("yt_comments:index") + "#settings")

    messages.success(
        request,
        f"YouTube connected as “{channel_title}” — replies and spam moderation are unlocked.",
    )
    configured = (prov.get_config().get("channel_id") or "").strip()
    if configured and configured != channel_id:
        messages.warning(
            request,
            f"Heads-up: the connected account's channel ({channel_title}, {channel_id}) "
            f"is not the channel this plugin monitors ({configured}). Replies are "
            "posted AS the connected account — reconnect with the channel owner "
            "(or brand account) if that's not what you want.",
        )
    return redirect(reverse("yt_comments:index") + "#settings")


@superuser_required
@require_POST
def oauth_disconnect(request):
    try:
        prov.disconnect_oauth()
    except ValueError as exc:  # store not provisioned — nothing to disconnect
        messages.error(request, str(exc))
        return redirect(reverse("yt_comments:index") + "#settings")
    messages.info(
        request,
        "YouTube disconnected — replies and moderation are off; monitoring "
        "continues on the API key. You can also revoke the app at "
        "myaccount.google.com/permissions.",
    )
    return redirect(reverse("yt_comments:index") + "#settings")


# --------------------------------------------------------------------------- #
# Reply Brain
# --------------------------------------------------------------------------- #

@superuser_required
@require_POST
def brain_save(request):
    form = _build_brain_form(request.POST)
    if not form.is_valid():
        messages.error(request, "Please fix the errors below.")
        return _render(request, _build_form(request), brain_form=form, force_tab="brain")
    try:
        prov.save_brain(form.cleaned_data)
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect(reverse("yt_comments:index") + "#brain")
    messages.success(request, "Reply Brain saved — it shapes every draft from the next run on.")
    return redirect(reverse("yt_comments:index") + "#brain")


# --------------------------------------------------------------------------- #
# Reply Queue (approve/edit → post now · reject)
# --------------------------------------------------------------------------- #

def _load_reply(reply_id):
    rows = prov.db_rows(
        "SELECT r.id, r.comment_id, r.status, c.parent_id FROM replies r "
        "JOIN comments c USING (comment_id) WHERE r.id = %s",
        (reply_id,),
    )
    return rows[0] if rows else None


@superuser_required
@require_POST
def reply_approve(request, reply_id):
    """Approve (with edits) and post immediately; on a transient failure the
    row stays ``approved`` and the worker retries next run."""
    final_text = (request.POST.get("final_text") or "").strip()
    if not final_text:
        messages.error(request, "The reply text is empty — write something or reject it.")
        return redirect(reverse("yt_comments:index") + "#replies")
    try:
        row = _load_reply(reply_id)
        if row is None or row["status"] not in ("pending_approval", "approved"):
            messages.error(request, "That draft is gone or was already decided.")
            return redirect(reverse("yt_comments:index") + "#replies")
        prov.db_execute(
            "UPDATE replies SET final_text = %s, status = 'approved', guard_note = '', "
            "decided_at = now(), decided_by = %s WHERE id = %s",
            (final_text, request.user.get_username(), reply_id),
        )
        yt_id, error = prov.post_reply_now(row["parent_id"] or row["comment_id"], final_text)
        if yt_id:
            prov.db_execute(
                "UPDATE replies SET status = 'posted', yt_reply_id = %s, "
                "posted_at = now(), error = '' WHERE id = %s",
                (yt_id, reply_id),
            )
            messages.success(request, "Reply posted to YouTube.")
        else:
            prov.db_execute("UPDATE replies SET error = %s WHERE id = %s", (error, reply_id))
            messages.warning(
                request,
                f"Approved, but posting failed: {error} The next analyzer run retries.",
            )
    except LookupError:
        messages.error(request, "The plugin database isn't provisioned yet.")
    except Exception as exc:
        import psycopg

        if isinstance(exc, psycopg.Error):
            messages.error(request, f"Database error: {exc}")
        else:
            raise
    return redirect(reverse("yt_comments:index") + "#replies")


@superuser_required
@require_POST
def reply_reject(request, reply_id):
    try:
        updated = prov.db_execute(
            "UPDATE replies SET status = 'rejected', decided_at = now(), decided_by = %s "
            "WHERE id = %s AND status IN ('pending_approval', 'approved')",
            (request.user.get_username(), reply_id),
        )
        if updated:
            messages.info(request, "Draft rejected — it will never be posted.")
        else:
            messages.error(request, "That draft is gone or was already decided.")
    except LookupError:
        messages.error(request, "The plugin database isn't provisioned yet.")
    except Exception as exc:
        import psycopg

        if isinstance(exc, psycopg.Error):
            messages.error(request, f"Database error: {exc}")
        else:
            raise
    return redirect(reverse("yt_comments:index") + "#replies")


@superuser_required
def export_testimonials(request, fmt):
    """Download the testimonial collection as Markdown or CSV (honors the tab's
    ?grade= filter; rows come publish-worthiest first, with grade + archived
    avatar URL alongside so a website build needs nothing else)."""
    if fmt not in ("md", "csv"):
        return HttpResponse(status=404)
    grade = (request.GET.get("grade") or "").strip()
    try:
        rows = _testimonial_views(grade=grade)
    except Exception:
        rows = []
    stamp = datetime.now().strftime("%Y-%m-%d")

    if fmt == "csv":
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="testimonials-{stamp}.csv"'
        writer = csv.writer(response)
        writer.writerow(["author", "comment", "grade", "why", "video",
                         "published", "likes", "link", "avatar_url"])
        for c in rows:
            writer.writerow([c["author"], c["text"], c["grade"], c["grade_note"],
                             c["video_title"], c["published"], c["like_count"],
                             c["link"], c.get("avatar") or ""])
        return response

    lines = [f"# Testimonials — {stamp}", ""]
    for c in rows:
        byline = (f"> — **{c['author']}** on *{c['video_title']}* ({c['published']}, "
                  f"👍 {c['like_count']}) · [source]({c['link']})")
        if c["grade"]:
            byline += f" · grade: {c['grade']}"
        if c.get("avatar"):
            byline += f" · [avatar]({c['avatar']})"
        lines.append(f"> {c['text']}")
        lines.append(">")
        lines.append(byline)
        lines.append("")
    response = HttpResponse("\n".join(lines), content_type="text/markdown; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="testimonials-{stamp}.md"'
    return response
