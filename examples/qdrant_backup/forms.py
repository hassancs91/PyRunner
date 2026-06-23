"""
Config form for the Qdrant Backup plugin.

Plain ``forms.Form`` (no model) — the plugin persists everything through the SDK
(owner-scoped Secrets + an owned DataStore), so there is nothing to ModelForm.
Inputs reuse the console's token classes so the page matches the rest of PyRunner.
"""

from django import forms

# Console input styling (kept in sync with core/forms.py:INPUT_CLASS).
INPUT_CLASS = (
    "w-full px-3.5 py-2.5 bg-ink border border-line rounded-lg text-text text-sm "
    "placeholder-faint/60 focus:outline-none focus:ring-2 focus:ring-ok/30 "
    "focus:border-ok/60 transition-colors"
)

# form-field name -> the clean env-var the secret injects under.
SECRET_FIELDS = {
    "qdrant_api_key": "QDRANT_API_KEY",
    "r2_access_key_id": "R2_ACCESS_KEY_ID",
    "r2_secret_access_key": "R2_SECRET_ACCESS_KEY",
}


def _text(**kw):
    return forms.CharField(widget=forms.TextInput(attrs={"class": INPUT_CLASS}), **kw)


def _secret(**kw):
    # autocomplete off so browsers don't prefill the credential boxes
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


class QdrantBackupConfigForm(forms.Form):
    # ---- Non-secret connection config (visible / editable) ----
    qdrant_url = forms.URLField(
        label="Qdrant URL",
        widget=forms.URLInput(attrs={"class": INPUT_CLASS, "placeholder": "https://qdrant.example.com"}),
    )
    r2_endpoint_url = forms.URLField(
        label="R2 endpoint URL",
        widget=forms.URLInput(
            attrs={"class": INPUT_CLASS, "placeholder": "https://<account_id>.r2.cloudflarestorage.com"}
        ),
    )
    r2_bucket_name = _text(label="R2 bucket name")

    # ---- The 3 sensitive credentials (write-only; blank = keep existing) ----
    qdrant_api_key = _secret(label="Qdrant API key", required=False)
    r2_access_key_id = _secret(label="R2 access key ID", required=False)
    r2_secret_access_key = _secret(label="R2 secret access key", required=False)

    # ---- Backup behavior ----
    retention_days = forms.IntegerField(
        label="Retention (days)", min_value=1, max_value=3650, initial=30,
        widget=forms.NumberInput(attrs={"class": INPUT_CLASS}),
    )
    backup_prefix = _text(
        label="R2 folder prefix", initial="qdrant-backups", required=False,
    )
    keep_zip = forms.BooleanField(
        label="Also store a combined .zip per backup",
        required=False,
        help_text="Bundles all of a run's snapshots into one downloadable archive "
        "(roughly doubles R2 storage). Per-collection downloads work either way.",
        widget=forms.CheckboxInput(attrs={"class": "h-4 w-4 accent-ok align-middle"}),
    )

    # ---- Environment (must have requests + boto3) ----
    environment = _select([], label="Environment")

    # ---- Alerts (PyRunner's built-in notifications) ----
    notify_on = _select(
        [("failure", "On failure"), ("both", "On success and failure"), ("never", "Never")],
        label="Email me", initial="failure",
    )
    notify_email = forms.EmailField(
        label="Notify email", required=False,
        widget=forms.EmailInput(
            attrs={"class": INPUT_CLASS, "placeholder": "defaults to the global notification email"}
        ),
    )

    # ---- Schedule ----
    schedule_mode = _select(
        [("manual", "Manual only"), ("daily", "Daily"), ("weekly", "Weekly"), ("interval", "Every…")],
        label="Schedule", initial="daily",
    )
    schedule_time = forms.CharField(
        label="Time (HH:MM)", required=False, initial="02:00",
        widget=forms.TextInput(attrs={"class": INPUT_CLASS, "placeholder": "02:00"}),
    )
    schedule_weekday = _select(
        [("0", "Monday"), ("1", "Tuesday"), ("2", "Wednesday"), ("3", "Thursday"),
         ("4", "Friday"), ("5", "Saturday"), ("6", "Sunday")],
        label="Day of week", required=False, initial="0",
    )
    schedule_interval = _select(
        [("60", "Every hour"), ("120", "Every 2 hours"), ("360", "Every 6 hours"),
         ("720", "Every 12 hours")],
        label="Interval", required=False, initial="360",
    )
    timezone = _text(label="Timezone", initial="UTC", required=False)

    def __init__(self, *args, environments=None, configured_secrets=None, **kwargs):
        super().__init__(*args, **kwargs)
        # Populate the environment dropdown from the SDK's read-only list.
        env_names = [e.name for e in (environments or [])]
        self.fields["environment"].choices = [(n, n) for n in env_names]
        if not env_names:
            self.fields["environment"].required = False

        # A credential is required only on first setup — once stored, blank keeps it.
        configured = configured_secrets or set()
        for field_name, env_key in SECRET_FIELDS.items():
            already = env_key in configured
            self.fields[field_name].required = not already
            if already:
                self.fields[field_name].widget.attrs["placeholder"] = "configured — leave blank to keep"

    def clean_timezone(self):
        return self.cleaned_data.get("timezone") or "UTC"

    def clean_backup_prefix(self):
        return (self.cleaned_data.get("backup_prefix") or "qdrant-backups").strip("/") or "qdrant-backups"

    def clean(self):
        cleaned = super().clean()
        mode = cleaned.get("schedule_mode")

        if mode in ("daily", "weekly") and not cleaned.get("schedule_time"):
            self.add_error("schedule_time", "Pick a time for a daily/weekly schedule.")
        if mode == "weekly" and cleaned.get("schedule_weekday") in (None, ""):
            self.add_error("schedule_weekday", "Pick a day for a weekly schedule.")
        if mode == "interval" and not cleaned.get("schedule_interval"):
            self.add_error("schedule_interval", "Pick an interval.")

        # Basic HH:MM sanity for time-based schedules.
        t = cleaned.get("schedule_time")
        if mode in ("daily", "weekly") and t:
            parts = t.split(":")
            ok = len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit() \
                and 0 <= int(parts[0]) <= 23 and 0 <= int(parts[1]) <= 59
            if not ok:
                self.add_error("schedule_time", "Use 24-hour HH:MM, e.g. 02:00.")
        return cleaned
