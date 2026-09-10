"""
Config form for the Brand Tracker plugin.

Plain ``forms.Form`` (no model) — the plugin persists everything through the SDK
(owner-scoped Secrets + an owned DataStore). This module also owns the
cross-process secret contract (``SECRET_FIELDS``) that both ``provisioning.py``
(web side) and ``worker_body.py`` (script side) agree on by name. Inputs reuse the
console's token classes so the page matches the rest of PyRun.
"""

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
# (OPENROUTER_API_KEY is added in the enrichment stage.)
SECRET_FIELDS = {
    "serper_api_key": "SERPER_API_KEY",
    "reddit_client_id": "REDDIT_CLIENT_ID",
    "reddit_client_secret": "REDDIT_CLIENT_SECRET",
    "resend_api_key": "RESEND_API_KEY",
    "openrouter_api_key": "OPENROUTER_API_KEY",
}

WEEKDAYS = [
    ("0", "Monday"), ("1", "Tuesday"), ("2", "Wednesday"), ("3", "Thursday"),
    ("4", "Friday"), ("5", "Saturday"), ("6", "Sunday"),
]


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


def _textarea(**kw):
    return forms.CharField(
        widget=forms.Textarea(attrs={"class": INPUT_CLASS, "rows": 4}), **kw
    )


def _number(**kw):
    return forms.IntegerField(widget=forms.NumberInput(attrs={"class": INPUT_CLASS}), **kw)


def _checkbox(**kw):
    return forms.BooleanField(
        required=False, widget=forms.CheckboxInput(attrs={"class": CHECK_CLASS}), **kw
    )


def _email(**kw):
    return forms.EmailField(widget=forms.EmailInput(attrs={"class": INPUT_CLASS}), **kw)


def _valid_hhmm(value):
    parts = (value or "").split(":")
    return (
        len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit()
        and 0 <= int(parts[0]) <= 23 and 0 <= int(parts[1]) <= 59
    )


class BrandTrackerConfigForm(forms.Form):
    # ---- What to track ----
    keywords = _textarea(
        label="Keywords to track",
        help_text="One keyword or exact phrase per line.",
    )
    excluded_domains = _textarea(
        label="Excluded domains", required=False,
        help_text="One domain per line (e.g. yoursite.com). Mentions from these are ignored.",
    )
    num_results = _number(
        label="Results per keyword", min_value=1, max_value=100, initial=10,
        help_text="Per source. More than 10 doubles the Serper credit cost.",
    )

    # ---- Sources (Serper web is always on) ----
    news_enabled = _checkbox(label="Google News (Serper — uses credits)", initial=True)
    hackernews_enabled = _checkbox(label="Hacker News (free)")
    reddit_enabled = _checkbox(label="Reddit (free — needs Reddit API credentials)")

    # ---- Credentials (write-only; blank = keep existing) ----
    serper_api_key = _secret(label="Serper API key", required=False)
    reddit_client_id = _secret(label="Reddit Client ID", required=False)
    reddit_client_secret = _secret(label="Reddit Client Secret", required=False)

    # ---- Retention & budget ----
    retention_days = _number(
        label="Keep mentions (days)", min_value=1, max_value=3650, initial=90,
    )
    monthly_credit_cap = _number(
        label="Monthly Serper credit cap", min_value=0, initial=0, required=False,
        help_text="0 = no cap. Serper searches pause for the month when reached; free sources keep running.",
    )

    # ---- Optional email report (Resend) ----
    email_enabled = _checkbox(label="Email me a report of new mentions")
    email_to = _email(label="Send report to", required=False)
    email_from = _text(label="From address (must be verified in Resend)", required=False)
    resend_api_key = _secret(label="Resend API key", required=False)

    # ---- AI enrichment (optional) ----
    enrich_provider = _select(
        [("off", "Off"), ("claude", "Claude (platform — no key)"), ("openrouter", "OpenRouter (your key)")],
        label="AI enrichment", initial="off",
        help_text="Tag each mention with source type + sentiment. Degrades safely if unavailable.",
    )
    enrich_model = _text(
        label="Model", required=False,
        help_text="OpenRouter: e.g. openai/gpt-4o-mini. Claude: optional override (blank = server default).",
    )
    openrouter_api_key = _secret(label="OpenRouter API key", required=False)

    # ---- Environment ----
    environment = _select([], label="Environment")

    # ---- Operational alerts (PyRun's built-in notifications) ----
    notify_on = _select(
        [("failure", "On failure"), ("both", "On success & failure"), ("never", "Never")],
        label="Failure alerts", initial="failure",
    )
    notify_email = _email(
        label="Alert email", required=False,
    )

    # ---- Schedule (weekly only in v1 — matches the past-week search window) ----
    schedule_weekday = _select(WEEKDAYS, label="Day of week", initial="0")
    schedule_time = _text(label="Time (HH:MM)", initial="08:00")
    timezone = _text(label="Timezone", initial="UTC", required=False)

    def __init__(self, *args, environments=None, configured_secrets=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._configured = set(configured_secrets or set())

        env_names = [e.name for e in (environments or [])]
        self.fields["environment"].choices = [(n, n) for n in env_names]
        if not env_names:
            self.fields["environment"].required = False

        # The Serper key is required only on first setup — once stored, blank keeps it.
        for field_name, env_key in SECRET_FIELDS.items():
            if env_key in self._configured:
                self.fields[field_name].widget.attrs["placeholder"] = "configured — leave blank to keep"
        if "SERPER_API_KEY" not in self._configured:
            self.fields["serper_api_key"].required = True

    def clean_keywords(self):
        kws = [k.strip() for k in (self.cleaned_data.get("keywords") or "").splitlines() if k.strip()]
        if not kws:
            raise forms.ValidationError("Add at least one keyword to track.")
        return self.cleaned_data["keywords"]

    def clean_timezone(self):
        return self.cleaned_data.get("timezone") or "UTC"

    def clean(self):
        cleaned = super().clean()

        if not _valid_hhmm(cleaned.get("schedule_time")):
            self.add_error("schedule_time", "Use 24-hour HH:MM, e.g. 08:00.")

        # The email report needs a destination, a verified sender, and a Resend key.
        if cleaned.get("email_enabled"):
            if not cleaned.get("email_to"):
                self.add_error("email_to", "Add an address to send the report to.")
            if not cleaned.get("email_from"):
                self.add_error("email_from", "Add a from address verified in Resend.")
            has_resend = (cleaned.get("resend_api_key") or "").strip() or "RESEND_API_KEY" in self._configured
            if not has_resend:
                self.add_error("resend_api_key", "Add a Resend API key to send the email report.")

        # OpenRouter enrichment needs a key + a model.
        if cleaned.get("enrich_provider") == "openrouter":
            has_or = (cleaned.get("openrouter_api_key") or "").strip() or "OPENROUTER_API_KEY" in self._configured
            if not has_or:
                self.add_error("openrouter_api_key", "Add an OpenRouter API key, or set enrichment to Off/Claude.")
            if not (cleaned.get("enrich_model") or "").strip():
                self.add_error("enrich_model", "Add an OpenRouter model, e.g. openai/gpt-4o-mini.")
        return cleaned
