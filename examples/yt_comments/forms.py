"""
Config form for the YouTube Comments AI plugin.

Plain ``forms.Form`` (no model) — the plugin persists everything through the SDK
(owner-scoped Secrets + an owned DataStore + an owned managed Database). This
module also owns two cross-process contracts shared by name with the standalone
``worker_body.py``:

  * ``SECRET_FIELDS`` — form-field name → clean env-var the secret injects under.
  * The tag taxonomy — ``DEFAULT_TAGS`` seeds it; the user edits it as
    ``name: description`` lines. ``RESERVED_TAGS`` carry plugin behavior
    (urgent/testimonial drive alerts, question drives reply drafting later,
    spam drives moderation later) so they are always re-added if removed.

Inputs reuse the console's token classes so the page matches the rest of
PyRunner.
"""

import re

from django import forms

# Console input styling (kept in sync with core/forms.py:INPUT_CLASS).
INPUT_CLASS = (
    "w-full px-3.5 py-2.5 bg-ink border border-line rounded-lg text-text text-sm "
    "placeholder-faint/60 focus:outline-none focus:ring-2 focus:ring-ok/30 "
    "focus:border-ok/60 transition-colors"
)
CHECK_CLASS = "h-4 w-4 accent-ok align-middle"

# form-field name -> the clean env-var the secret injects under. The worker reads
# the SAME env-var names from os.environ; they are wired only by matching strings.
# (The OAuth REFRESH token is not here — it has no form field; the Connect
# callback stores it under provisioning.OAUTH_TOKEN_KEY.)
SECRET_FIELDS = {
    "yt_api_key": "YT_API_KEY",
    "yt_oauth_client_id": "YT_OAUTH_CLIENT_ID",
    "yt_oauth_client_secret": "YT_OAUTH_CLIENT_SECRET",
}

# Per-tag reply policy. `urgent` and `spam` can NEVER be auto — a locked
# guardrail: their choice lists simply don't contain it (and provisioning
# re-enforces server-side).
REPLY_MODE_CHOICES = [
    ("off", "Off"),
    ("draft", "Draft for approval"),
    ("auto", "Auto-publish"),
]
NEVER_AUTO_TAGS = ("urgent", "spam")

# Day the weekly insights email goes out (Python weekday: 0 = Monday).
WEEKDAY_CHOICES = [
    ("0", "Monday"), ("1", "Tuesday"), ("2", "Wednesday"), ("3", "Thursday"),
    ("4", "Friday"), ("5", "Saturday"), ("6", "Sunday"),
]

# The taxonomy the AI classifies into. Users edit names + descriptions freely;
# the descriptions ARE the prompt, so specific beats terse.
DEFAULT_TAGS = {
    "urgent": "Needs immediate response - frustrated users, serious complaints, or time-sensitive issues",
    "testimonial": "Positive praise worth featuring - success stories, results achieved, heartfelt thank-you messages",
    "question": "Asks a question that needs an answer",
    "spam": "Irrelevant content, self-promotion, scams, or bot-like messages",
    "negative": "Criticism or complaints that aren't urgent but worth monitoring",
    "positive": "General positive feedback, encouragement, or supportive messages",
    "content_idea": "Suggestions for future videos or content requests",
    "feature_request": "Requests for specific tutorials, features, or topics to cover",
    "brand_mention": "Mentions other brands, tools, products, or competitors",
    "collaboration": "Partnership requests, collab proposals, or business inquiries",
}

# Behavior-bearing tags the plugin itself acts on — silently restored on save.
RESERVED_TAGS = ("urgent", "testimonial", "question", "spam")

_TAG_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,29}$")


def parse_tags(text):
    """Parse ``name: description`` lines into an ordered dict.

    Raises ``forms.ValidationError`` on a malformed line or duplicate name;
    always re-adds missing RESERVED_TAGS (with their default descriptions) so
    alert/reply behavior can't be configured away by accident.
    """
    tags = {}
    for lineno, raw in enumerate((text or "").splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        name, sep, desc = line.partition(":")
        name, desc = name.strip().lower(), desc.strip()
        if not sep or not desc:
            raise forms.ValidationError(
                f"Line {lineno}: use 'name: description' (missing description)."
            )
        if not _TAG_NAME_RE.match(name):
            raise forms.ValidationError(
                f"Line {lineno}: '{name}' — tag names are lowercase letters/digits/underscores "
                "(max 30 chars, starting with a letter)."
            )
        if name in tags:
            raise forms.ValidationError(f"Line {lineno}: duplicate tag '{name}'.")
        tags[name] = desc
    for name in RESERVED_TAGS:
        tags.setdefault(name, DEFAULT_TAGS[name])
    return tags


def tags_to_text(tags):
    """Render a tag dict back into the textarea's ``name: description`` lines."""
    return "\n".join(f"{name}: {desc}" for name, desc in (tags or {}).items())


def _tag_names_tolerant(text):
    """Best-effort tag names from the textarea — used to build the per-tag
    policy selects even while the tags field itself may be invalid. Reserved
    tags are always present (mirroring ``parse_tags``)."""
    names = []
    for raw in (text or "").splitlines():
        name, sep, desc = raw.strip().partition(":")
        name = name.strip().lower()
        if sep and desc.strip() and _TAG_NAME_RE.match(name) and name not in names:
            names.append(name)
    for name in RESERVED_TAGS:
        if name not in names:
            names.append(name)
    return names


def parse_tag_guidance(text):
    """Parse the Brain's ``tag: guidance`` lines into a dict (no reserved
    re-add — guidance is optional per tag)."""
    out = {}
    for lineno, raw in enumerate((text or "").splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        name, sep, desc = line.partition(":")
        name, desc = name.strip().lower(), desc.strip()
        if not sep or not desc:
            raise forms.ValidationError(
                f"Line {lineno}: use 'tag: guidance' (missing guidance)."
            )
        if not _TAG_NAME_RE.match(name):
            raise forms.ValidationError(f"Line {lineno}: '{name}' is not a valid tag name.")
        if name in out:
            raise forms.ValidationError(f"Line {lineno}: duplicate tag '{name}'.")
        out[name] = desc
    return out


def guidance_to_text(guidance):
    return "\n".join(f"{name}: {desc}" for name, desc in (guidance or {}).items())


def _text(**kw):
    return forms.CharField(widget=forms.TextInput(attrs={"class": INPUT_CLASS}), **kw)


def _secret(**kw):
    return forms.CharField(
        widget=forms.PasswordInput(
            render_value=False, attrs={"class": INPUT_CLASS, "autocomplete": "new-password"}
        ),
        **kw,
    )


def _select(choices, **kw):
    return forms.ChoiceField(
        choices=choices, widget=forms.Select(attrs={"class": INPUT_CLASS}), **kw
    )


def _textarea(rows=4, **kw):
    return forms.CharField(
        widget=forms.Textarea(attrs={"class": INPUT_CLASS, "rows": rows}), **kw
    )


def _number(**kw):
    return forms.IntegerField(widget=forms.NumberInput(attrs={"class": INPUT_CLASS}), **kw)


def _checkbox(**kw):
    return forms.BooleanField(
        required=False, widget=forms.CheckboxInput(attrs={"class": CHECK_CLASS}), **kw
    )


def _email(**kw):
    return forms.EmailField(widget=forms.EmailInput(attrs={"class": INPUT_CLASS}), **kw)


def _date(**kw):
    return forms.DateField(
        widget=forms.DateInput(attrs={"class": INPUT_CLASS, "type": "date"}), **kw
    )


def _valid_hhmm(value):
    parts = (value or "").split(":")
    return (
        len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit()
        and 0 <= int(parts[0]) <= 23 and 0 <= int(parts[1]) <= 59
    )


class YtCommentsConfigForm(forms.Form):
    # ---- Channel ----
    channel = _text(
        label="YouTube channel",
        help_text="Channel ID (UC…), @handle, or a channel URL — resolved when you save.",
    )
    start_date = _date(
        label="Analyze comments from",
        help_text="Only comments posted on/after this date are fetched on the first run.",
    )
    yt_api_key = _secret(
        label="YouTube Data API key", required=False,
        help_text="From Google Cloud Console (YouTube Data API v3 enabled). Read-only public data.",
    )

    # ---- Fetching ----
    include_replies = _checkbox(label="Also analyze replies to comments", initial=True)
    max_pages_per_run = _number(
        label="Max fetch pages per run", min_value=1, max_value=100, initial=30,
        help_text="100 comment threads per page, 1 quota unit each. Caps the first-run backfill.",
    )

    # ---- Tag taxonomy ----
    tags = _textarea(
        rows=12, label="Comment tags (one per line: name: description)",
        help_text="The descriptions are the AI's instructions — be specific. "
                  "urgent / testimonial / question / spam are built-in behaviors and always kept.",
    )

    # ---- AI classification ----
    ai_enabled = _checkbox(label="Classify comments with the platform AI provider", initial=True)
    ai_model = _text(
        label="Model override", required=False,
        help_text="Blank = the active AI provider's default model.",
    )
    max_ai_per_run = _number(
        label="Max comments analyzed per run", min_value=10, max_value=2000, initial=200,
        help_text="A cost cap. Comments over the cap stay pending and are analyzed next run.",
    )

    # ---- Reply automation (Stage 3 — OAuth + per-tag policy) ----
    yt_oauth_client_id = _text(
        label="Google OAuth client ID", required=False,
        help_text="A 'Web application' OAuth client from Google Cloud Console. "
                  "Needed only for posting replies and spam moderation.",
    )
    yt_oauth_client_secret = _secret(
        label="Google OAuth client secret", required=False,
        help_text="Stored encrypted; leave blank to keep the saved value.",
    )
    auto_post_daily_cap = _number(
        label="Auto-publish daily cap", min_value=1, max_value=200, initial=10,
        help_text="At most this many auto-published replies per day (UTC). "
                  "Over-cap drafts drop into the approval queue instead.",
    )
    moderate_spam = _checkbox(
        label="Hold AI-tagged spam for review on YouTube (setModerationStatus)",
    )

    # ---- Testimonials (Stage 5 — grading is automatic; avatars are opt-out) ----
    avatar_archive = _checkbox(
        label="Archive testimonial authors' avatars to object storage",
        initial=True,
        help_text="For publishing testimonials outside YouTube. Needs an assets "
                  "connection under Services → Object Storage; stored under a stable "
                  "key per author, so published links keep working and update when "
                  "the avatar changes.",
    )

    # ---- Alerts & digest (instance email) ----
    alerts_enabled = _checkbox(label="Email me urgent + testimonial alerts (batched per run)", initial=True)
    digest_enabled = _checkbox(label="Email me a digest after runs that find new comments", initial=True)
    alert_email = _email(
        label="Send to", required=False,
        help_text="Blank = the instance's default notification email.",
    )
    insights_enabled = _checkbox(
        label="Email a weekly insights report (most-asked questions → FAQ/content "
              "ideas + sentiment trend per video)",
        initial=True,
    )
    insights_weekday = _select(
        WEEKDAY_CHOICES, label="Insights day", required=False, initial="0",
        help_text="The weekly report goes out with that day's run.",
    )

    # ---- Messaging channel alerts (optional, transport-agnostic) ----
    alert_channel = _select(
        [], label="Alert channel", required=False,
        help_text="Urgent + testimonial alerts also go here. Channels are configured "
                  "once under Channels — swap Telegram for another provider anytime "
                  "without touching this plugin.",
    )
    channel_digest_enabled = _checkbox(label="Also send the digest summary to the channel")

    # ---- Environment ----
    environment = _select([], label="Environment")

    # ---- Operational alerts (PyRunner's built-in notifications) ----
    notify_on = _select(
        [("failure", "On failure"), ("both", "On success & failure"), ("never", "Never")],
        label="Failure alerts", initial="failure",
    )
    notify_email = _email(label="Alert email", required=False)

    # ---- Schedule (daily) ----
    schedule_time = _text(label="Daily run time (HH:MM)", initial="08:00")
    timezone = _text(label="Timezone", initial="UTC", required=False)

    def __init__(self, *args, environments=None, configured_secrets=None, channels=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._configured = set(configured_secrets or set())

        env_names = [e.name for e in (environments or [])]
        self.fields["environment"].choices = [(n, n) for n in env_names]
        if not env_names:
            self.fields["environment"].required = False

        # Channel picker — names come from the read-only ChannelAPI (SDK 2.3);
        # the plugin only selects a channel, it never creates or configures one.
        self.fields["alert_channel"].choices = [("", "— none —")] + [
            (c["name"], f'{c["name"]} ({c["channel_type"]})') for c in (channels or [])
        ]

        # The API key is required only on first setup — once stored, blank keeps it.
        for field_name, env_key in SECRET_FIELDS.items():
            if env_key in self._configured:
                self.fields[field_name].widget.attrs["placeholder"] = "configured — leave blank to keep"
        if "YT_API_KEY" not in self._configured:
            self.fields["yt_api_key"].required = True

        if not self.is_bound and not self.initial.get("tags"):
            self.initial["tags"] = tags_to_text(DEFAULT_TAGS)

        # Per-tag reply-policy selects — one ChoiceField per tag, derived from
        # the SUBMITTED tags when bound (a brand-new tag gets its select on the
        # re-render, defaulting to off) or the saved/default taxonomy otherwise.
        # urgent/spam never offer "auto" (locked guardrail) — a tampered POST
        # fails ChoiceField validation.
        source = self.data.get("tags", "") if self.is_bound else self.initial.get("tags", "")
        self._policy_tags = _tag_names_tolerant(source)
        for tag in self._policy_tags:
            choices = [
                c for c in REPLY_MODE_CHOICES
                if not (tag in NEVER_AUTO_TAGS and c[0] == "auto")
            ]
            self.fields[f"policy_{tag}"] = forms.ChoiceField(
                choices=choices, required=False, initial="off", label=tag,
                widget=forms.Select(attrs={"class": INPUT_CLASS}),
            )

    def policy_fields(self):
        """(tag, BoundField) pairs for the Settings template's policy matrix."""
        return [(tag, self[f"policy_{tag}"]) for tag in self._policy_tags]

    def clean_channel(self):
        value = (self.cleaned_data.get("channel") or "").strip()
        if not value:
            raise forms.ValidationError("Add your channel ID, @handle, or channel URL.")
        return value

    def clean_tags(self):
        # Store the parsed dict; provisioning persists it as-is.
        return parse_tags(self.cleaned_data.get("tags"))

    def clean_timezone(self):
        return self.cleaned_data.get("timezone") or "UTC"

    def clean(self):
        cleaned = super().clean()
        if not _valid_hhmm(cleaned.get("schedule_time")):
            self.add_error("schedule_time", "Use 24-hour HH:MM, e.g. 08:00.")
        if cleaned.get("channel_digest_enabled") and not cleaned.get("alert_channel"):
            self.add_error("alert_channel", "Pick a channel to send the digest summary to.")

        # Assemble the per-tag policy dict provisioning persists. The choices
        # already exclude auto for urgent/spam; this is belt-and-suspenders.
        policies = {}
        for tag in self._policy_tags:
            mode = cleaned.get(f"policy_{tag}") or "off"
            if tag in NEVER_AUTO_TAGS and mode == "auto":
                self.add_error(f"policy_{tag}", f"'{tag}' can never be auto-published.")
                mode = "draft"
            policies[tag] = mode
        cleaned["reply_policies"] = policies
        return cleaned


class BrainForm(forms.Form):
    """The Reply Brain — voice / knowledge / rules injected into every drafting
    prompt (plus optional per-tag guidance). Saved to its own store entry so a
    Settings re-save never touches it."""

    voice = _textarea(
        rows=4, required=False, label="Voice & style — how you write",
        help_text="e.g. \"Warm and direct, light humor, first person, no emojis, "
                  "sign off with -Hasan\". Blank = a friendly, concise default.",
    )
    knowledge = _textarea(
        rows=8, required=False, label="Knowledge — facts, links & canned answers",
        help_text="Product facts, FAQs, links to your courses/tools. These are the "
                  "ONLY links an auto-published reply may contain — a draft with "
                  "any other URL is held for your approval.",
    )
    rules = _textarea(
        rows=4, required=False, label="Rules — always / never",
        help_text="e.g. \"Never promise release dates. Always thank first-time "
                  "commenters. Never discuss pricing in comments.\"",
    )
    tag_guidance = _textarea(
        rows=4, required=False,
        label="Per-tag guidance (one per line: tag: guidance)",
        help_text="e.g. \"question: answer directly, then point to the relevant "
                  "video if one exists\".",
    )

    def clean_tag_guidance(self):
        return parse_tag_guidance(self.cleaned_data.get("tag_guidance"))
