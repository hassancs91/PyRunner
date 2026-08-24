"""
Forms for the core app.
"""
import calendar
import re
from zoneinfo import available_timezones

from django import forms
from django.utils.text import slugify

from core.models import Script, Environment, ScriptSchedule, Tag, DataStore, DataStoreEntry, APIToken, Database, Library, Secret, SecretProvider
from core.services import EnvironmentService


# Regex pattern for secret key validation (uppercase, numbers, underscores, must start with letter)
SECRET_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")


# Shared form-control classes (console design tokens — theme-aware via CSS variables)
INPUT_CLASS = (
    "w-full px-3.5 py-2.5 bg-ink border border-line rounded-lg text-text text-sm "
    "placeholder-faint/60 focus:outline-none focus:ring-2 focus:ring-ok/30 "
    "focus:border-ok/60 transition-colors"
)
CHECK_CLASS = "w-4 h-4 rounded text-ok bg-ink border-line focus:ring-ok/40 focus:ring-2"
FILE_CLASS = (
    "block w-full text-sm text-muted file:mr-3 file:py-2 file:px-3.5 file:rounded-lg "
    "file:border-0 file:text-sm file:font-medium file:bg-ok file:text-ink "
    "hover:file:opacity-90 cursor-pointer"
)


class PluginUploadForm(forms.Form):
    """Upload a plugin packaged as a ``.zip`` (superuser only)."""

    plugin_file = forms.FileField(
        label="Plugin .zip",
        help_text="A .zip whose single top-level folder is the plugin and contains "
        "plugin.json (slug, name, version).",
        widget=forms.ClearableFileInput(attrs={"class": FILE_CLASS, "accept": ".zip"}),
    )

    def clean_plugin_file(self):
        f = self.cleaned_data["plugin_file"]
        if not (f.name or "").lower().endswith(".zip"):
            raise forms.ValidationError("Plugin must be a .zip file.")
        return f


# Common timezone choices (sorted, common ones first)
COMMON_TIMEZONES = [
    "UTC",
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
    "Europe/London",
    "Europe/Paris",
    "Europe/Berlin",
    "Asia/Tokyo",
    "Asia/Shanghai",
    "Asia/Singapore",
    "Australia/Sydney",
]


def get_timezone_choices():
    """Timezone choices grouped Common/All.

    Optgroups (not a fake separator row) so the visual divider is not a
    submittable value — a plain separator choice used to validate and could be
    stored as timezone="---".
    """
    all_tz = sorted(available_timezones())
    common = [(tz, tz) for tz in COMMON_TIMEZONES if tz in all_tz]
    others = [(tz, tz) for tz in all_tz if tz not in COMMON_TIMEZONES]
    return [("Common", common), ("All timezones", others)]


class ScriptForm(forms.ModelForm):
    """Form for creating and editing scripts."""

    class Meta:
        model = Script
        fields = [
            "name",
            "description",
            "code",
            "environment",
            "tags",
            "timeout_seconds",
            "is_enabled",
            "isolation_mode",
            "injection_mode",
            "notify_on",
            "notify_email",
            "notify_webhook_url",
            "notify_webhook_enabled",
            "notify_channels",
        ]
        widgets = {
            "name": forms.TextInput(
                attrs={"class": INPUT_CLASS, "placeholder": "My script name"}
            ),
            "description": forms.Textarea(
                attrs={
                    "class": INPUT_CLASS,
                    "rows": 2,
                    "placeholder": "What does this script do?",
                }
            ),
            "code": forms.Textarea(
                attrs={
                    "class": INPUT_CLASS + " font-mono",
                    "rows": 15,
                    "placeholder": '# Your Python code here\nprint("Hello, World!")',
                }
            ),
            "environment": forms.Select(attrs={"class": INPUT_CLASS}),
            "tags": forms.CheckboxSelectMultiple(attrs={"class": "tag-checkbox"}),
            "timeout_seconds": forms.NumberInput(
                attrs={"class": INPUT_CLASS, "min": 1, "max": 86400}
            ),
            "is_enabled": forms.CheckboxInput(attrs={"class": CHECK_CLASS}),
            "isolation_mode": forms.Select(attrs={"class": INPUT_CLASS}),
            "injection_mode": forms.Select(attrs={"class": INPUT_CLASS}),
            "notify_on": forms.Select(attrs={"class": INPUT_CLASS}),
            "notify_email": forms.EmailInput(
                attrs={
                    "class": INPUT_CLASS,
                    "placeholder": "Override default email (optional)",
                }
            ),
            "notify_webhook_url": forms.URLInput(
                attrs={
                    "class": INPUT_CLASS,
                    "placeholder": "https://your-service.com/webhook",
                }
            ),
            "notify_webhook_enabled": forms.CheckboxInput(attrs={"class": CHECK_CLASS}),
            "notify_channels": forms.CheckboxSelectMultiple(attrs={"class": CHECK_CLASS}),
        }
        labels = {
            "name": "Script Name",
            "description": "Description",
            "code": "Python Code",
            "environment": "Environment",
            "tags": "Tags",
            "timeout_seconds": "Timeout (seconds)",
            "is_enabled": "Enabled",
            "isolation_mode": "Execution Isolation",
            "injection_mode": "Secret injection",
            "notify_on": "Notify On",
            "notify_email": "Notification Email",
            "notify_webhook_url": "Webhook URL",
            "notify_webhook_enabled": "Enable Webhook",
            "notify_channels": "Notify channels",
        }
        help_texts = {
            "timeout_seconds": "Maximum execution time (1 second to 24 hours)",
            "isolation_mode": "Run sandboxed. Effective only when the workspace policy is 'optional' (a 'required' workspace always sandboxes).",
            "injection_mode": "All = every workspace secret (default). Selected = only the secrets you attach below.",
            "notify_email": "Leave empty to use global default",
            "notify_webhook_url": "URL to POST notifications to when script completes",
        }

    def __init__(self, *args, workspace=None, **kwargs):
        super().__init__(*args, **kwargs)
        # Only show active environments
        self.fields["environment"].queryset = Environment.objects.filter(is_active=True)
        # Preselect the default environment on the create form (the field is
        # required with no model default, so it otherwise renders on the empty
        # "---------" choice even though a default environment exists).
        # (instance pk can't distinguish create from edit here — Script's UUID
        # pk is filled at instantiation — so use _state.adding.)
        if (
            not self.is_bound
            and self.instance._state.adding
            and "environment" not in self.initial
        ):
            default_env = Environment.objects.filter(
                is_active=True, is_default=True
            ).first()
            if default_env:
                self.initial["environment"] = default_env.pk
        # Optional: a form submitted without it (or legacy callers) defaults to
        # 'all' in clean_injection_mode, matching the model default.
        self.fields["injection_mode"].required = False
        # Scope the notify-channels picker to the active workspace (tenancy);
        # None ⇒ empty so a channel never leaks across workspaces in the picker.
        from core.models import Channel

        self.fields["notify_channels"].required = False
        self.fields["notify_channels"].queryset = (
            Channel.objects.for_workspace(workspace)
            if workspace is not None
            else Channel.objects.none()
        )

    def clean_injection_mode(self):
        return self.cleaned_data.get("injection_mode") or Script.InjectionMode.ALL

    def clean_code(self):
        code = self.cleaned_data.get("code", "").strip()
        if not code:
            raise forms.ValidationError("Script code cannot be empty.")
        return code

    def clean_timeout_seconds(self):
        timeout = self.cleaned_data.get("timeout_seconds")
        if timeout is not None and (timeout < 1 or timeout > 86400):
            raise forms.ValidationError("Timeout must be between 1 and 86400 seconds (24 hours).")
        return timeout


class TagForm(forms.ModelForm):
    """Form for creating and editing tags."""

    class Meta:
        model = Tag
        fields = ["name", "color"]
        widgets = {
            "name": forms.TextInput(
                attrs={
                    "class": INPUT_CLASS,
                    "placeholder": "Tag name",
                }
            ),
            "color": forms.Select(
                attrs={
                    "class": INPUT_CLASS,
                }
            ),
        }
        labels = {
            "name": "Tag Name",
            "color": "Color",
        }

    def clean_name(self):
        name = self.cleaned_data.get("name", "").strip()
        if not name:
            raise forms.ValidationError("Tag name is required.")
        # Check uniqueness (excluding current instance for edits)
        qs = Tag.objects.filter(name__iexact=name)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError("A tag with this name already exists.")
        return name


class ScheduleForm(forms.ModelForm):
    """Form for configuring script schedules."""

    WEEKDAY_CHOICES = [
        (0, "Monday"),
        (1, "Tuesday"),
        (2, "Wednesday"),
        (3, "Thursday"),
        (4, "Friday"),
        (5, "Saturday"),
        (6, "Sunday"),
    ]

    MONTHDAY_CHOICES = [(i, str(i)) for i in range(1, 32)]
    MONTH_CHOICES = [(i, calendar.month_name[i]) for i in range(1, 13)]

    # Custom field for daily times (comma-separated input)
    daily_times_input = forms.CharField(
        required=False,
        widget=forms.TextInput(
            attrs={"class": INPUT_CLASS, "placeholder": "09:00, 18:00"}
        ),
        label="Run Times",
        help_text="Comma-separated times in HH:MM format (24-hour)",
    )

    # Weekly mode fields
    weekly_days_input = forms.MultipleChoiceField(
        required=False,
        choices=WEEKDAY_CHOICES,
        widget=forms.CheckboxSelectMultiple(
            attrs={
                "class": "sr-only peer",
            }
        ),
        label="Days of Week",
    )

    weekly_times_input = forms.CharField(
        required=False,
        widget=forms.TextInput(
            attrs={"class": INPUT_CLASS, "placeholder": "09:00, 18:00"}
        ),
        label="Run Times",
        help_text="Comma-separated times in HH:MM format (24-hour)",
    )

    # Monthly mode fields
    monthly_days_input = forms.MultipleChoiceField(
        required=False,
        choices=MONTHDAY_CHOICES,
        widget=forms.CheckboxSelectMultiple(
            attrs={
                "class": "sr-only peer",
            }
        ),
        label="Days of Month",
    )

    monthly_times_input = forms.CharField(
        required=False,
        widget=forms.TextInput(
            attrs={"class": INPUT_CLASS, "placeholder": "09:00, 18:00"}
        ),
        label="Run Times",
        help_text="Comma-separated times in HH:MM format (24-hour)",
    )

    yearly_month = forms.TypedChoiceField(
        required=False, choices=MONTH_CHOICES, coerce=int,
        widget=forms.Select(attrs={"class": INPUT_CLASS}), label="Month",
    )
    yearly_day = forms.TypedChoiceField(
        required=False, choices=MONTHDAY_CHOICES, coerce=int,
        widget=forms.Select(attrs={"class": INPUT_CLASS}), label="Day",
    )
    yearly_time = forms.CharField(
        required=False,
        widget=forms.TimeInput(
            attrs={"class": INPUT_CLASS, "type": "time"}, format="%H:%M"
        ),
        label="Run time", help_text="24-hour format (HH:MM)",
    )

    timezone = forms.ChoiceField(
        choices=get_timezone_choices,
        initial="UTC",
        widget=forms.Select(attrs={"class": INPUT_CLASS}),
    )

    class Meta:
        model = ScriptSchedule
        fields = ["run_mode", "interval_minutes", "timezone", "is_active"]
        widgets = {
            "run_mode": forms.RadioSelect(
                attrs={
                    "class": "sr-only peer",
                }
            ),
            "interval_minutes": forms.Select(attrs={"class": INPUT_CLASS}),
            "is_active": forms.CheckboxInput(attrs={"class": CHECK_CLASS}),
        }
        labels = {
            "run_mode": "Run Mode",
            "interval_minutes": "Interval",
            "is_active": "Schedule Active",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Populate daily_times_input from instance
        if self.instance and self.instance.pk and self.instance.daily_times:
            self.fields["daily_times_input"].initial = ", ".join(
                self.instance.daily_times
            )

        # Populate weekly fields from instance
        if self.instance and self.instance.pk:
            if self.instance.weekly_days:
                self.fields["weekly_days_input"].initial = [
                    str(d) for d in self.instance.weekly_days
                ]
            if self.instance.weekly_times:
                self.fields["weekly_times_input"].initial = ", ".join(
                    self.instance.weekly_times
                )

        # Populate monthly fields from instance
        if self.instance and self.instance.pk:
            if self.instance.monthly_days:
                self.fields["monthly_days_input"].initial = [
                    str(d) for d in self.instance.monthly_days
                ]
            if self.instance.monthly_times:
                self.fields["monthly_times_input"].initial = ", ".join(
                    self.instance.monthly_times
                )
            self.fields["yearly_month"].initial = self.instance.yearly_month
            self.fields["yearly_day"].initial = self.instance.yearly_day
            self.fields["yearly_time"].initial = self.instance.yearly_time

    def _parse_times(self, value):
        """Parse and validate comma-separated times input."""
        if not value:
            return []

        value = value.strip()
        if not value:
            return []

        times = []
        for time_str in value.split(","):
            time_str = time_str.strip()
            if not time_str:
                continue

            # Validate HH:MM format
            try:
                parts = time_str.split(":")
                if len(parts) != 2:
                    raise ValueError
                hour, minute = int(parts[0]), int(parts[1])
                if not (0 <= hour <= 23 and 0 <= minute <= 59):
                    raise ValueError
                times.append(f"{hour:02d}:{minute:02d}")
            except ValueError:
                raise forms.ValidationError(
                    f"Invalid time format: '{time_str}'. Use HH:MM (e.g., 09:00)"
                )

        return times

    def clean_daily_times_input(self):
        """Parse and validate daily times input."""
        return self._parse_times(self.cleaned_data.get("daily_times_input", ""))

    def clean_weekly_times_input(self):
        """Parse and validate weekly times input."""
        return self._parse_times(self.cleaned_data.get("weekly_times_input", ""))

    def clean_monthly_times_input(self):
        """Parse and validate monthly times input."""
        return self._parse_times(self.cleaned_data.get("monthly_times_input", ""))

    def clean_yearly_time(self):
        value = self.cleaned_data.get("yearly_time")
        if not value:
            return ""
        times = self._parse_times(value)
        if len(times) != 1:
            raise forms.ValidationError("Enter one time in HH:MM format.")
        return times[0]

    def clean(self):
        cleaned_data = super().clean()
        run_mode = cleaned_data.get("run_mode")

        if run_mode == ScriptSchedule.RunMode.INTERVAL:
            if not cleaned_data.get("interval_minutes"):
                self.add_error(
                    "interval_minutes", "Interval is required for interval mode."
                )

        elif run_mode == ScriptSchedule.RunMode.DAILY:
            daily_times = cleaned_data.get("daily_times_input", [])
            if not daily_times:
                self.add_error(
                    "daily_times_input",
                    "At least one time is required for daily mode.",
                )

        elif run_mode == ScriptSchedule.RunMode.WEEKLY:
            weekly_days = cleaned_data.get("weekly_days_input", [])
            weekly_times = cleaned_data.get("weekly_times_input", [])
            if not weekly_days:
                self.add_error(
                    "weekly_days_input",
                    "At least one day is required for weekly mode.",
                )
            if not weekly_times:
                self.add_error(
                    "weekly_times_input",
                    "At least one time is required for weekly mode.",
                )

        elif run_mode == ScriptSchedule.RunMode.MONTHLY:
            monthly_days = cleaned_data.get("monthly_days_input", [])
            monthly_times = cleaned_data.get("monthly_times_input", [])
            if not monthly_days:
                self.add_error(
                    "monthly_days_input",
                    "At least one day is required for monthly mode.",
                )
            if not monthly_times:
                self.add_error(
                    "monthly_times_input",
                    "At least one time is required for monthly mode.",
                )

        elif run_mode == ScriptSchedule.RunMode.YEARLY:
            month = cleaned_data.get("yearly_month")
            day = cleaned_data.get("yearly_day")
            if not month:
                self.add_error("yearly_month", "Month is required for yearly mode.")
            if not day:
                self.add_error("yearly_day", "Day is required for yearly mode.")
            elif month and day > calendar.monthrange(2024, month)[1]:
                self.add_error("yearly_day", "That day does not exist in the selected month.")
            if not cleaned_data.get("yearly_time"):
                self.add_error("yearly_time", "Time is required for yearly mode.")

        return cleaned_data

    def save(self, commit=True):
        instance = super().save(commit=False)

        # Set daily_times from parsed input
        instance.daily_times = self.cleaned_data.get("daily_times_input", [])

        # Set weekly fields from parsed input
        weekly_days = self.cleaned_data.get("weekly_days_input", [])
        instance.weekly_days = [int(d) for d in weekly_days] if weekly_days else []
        instance.weekly_times = self.cleaned_data.get("weekly_times_input", [])

        # Set monthly fields from parsed input
        monthly_days = self.cleaned_data.get("monthly_days_input", [])
        instance.monthly_days = [int(d) for d in monthly_days] if monthly_days else []
        instance.monthly_times = self.cleaned_data.get("monthly_times_input", [])
        instance.yearly_month = self.cleaned_data.get("yearly_month") or None
        instance.yearly_day = self.cleaned_data.get("yearly_day") or None
        instance.yearly_time = self.cleaned_data.get("yearly_time", "")

        if commit:
            instance.save()

        return instance


class EnvironmentCreateForm(forms.ModelForm):
    """Form for creating a new environment."""

    python_path = forms.ChoiceField(
        choices=[],
        widget=forms.Select(attrs={"class": INPUT_CLASS}),
        label="Python Version",
        help_text="Select Python installation to use for this environment",
    )

    class Meta:
        model = Environment
        fields = ["name", "description"]
        widgets = {
            "name": forms.TextInput(
                attrs={"class": INPUT_CLASS, "placeholder": "My environment"}
            ),
            "description": forms.Textarea(
                attrs={
                    "class": INPUT_CLASS,
                    "rows": 2,
                    "placeholder": "Environment description (optional)",
                }
            ),
        }
        labels = {
            "name": "Environment Name",
            "description": "Description",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Populate python_path choices from discovered Python installations
        pythons = EnvironmentService.discover_python_versions()
        choices = [(p["path"], p["display"]) for p in pythons]
        if not choices:
            choices = [("", "No Python installations found")]
        self.fields["python_path"].choices = choices

    def clean_name(self):
        """Validate name and check for path uniqueness."""
        name = self.cleaned_data.get("name", "").strip()
        if not name:
            raise forms.ValidationError("Environment name is required.")

        # Generate path from slugified name
        base_path = slugify(name)
        if not base_path:
            base_path = "environment"

        # Ensure path is unique
        path = base_path
        counter = 1
        while Environment.objects.filter(path=path).exists():
            path = f"{base_path}-{counter}"
            counter += 1

        # Store generated path for use in view
        self._generated_path = path
        return name

    def get_generated_path(self) -> str:
        """Return the generated path after validation."""
        return getattr(self, "_generated_path", slugify(self.cleaned_data.get("name", "env")))


class EnvironmentEditForm(forms.ModelForm):
    """Form for editing environment details (name/description only)."""

    class Meta:
        model = Environment
        fields = ["name", "description"]
        widgets = {
            "name": forms.TextInput(
                attrs={"class": INPUT_CLASS, "placeholder": "My environment"}
            ),
            "description": forms.Textarea(
                attrs={
                    "class": INPUT_CLASS,
                    "rows": 2,
                    "placeholder": "Environment description (optional)",
                }
            ),
        }
        labels = {
            "name": "Environment Name",
            "description": "Description",
        }


class PackageInstallForm(forms.Form):
    """Form for installing a single package."""

    package_spec = forms.CharField(
        max_length=200,
        widget=forms.TextInput(
            attrs={"class": INPUT_CLASS + " font-mono", "placeholder": "requests==2.31.0"}
        ),
        label="Package",
        help_text="Package name with optional version (e.g., requests, django>=4.0)",
    )

    def clean_package_spec(self):
        spec = self.cleaned_data.get("package_spec", "").strip()
        if not spec:
            raise forms.ValidationError("Package specification is required.")
        if not EnvironmentService.validate_package_spec(spec):
            raise forms.ValidationError(
                "Invalid package specification. Use format: package or package==version"
            )
        return spec


class BulkInstallForm(forms.Form):
    """Form for bulk package installation from requirements."""

    requirements = forms.CharField(
        widget=forms.Textarea(
            attrs={
                "class": INPUT_CLASS + " font-mono",
                "rows": 10,
                "placeholder": "requests==2.31.0\ndjango>=4.0\nnumpy",
            }
        ),
        label="Requirements",
        help_text="Paste requirements.txt content (one package per line)",
        required=False,
    )

    requirements_file = forms.FileField(
        required=False,
        widget=forms.FileInput(attrs={"class": FILE_CLASS, "accept": ".txt"}),
        label="Or upload requirements.txt",
    )

    def clean(self):
        cleaned_data = super().clean()
        text = cleaned_data.get("requirements", "").strip()
        file = cleaned_data.get("requirements_file")

        if not text and not file:
            raise forms.ValidationError(
                "Provide requirements text or upload a file."
            )

        # If file provided, read its content
        if file:
            try:
                content = file.read().decode("utf-8")
                cleaned_data["requirements"] = content
            except UnicodeDecodeError:
                raise forms.ValidationError(
                    "Could not read file. Ensure it's a valid text file."
                )

        # Validate each line
        requirements_text = cleaned_data.get("requirements", "")
        for line in requirements_text.splitlines():
            line = line.strip()
            # Skip empty lines and comments
            if not line or line.startswith("#"):
                continue
            # Reject pip option lines (leading "-", e.g. --index-url /
            # --extra-index-url): pip honours them inside a requirements file,
            # so they can redirect installs to an attacker-controlled index
            # (Vuln 6). Only comments and package specs are allowed.
            if line.startswith("-"):
                raise forms.ValidationError(
                    f"Option lines (e.g. --index-url) are not allowed: {line}"
                )
            # Extract package spec (first word before any whitespace)
            pkg_spec = line.split()[0] if line.split() else ""
            if pkg_spec and not EnvironmentService.validate_package_spec(pkg_spec):
                raise forms.ValidationError(f"Invalid package specification: {line}")

        return cleaned_data


class SecretCreateForm(forms.Form):
    """Form for creating a new secret.

    Value source (External Secret Providers): ``source="local"`` stores an
    encrypted ``value`` (today's path); ``source="external"`` stores a reference
    (``provider`` + ``external_ref``) resolved live at run time. The value/provider
    fields are conditionally required in ``clean()`` on the chosen source.
    """

    def __init__(self, *args, workspace=None, **kwargs):
        # The active workspace scopes the key-uniqueness check (tenancy Stage 3:
        # secret keys are unique per workspace, not globally).
        self._workspace = workspace
        super().__init__(*args, **kwargs)
        # Set the provider queryset here (not at class scope) to avoid a DB hit at
        # import time, and to keep the dropdown fresh per request.
        self.fields["provider"].queryset = SecretProvider.objects.all().order_by("name")

    key = forms.CharField(
        max_length=100,
        widget=forms.TextInput(
            attrs={
                "class": INPUT_CLASS + " font-mono uppercase",
                "placeholder": "API_KEY",
                "autocomplete": "off",
            }
        ),
        label="Key Name",
        help_text="Uppercase letters, numbers, and underscores only. Must start with a letter.",
    )

    # required=False so a POST that omits it means today's local secret — keeps
    # every existing caller (and existing test) byte-for-byte; clean() normalizes.
    source = forms.ChoiceField(
        choices=Secret.Source.choices,
        initial=Secret.Source.LOCAL,
        required=False,
        widget=forms.Select(attrs={"class": INPUT_CLASS}),
        label="Value source",
    )

    value = forms.CharField(
        required=False,
        widget=forms.Textarea(
            attrs={
                "class": INPUT_CLASS + " font-mono",
                "rows": 3,
                "placeholder": "sk-your-secret-value-here",
                "autocomplete": "off",
            }
        ),
        label="Secret Value",
        help_text="The secret value (will be encrypted at rest)",
    )

    provider = forms.ModelChoiceField(
        queryset=SecretProvider.objects.none(),
        required=False,
        empty_label="— select a provider —",
        widget=forms.Select(attrs={"class": INPUT_CLASS}),
        label="Provider",
    )

    external_ref = forms.CharField(
        required=False,
        max_length=500,
        widget=forms.TextInput(
            attrs={
                "class": INPUT_CLASS + " font-mono",
                "placeholder": "path/to/secret#key",
                "autocomplete": "off",
            }
        ),
        label="Reference",
        help_text="Reference to the value within the provider (e.g. a Vault path#key).",
    )

    description = forms.CharField(
        required=False,
        widget=forms.Textarea(
            attrs={
                "class": INPUT_CLASS,
                "rows": 2,
                "placeholder": "What is this secret used for?",
            }
        ),
        label="Description",
        help_text="Optional description to help remember what this secret is for",
    )

    def clean_external_ref(self):
        return (self.cleaned_data.get("external_ref") or "").strip()

    def clean(self):
        cleaned = super().clean()
        source = cleaned.get("source") or Secret.Source.LOCAL
        cleaned["source"] = source  # normalize blank → local for the view
        if source == Secret.Source.EXTERNAL:
            if not cleaned.get("provider"):
                self.add_error("provider", "Select a provider for an external secret.")
            if not cleaned.get("external_ref"):
                self.add_error(
                    "external_ref", "A reference is required for an external secret."
                )
        else:  # local
            if not cleaned.get("value"):
                self.add_error("value", "Secret value is required.")
        return cleaned

    def clean_key(self):
        """Validate and normalize the key."""
        key = self.cleaned_data.get("key", "").strip().upper()

        if not key:
            raise forms.ValidationError("Key name is required.")

        if not SECRET_KEY_PATTERN.match(key):
            raise forms.ValidationError(
                "Key must start with a letter and contain only uppercase letters, numbers, and underscores."
            )

        # Check for reserved environment variable names
        reserved = {
            "PATH",
            "HOME",
            "USER",
            "SHELL",
            "PWD",
            "PYTHONPATH",
            "VIRTUAL_ENV",
            "PYTHONHOME",
            "PYTHONDONTWRITEBYTECODE",
            "PYTHONUNBUFFERED",
        }
        if key in reserved:
            raise forms.ValidationError(
                f"'{key}' is a reserved environment variable name."
            )

        # Check if key already exists within the active workspace (keys are
        # per-workspace, so two workspaces can each own an API_KEY).
        from core.models import Secret

        qs = Secret.objects.filter(key=key)
        if self._workspace is not None:
            qs = qs.filter(workspace=self._workspace)
        if qs.exists():
            raise forms.ValidationError(
                f"A secret with key '{key}' already exists in this workspace."
            )

        return key

    def clean_value(self):
        """Length-check only; the required-ness of the value depends on the chosen
        source and is enforced in ``clean()`` (local needs a value; external does not)."""
        value = self.cleaned_data.get("value", "")

        # Reasonable max length for secrets
        if len(value) > 10000:
            raise forms.ValidationError(
                "Secret value is too long (max 10,000 characters)."
            )

        return value


class SecretEditForm(forms.Form):
    """Form for editing an existing secret (value/source and description).

    Supports switching a secret's value source (External Secret Providers). A
    local edit may leave ``value`` blank to keep the stored value; switching to
    (or staying) external requires ``provider`` + ``external_ref``.
    """

    def __init__(self, *args, instance=None, **kwargs):
        self._instance = instance
        super().__init__(*args, **kwargs)
        self.fields["provider"].queryset = SecretProvider.objects.all().order_by("name")
        if instance is not None and not self.is_bound:
            self.fields["source"].initial = instance.source
            self.fields["provider"].initial = instance.provider_id
            self.fields["external_ref"].initial = instance.external_ref
            self.fields["description"].initial = instance.description

    # required=False so a POST that omits it means today's local secret — keeps
    # every existing caller (and existing test) byte-for-byte; clean() normalizes.
    source = forms.ChoiceField(
        choices=Secret.Source.choices,
        initial=Secret.Source.LOCAL,
        required=False,
        widget=forms.Select(attrs={"class": INPUT_CLASS}),
        label="Value source",
    )

    value = forms.CharField(
        required=False,
        widget=forms.Textarea(
            attrs={
                "class": INPUT_CLASS + " font-mono",
                "rows": 3,
                "placeholder": "Leave blank to keep current value",
                "autocomplete": "off",
            }
        ),
        label="New Secret Value",
        help_text="Leave blank to keep the current value",
    )

    provider = forms.ModelChoiceField(
        queryset=SecretProvider.objects.none(),
        required=False,
        empty_label="— select a provider —",
        widget=forms.Select(attrs={"class": INPUT_CLASS}),
        label="Provider",
    )

    external_ref = forms.CharField(
        required=False,
        max_length=500,
        widget=forms.TextInput(
            attrs={
                "class": INPUT_CLASS + " font-mono",
                "placeholder": "path/to/secret#key",
                "autocomplete": "off",
            }
        ),
        label="Reference",
        help_text="Reference to the value within the provider (e.g. a Vault path#key).",
    )

    description = forms.CharField(
        required=False,
        widget=forms.Textarea(
            attrs={
                "class": INPUT_CLASS,
                "rows": 2,
                "placeholder": "What is this secret used for?",
            }
        ),
        label="Description",
    )

    def clean_external_ref(self):
        return (self.cleaned_data.get("external_ref") or "").strip()

    def clean_value(self):
        """Validate the secret value if provided."""
        value = self.cleaned_data.get("value", "")

        if value and len(value) > 10000:
            raise forms.ValidationError(
                "Secret value is too long (max 10,000 characters)."
            )

        return value

    def clean(self):
        cleaned = super().clean()
        source = cleaned.get("source") or Secret.Source.LOCAL
        cleaned["source"] = source  # normalize blank → local for the view
        if source == Secret.Source.EXTERNAL:
            if not cleaned.get("provider"):
                self.add_error("provider", "Select a provider for an external secret.")
            if not cleaned.get("external_ref"):
                self.add_error(
                    "external_ref", "A reference is required for an external secret."
                )
        else:  # local
            # A value may be omitted only when the row already holds a stored local
            # value to keep; switching from external → local must supply one.
            has_stored_local = bool(
                self._instance
                and self._instance.source == Secret.Source.LOCAL
                and self._instance.encrypted_value
            )
            if not cleaned.get("value") and not has_stored_local:
                self.add_error("value", "Secret value is required.")
        return cleaned


class NotificationSettingsForm(forms.Form):
    """Form for global notification settings."""

    from core.models import GlobalSettings

    EMAIL_BACKEND_CHOICES = [
        (GlobalSettings.EmailBackend.DISABLED, "Disabled"),
        (GlobalSettings.EmailBackend.SMTP, "SMTP"),
        (GlobalSettings.EmailBackend.RESEND, "Resend API"),
    ]

    email_backend = forms.ChoiceField(
        choices=EMAIL_BACKEND_CHOICES,
        initial=GlobalSettings.EmailBackend.DISABLED,
        widget=forms.RadioSelect(
            attrs={
                "class": "sr-only peer",
            }
        ),
        label="Email Backend",
    )

    # SMTP Configuration
    smtp_host = forms.CharField(
        required=False,
        max_length=255,
        widget=forms.TextInput(
            attrs={
                "class": INPUT_CLASS,
                "placeholder": "smtp.example.com",
            }
        ),
        label="SMTP Host",
    )

    smtp_port = forms.IntegerField(
        required=False,
        initial=587,
        widget=forms.NumberInput(
            attrs={
                "class": INPUT_CLASS,
                "min": 1,
                "max": 65535,
            }
        ),
        label="SMTP Port",
    )

    smtp_username = forms.CharField(
        required=False,
        max_length=255,
        widget=forms.TextInput(
            attrs={
                "class": INPUT_CLASS,
                "placeholder": "username@example.com",
            }
        ),
        label="SMTP Username",
    )

    smtp_password = forms.CharField(
        required=False,
        widget=forms.PasswordInput(
            attrs={
                "class": INPUT_CLASS,
                "placeholder": "Leave blank to keep current",
                "autocomplete": "new-password",
            }
        ),
        label="SMTP Password",
    )

    smtp_use_tls = forms.BooleanField(
        required=False,
        initial=True,
        widget=forms.CheckboxInput(
            attrs={
                "class": CHECK_CLASS,
            }
        ),
        label="Use TLS",
    )

    smtp_from_email = forms.EmailField(
        required=False,
        widget=forms.EmailInput(
            attrs={
                "class": INPUT_CLASS,
                "placeholder": "noreply@example.com",
            }
        ),
        label="From Email",
    )

    # Resend Configuration
    resend_api_key = forms.CharField(
        required=False,
        widget=forms.PasswordInput(
            attrs={
                "class": INPUT_CLASS,
                "placeholder": "Leave blank to keep current",
                "autocomplete": "new-password",
            }
        ),
        label="Resend API Key",
    )

    resend_from_email = forms.EmailField(
        required=False,
        widget=forms.EmailInput(
            attrs={
                "class": INPUT_CLASS,
                "placeholder": "noreply@yourdomain.com",
            }
        ),
        label="From Email",
    )

    # Default notification email
    default_notification_email = forms.EmailField(
        required=False,
        widget=forms.EmailInput(
            attrs={
                "class": INPUT_CLASS,
                "placeholder": "notifications@example.com",
            }
        ),
        label="Default Notification Email",
        help_text="All script notifications will be sent here unless overridden per-script.",
    )

    def __init__(self, *args, instance=None, **kwargs):
        """Initialize form with existing settings."""
        super().__init__(*args, **kwargs)
        if instance:
            self.fields["email_backend"].initial = instance.email_backend
            self.fields["smtp_host"].initial = instance.smtp_host
            self.fields["smtp_port"].initial = instance.smtp_port
            self.fields["smtp_username"].initial = instance.smtp_username
            self.fields["smtp_use_tls"].initial = instance.smtp_use_tls
            self.fields["smtp_from_email"].initial = instance.smtp_from_email
            self.fields["resend_from_email"].initial = instance.resend_from_email
            self.fields["default_notification_email"].initial = instance.default_notification_email

    def clean(self):
        """Validate configuration based on selected backend."""
        cleaned_data = super().clean()
        backend = cleaned_data.get("email_backend")

        from core.models import GlobalSettings

        if backend == GlobalSettings.EmailBackend.SMTP:
            if not cleaned_data.get("smtp_host"):
                self.add_error("smtp_host", "SMTP host is required for SMTP backend.")
            if not cleaned_data.get("smtp_from_email"):
                self.add_error("smtp_from_email", "From email is required for SMTP backend.")

        elif backend == GlobalSettings.EmailBackend.RESEND:
            if not cleaned_data.get("resend_from_email"):
                self.add_error("resend_from_email", "From email is required for Resend backend.")

        return cleaned_data

    def save(self, instance):
        """Save the notification settings to the GlobalSettings instance."""
        from core.services import EncryptionService

        instance.email_backend = self.cleaned_data["email_backend"]
        instance.smtp_host = self.cleaned_data.get("smtp_host") or ""
        instance.smtp_port = self.cleaned_data.get("smtp_port") or 587
        instance.smtp_username = self.cleaned_data.get("smtp_username") or ""
        instance.smtp_use_tls = self.cleaned_data.get("smtp_use_tls", True)
        instance.smtp_from_email = self.cleaned_data.get("smtp_from_email") or ""
        instance.resend_from_email = self.cleaned_data.get("resend_from_email") or ""
        instance.default_notification_email = self.cleaned_data.get("default_notification_email") or ""

        # Encrypt and save SMTP password if provided
        smtp_password = self.cleaned_data.get("smtp_password")
        if smtp_password:
            instance.smtp_password_encrypted = EncryptionService.encrypt(smtp_password)

        # Encrypt and save Resend API key if provided
        resend_api_key = self.cleaned_data.get("resend_api_key")
        if resend_api_key:
            instance.resend_api_key_encrypted = EncryptionService.encrypt(resend_api_key)

        instance.save()
        return instance


class GeneralSettingsForm(forms.Form):
    """Form for general instance settings."""

    instance_name = forms.CharField(
        max_length=100,
        required=False,
        widget=forms.TextInput(
            attrs={
                "class": INPUT_CLASS,
                "placeholder": "PyRunner",
            }
        ),
        label="Instance Name",
        help_text="Displayed in the header and email notifications",
    )

    timezone = forms.ChoiceField(
        choices=get_timezone_choices,
        initial="UTC",
        widget=forms.Select(
            attrs={
                "class": INPUT_CLASS,
            }
        ),
        label="Timezone",
        help_text="Times in the console and scheduled backup times use this timezone",
    )

    admin_url_slug = forms.CharField(
        max_length=100,
        required=False,
        widget=forms.TextInput(
            attrs={
                "class": INPUT_CLASS,
                "placeholder": "django-admin",
            }
        ),
        label="Django Admin URL",
        help_text="URL path for Django admin (e.g., 'django-admin' → /django-admin/). Requires app restart.",
    )

    def clean_admin_url_slug(self):
        """Validate admin URL slug format."""
        import re
        slug = self.cleaned_data.get("admin_url_slug", "django-admin").strip().lower()
        if not slug:
            slug = "django-admin"
        # Remove leading/trailing slashes
        slug = slug.strip("/")
        # Validate: alphanumeric, hyphens, underscores only
        if not re.match(r"^[a-z0-9_-]+$", slug):
            raise forms.ValidationError(
                "Admin URL can only contain lowercase letters, numbers, hyphens, and underscores."
            )
        # Prevent conflicts with existing routes
        reserved = ["setup", "auth", "cpanel", "webhook", "static", "media"]
        if slug in reserved:
            raise forms.ValidationError(
                f"'{slug}' is a reserved URL path. Please choose a different name."
            )
        return slug

    def __init__(self, *args, instance=None, **kwargs):
        """Initialize form with existing settings."""
        super().__init__(*args, **kwargs)
        if instance:
            self.fields["instance_name"].initial = instance.instance_name
            self.fields["timezone"].initial = instance.timezone
            self.fields["admin_url_slug"].initial = instance.admin_url_slug

    def save(self, instance):
        """Save the general settings to the GlobalSettings instance."""
        instance.instance_name = self.cleaned_data.get("instance_name") or "PyRunner"
        instance.timezone = self.cleaned_data.get("timezone") or "UTC"
        instance.admin_url_slug = self.cleaned_data.get("admin_url_slug") or "django-admin"
        instance.save(update_fields=[
            "instance_name", "timezone", "admin_url_slug", "updated_at"
        ])
        return instance


class LogRetentionForm(forms.Form):
    """Form for log retention settings."""

    retention_days = forms.IntegerField(
        min_value=0,
        initial=0,
        widget=forms.NumberInput(
            attrs={
                "class": INPUT_CLASS,
                "min": 0,
            }
        ),
        label="Retention Days",
        help_text="Delete runs older than this many days (0 = keep forever)",
    )

    retention_count = forms.IntegerField(
        min_value=0,
        initial=0,
        widget=forms.NumberInput(
            attrs={
                "class": INPUT_CLASS,
                "min": 0,
            }
        ),
        label="Retention Count",
        help_text="Keep only the last N runs per script (0 = unlimited)",
    )

    auto_cleanup_enabled = forms.BooleanField(
        required=False,
        initial=False,
        widget=forms.CheckboxInput(
            attrs={
                "class": CHECK_CLASS,
            }
        ),
        label="Auto Cleanup",
        help_text="Automatically clean up old runs daily at 2 AM",
    )

    def __init__(self, *args, instance=None, **kwargs):
        """Initialize form with existing settings."""
        super().__init__(*args, **kwargs)
        if instance:
            self.fields["retention_days"].initial = instance.retention_days
            self.fields["retention_count"].initial = instance.retention_count
            self.fields["auto_cleanup_enabled"].initial = instance.auto_cleanup_enabled

    def save(self, instance):
        """Save the retention settings to the GlobalSettings instance."""
        from core.services import RetentionService

        instance.retention_days = self.cleaned_data.get("retention_days") or 0
        instance.retention_count = self.cleaned_data.get("retention_count") or 0

        # Handle auto cleanup schedule
        new_auto_cleanup = self.cleaned_data.get("auto_cleanup_enabled", False)
        old_auto_cleanup = instance.auto_cleanup_enabled

        instance.auto_cleanup_enabled = new_auto_cleanup
        instance.save(update_fields=[
            "retention_days", "retention_count", "auto_cleanup_enabled", "updated_at"
        ])

        # Manage the django-q2 schedule
        if new_auto_cleanup and not old_auto_cleanup:
            RetentionService.enable_auto_cleanup()
        elif not new_auto_cleanup and old_auto_cleanup:
            RetentionService.disable_auto_cleanup()

        return instance


class WorkerSettingsForm(forms.Form):
    """Form for Django-Q2 worker configuration."""

    q_workers = forms.IntegerField(
        min_value=1,
        max_value=16,
        initial=2,
        widget=forms.NumberInput(
            attrs={
                "class": INPUT_CLASS,
                "min": 1,
                "max": 16,
            }
        ),
        label="Worker Count",
        help_text="Number of worker processes (1-16). More workers can process more tasks simultaneously.",
    )

    q_timeout = forms.IntegerField(
        min_value=0,
        max_value=86400,
        initial=600,
        widget=forms.NumberInput(
            attrs={
                "class": INPUT_CLASS,
                "min": 0,
                "max": 86400,
            }
        ),
        label="Task Timeout (seconds)",
        help_text="Maximum time a task can run before worker timeout. Use 0 for no timeout (required on Windows). For long-running scripts, also increase the script's own timeout.",
    )

    q_retry = forms.IntegerField(
        min_value=60,
        max_value=86400,
        initial=660,
        widget=forms.NumberInput(
            attrs={
                "class": INPUT_CLASS,
                "min": 60,
                "max": 86400,
            }
        ),
        label="Retry Delay (seconds)",
        help_text="Time before a task is re-delivered to another worker. Should be greater than timeout. At worker start the effective value is automatically raised above the largest script timeout so long runs are never re-delivered mid-run.",
    )

    q_queue_limit = forms.IntegerField(
        min_value=5,
        max_value=100,
        initial=20,
        widget=forms.NumberInput(
            attrs={
                "class": INPUT_CLASS,
                "min": 5,
                "max": 100,
            }
        ),
        label="Queue Limit",
        help_text="Maximum number of tasks that can be queued at once.",
    )

    def __init__(self, *args, instance=None, **kwargs):
        """Initialize form with existing settings."""
        super().__init__(*args, **kwargs)
        if instance:
            self.fields["q_workers"].initial = instance.q_workers
            self.fields["q_timeout"].initial = instance.q_timeout
            self.fields["q_retry"].initial = instance.q_retry
            self.fields["q_queue_limit"].initial = instance.q_queue_limit

    def clean(self):
        """Validate that retry > timeout."""
        cleaned_data = super().clean()
        timeout = cleaned_data.get("q_timeout", 0)
        retry = cleaned_data.get("q_retry", 660)

        if timeout > 0 and retry <= timeout:
            self.add_error(
                "q_retry",
                f"Retry delay ({retry}s) must be greater than timeout ({timeout}s).",
            )

        return cleaned_data

    def save(self, instance):
        """Save the worker settings to the GlobalSettings instance."""
        from django.utils import timezone

        # Check if any values actually changed
        changed = (
            instance.q_workers != self.cleaned_data["q_workers"]
            or instance.q_timeout != self.cleaned_data["q_timeout"]
            or instance.q_retry != self.cleaned_data["q_retry"]
            or instance.q_queue_limit != self.cleaned_data["q_queue_limit"]
        )

        instance.q_workers = self.cleaned_data["q_workers"]
        instance.q_timeout = self.cleaned_data["q_timeout"]
        instance.q_retry = self.cleaned_data["q_retry"]
        instance.q_queue_limit = self.cleaned_data["q_queue_limit"]

        if changed:
            instance.worker_settings_updated_at = timezone.now()

        instance.save(
            update_fields=[
                "q_workers",
                "q_timeout",
                "q_retry",
                "q_queue_limit",
                "worker_settings_updated_at",
                "updated_at",
            ]
        )
        return instance


class ExecutionIsolationForm(forms.Form):
    """Form for the script-execution sandbox (FOUNDATIONS Seam 2).

    Dashboard-managed, stored on ``GlobalSettings`` and resolved per-run at
    execution time (no restart). Mirrors ``WorkerSettingsForm``. The resource
    limits are active immediately; the isolation *mode* selects the filesystem/
    network sandbox (``SandboxedSubprocessBackend``), resolved per run against the
    workspace/script policy.
    """

    from core.models import GlobalSettings

    SANDBOX_MODE_CHOICES = GlobalSettings.SandboxMode.choices

    sandbox_default = forms.ChoiceField(
        choices=SANDBOX_MODE_CHOICES,
        initial=GlobalSettings.SandboxMode.OFF,
        widget=forms.Select(attrs={"class": INPUT_CLASS}),
        label="Isolation default",
        help_text="Instance-wide default. Gates the filesystem/network sandbox; "
        "the resource limits below apply regardless.",
    )

    sandbox_fail_closed = forms.BooleanField(
        required=False,
        initial=False,
        widget=forms.CheckboxInput(attrs={"class": CHECK_CLASS}),
        label="Fail closed",
        help_text="When a required sandbox is unavailable on the host, fail the "
        "run instead of degrading to a lower tier with a warning.",
    )

    sandbox_rlimit_memory_mb = forms.IntegerField(
        min_value=0,
        max_value=1048576,
        initial=0,
        widget=forms.NumberInput(attrs={"class": INPUT_CLASS, "min": 0}),
        label="Memory limit (MB)",
        help_text="Per-run address-space cap (RLIMIT_AS). 0 = unlimited. POSIX only.",
    )

    sandbox_rlimit_cpu_seconds = forms.IntegerField(
        min_value=0,
        max_value=86400,
        initial=0,
        widget=forms.NumberInput(attrs={"class": INPUT_CLASS, "min": 0}),
        label="CPU time limit (seconds)",
        help_text="Per-run CPU-time cap (RLIMIT_CPU). 0 = unlimited. POSIX only.",
    )

    sandbox_rlimit_nproc = forms.IntegerField(
        min_value=0,
        max_value=100000,
        initial=0,
        widget=forms.NumberInput(attrs={"class": INPUT_CLASS, "min": 0}),
        label="Process limit",
        help_text="Per-run process/thread cap (RLIMIT_NPROC, fork-bomb guard). "
        "0 = unlimited. POSIX only.",
    )

    sandbox_rlimit_fsize_mb = forms.IntegerField(
        min_value=0,
        max_value=1048576,
        initial=0,
        widget=forms.NumberInput(attrs={"class": INPUT_CLASS, "min": 0}),
        label="Max file size (MB)",
        help_text="Per-run largest single-file write (RLIMIT_FSIZE). 0 = unlimited. POSIX only.",
    )

    def __init__(self, *args, instance=None, **kwargs):
        """Initialize form with existing settings."""
        super().__init__(*args, **kwargs)
        if instance:
            self.fields["sandbox_default"].initial = instance.sandbox_default
            self.fields["sandbox_fail_closed"].initial = instance.sandbox_fail_closed
            self.fields["sandbox_rlimit_memory_mb"].initial = instance.sandbox_rlimit_memory_mb
            self.fields["sandbox_rlimit_cpu_seconds"].initial = instance.sandbox_rlimit_cpu_seconds
            self.fields["sandbox_rlimit_nproc"].initial = instance.sandbox_rlimit_nproc
            self.fields["sandbox_rlimit_fsize_mb"].initial = instance.sandbox_rlimit_fsize_mb

    def save(self, instance):
        """Save the isolation settings to the GlobalSettings instance."""
        instance.sandbox_default = self.cleaned_data["sandbox_default"]
        instance.sandbox_fail_closed = self.cleaned_data.get("sandbox_fail_closed", False)
        instance.sandbox_rlimit_memory_mb = self.cleaned_data.get("sandbox_rlimit_memory_mb") or 0
        instance.sandbox_rlimit_cpu_seconds = self.cleaned_data.get("sandbox_rlimit_cpu_seconds") or 0
        instance.sandbox_rlimit_nproc = self.cleaned_data.get("sandbox_rlimit_nproc") or 0
        instance.sandbox_rlimit_fsize_mb = self.cleaned_data.get("sandbox_rlimit_fsize_mb") or 0
        instance.save(
            update_fields=[
                "sandbox_default",
                "sandbox_fail_closed",
                "sandbox_rlimit_memory_mb",
                "sandbox_rlimit_cpu_seconds",
                "sandbox_rlimit_nproc",
                "sandbox_rlimit_fsize_mb",
                "updated_at",
            ]
        )
        return instance


class BackupCreateForm(forms.Form):
    """Form for configuring backup creation."""

    backup_format = forms.ChoiceField(
        choices=[
            ("gzip", "Compressed (recommended) - .json.gz"),
            ("json", "Plain JSON - .json"),
        ],
        initial="gzip",
        widget=forms.RadioSelect(
            attrs={
                "class": CHECK_CLASS,
            }
        ),
        label="Backup format",
        help_text="Compressed backups are 80-95% smaller",
    )

    include_datastores = forms.BooleanField(
        required=False,
        initial=True,
        widget=forms.CheckboxInput(
            attrs={
                "class": CHECK_CLASS,
            }
        ),
        label="Include DataStores",
        help_text="Include all DataStores and their key-value entries",
    )

    include_runs = forms.BooleanField(
        required=False,
        initial=True,
        widget=forms.CheckboxInput(
            attrs={
                "class": CHECK_CLASS,
            }
        ),
        label="Include run history",
        help_text="Include execution history (stdout/stderr)",
    )

    max_runs = forms.IntegerField(
        initial=1000,
        min_value=0,
        max_value=10000,
        widget=forms.NumberInput(
            attrs={
                "class": INPUT_CLASS,
                "placeholder": "1000",
            }
        ),
        label="Maximum runs to include",
        help_text="Limit run history to most recent N runs (0 = all runs)",
    )

    include_package_operations = forms.BooleanField(
        required=False,
        initial=False,
        widget=forms.CheckboxInput(
            attrs={
                "class": CHECK_CLASS,
            }
        ),
        label="Include package operations",
        help_text="Include pip installation history",
    )


class BackupRestoreForm(forms.Form):
    """Form for restoring from backup."""

    backup_file = forms.FileField(
        widget=forms.FileInput(
            attrs={
                "class": FILE_CLASS,
                "accept": ".json,.json.gz,.gz",
            }
        ),
        label="Backup file",
        help_text="JSON or compressed backup file (.json or .json.gz)",
    )

    restore_runs = forms.BooleanField(
        required=False,
        initial=True,
        widget=forms.CheckboxInput(
            attrs={
                "class": CHECK_CLASS,
            }
        ),
        label="Restore run history",
        help_text="Import execution history from backup",
    )

    confirm_delete = forms.BooleanField(
        required=True,
        widget=forms.CheckboxInput(
            attrs={
                "class": "w-4 h-4 rounded text-fail bg-ink border-line focus:ring-fail/40 focus:ring-2",
            }
        ),
        label="I understand all existing data will be deleted",
        help_text="This action cannot be undone without the automatic backup",
    )


# =============================================================================
# Data Store Forms
# =============================================================================


class DataStoreForm(forms.ModelForm):
    """Form for creating and editing data stores."""

    def __init__(self, *args, workspace=None, **kwargs):
        # The active workspace scopes the name-uniqueness check (tenancy: names
        # are unique per workspace, not globally).
        self._workspace = workspace
        super().__init__(*args, **kwargs)

    class Meta:
        model = DataStore
        fields = ["name", "description"]
        widgets = {
            "name": forms.TextInput(
                attrs={
                    "class": INPUT_CLASS,
                    "placeholder": "my_data_store",
                }
            ),
            "description": forms.Textarea(
                attrs={
                    "class": INPUT_CLASS,
                    "rows": 2,
                    "placeholder": "What is this data store used for?",
                }
            ),
        }
        labels = {
            "name": "Store Name",
            "description": "Description",
        }
        help_texts = {
            "name": "Used in scripts as: DataStore(\"name\")",
        }

    def clean_name(self):
        name = self.cleaned_data.get("name", "").strip()
        if not name:
            raise forms.ValidationError("Store name is required.")
        # Check for valid identifier-like name
        if not name.replace("_", "").replace("-", "").isalnum():
            raise forms.ValidationError(
                "Name can only contain letters, numbers, underscores, and hyphens."
            )
        # Check uniqueness within the active workspace (names are per-workspace).
        qs = DataStore.objects.filter(name__iexact=name)
        if self._workspace is not None:
            qs = qs.filter(workspace=self._workspace)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError(
                "A data store with this name already exists in this workspace."
            )
        return name


class DatabaseForm(forms.ModelForm):
    """Form for creating and editing managed databases."""

    def __init__(self, *args, workspace=None, **kwargs):
        # The active workspace scopes the name-uniqueness check (tenancy: names
        # are unique per workspace, not globally).
        self._workspace = workspace
        super().__init__(*args, **kwargs)

    class Meta:
        model = Database
        fields = ["name", "description"]
        widgets = {
            "name": forms.TextInput(
                attrs={
                    "class": INPUT_CLASS,
                    "placeholder": "my_database",
                }
            ),
            "description": forms.Textarea(
                attrs={
                    "class": INPUT_CLASS,
                    "rows": 2,
                    "placeholder": "What is this database used for?",
                }
            ),
        }
        labels = {
            "name": "Database Name",
            "description": "Description",
        }
        help_texts = {
            "name": 'Used in scripts as: pyrunner_db.connect("name")',
        }

    def clean_name(self):
        name = self.cleaned_data.get("name", "").strip()
        if not name:
            raise forms.ValidationError("Database name is required.")
        # Identifier-like names only: the Postgres schema/role name is derived
        # from this (lowercased, hyphens folded to underscores).
        if not name.replace("_", "").replace("-", "").isalnum():
            raise forms.ValidationError(
                "Name can only contain letters, numbers, underscores, and hyphens."
            )
        # Check uniqueness within the active workspace (names are per-workspace).
        qs = Database.objects.filter(name__iexact=name)
        if self._workspace is not None:
            qs = qs.filter(workspace=self._workspace)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError(
                "A database with this name already exists in this workspace."
            )
        return name


class DataStoreEntryForm(forms.Form):
    """Form for creating and editing data store entries."""

    key = forms.CharField(
        max_length=255,
        widget=forms.TextInput(
            attrs={
                "class": INPUT_CLASS + " font-mono",
                "placeholder": "my_key",
            }
        ),
        label="Key",
        help_text="Unique identifier for this entry",
    )

    value = forms.CharField(
        widget=forms.Textarea(
            attrs={
                "class": INPUT_CLASS + " font-mono",
                "rows": 6,
                "placeholder": '{"example": "value"}\nor just a string\nor a number like 42',
            }
        ),
        label="Value (JSON)",
        help_text="JSON value: string, number, boolean, array, or object",
    )

    def __init__(self, *args, datastore=None, instance=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.datastore = datastore
        self.instance = instance

        # Pre-populate for editing
        if instance:
            self.fields["key"].initial = instance.key
            self.fields["value"].initial = instance.value_json

    def clean_key(self):
        key = self.cleaned_data.get("key", "").strip()
        if not key:
            raise forms.ValidationError("Key is required.")
        if len(key) > 255:
            raise forms.ValidationError("Key cannot exceed 255 characters.")

        # Check uniqueness within the data store
        if self.datastore:
            qs = DataStoreEntry.objects.filter(datastore=self.datastore, key=key)
            if self.instance:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise forms.ValidationError(
                    f"Key '{key}' already exists in this data store."
                )
        return key

    def clean_value(self):
        import json

        value = self.cleaned_data.get("value", "").strip()
        if not value:
            raise forms.ValidationError("Value is required.")

        try:
            # Validate it's valid JSON
            json.loads(value)
        except json.JSONDecodeError as e:
            raise forms.ValidationError(f"Invalid JSON: {e}")

        return value


# =============================================================================
# API Token Forms
# =============================================================================


class APITokenForm(forms.ModelForm):
    """Form for creating API tokens (datastore, all-datastores, or plugin scope)."""

    # Plugin choices are the plugins loaded RIGHT NOW (validated at creation
    # time only — plugin_slug is not an FK; see the model).
    plugin_slug = forms.ChoiceField(
        required=False,
        label="Plugin",
        help_text="The plugin whose API this token can call",
        widget=forms.Select(attrs={"class": INPUT_CLASS}),
    )

    class Meta:
        model = APIToken
        fields = ["name", "scope", "datastore", "plugin_slug", "expires_at"]
        widgets = {
            "name": forms.TextInput(
                attrs={
                    "class": INPUT_CLASS,
                    "placeholder": "My Dashboard Token",
                }
            ),
            "scope": forms.Select(
                attrs={
                    "class": INPUT_CLASS,
                }
            ),
            "datastore": forms.Select(
                attrs={
                    "class": INPUT_CLASS,
                }
            ),
            "expires_at": forms.DateTimeInput(
                attrs={
                    "class": INPUT_CLASS,
                    "type": "datetime-local",
                },
                format="%Y-%m-%dT%H:%M",
            ),
        }
        labels = {
            "name": "Token Name",
            "scope": "Scope",
            "datastore": "Datastore",
            "expires_at": "Expires At",
        }
        help_texts = {
            "name": "A friendly name to identify this token",
            "scope": "What this token grants access to",
            "datastore": "Required for single-datastore scope",
            "expires_at": "Optional. Leave empty for no expiration.",
        }

    def __init__(self, *args, workspace=None, **kwargs):
        super().__init__(*args, **kwargs)
        # Make datastore optional with a clear empty choice
        self.fields["datastore"].required = False
        self.fields["datastore"].empty_label = "Select a datastore…"
        self.fields["expires_at"].required = False
        # Scope the datastore choices to the active workspace (tenancy Stage 3),
        # so a token can't be bound to another workspace's datastore.
        if workspace is not None:
            self.fields["datastore"].queryset = DataStore.objects.for_workspace(
                workspace
            )
        # Offer only currently-loaded plugins for plugin scope. Lazy import:
        # forms.py must stay importable without the plugin registry populated.
        from core.plugins import all_plugins

        self.fields["plugin_slug"].choices = [("", "Select a plugin…")] + [
            (p.slug, f"{p.name} ({p.slug})") for p in sorted(all_plugins(), key=lambda p: p.slug)
        ]

    def clean(self):
        cleaned = super().clean()
        scope = cleaned.get("scope")
        if scope == APIToken.Scope.DATASTORE and not cleaned.get("datastore"):
            self.add_error("datastore", "Select the datastore this token is scoped to.")
        if scope == APIToken.Scope.PLUGIN and not cleaned.get("plugin_slug"):
            self.add_error("plugin_slug", "Select the plugin this token is scoped to.")
        # Blank out the fields the chosen scope doesn't use (matches the model's
        # CheckConstraints — a stray selection must not survive a scope switch).
        if scope != APIToken.Scope.DATASTORE:
            cleaned["datastore"] = None
        if scope != APIToken.Scope.PLUGIN:
            cleaned["plugin_slug"] = ""
        return cleaned


# Historical name — kept importable alongside the model alias.
DataStoreAPITokenForm = APITokenForm


# =============================================================================
# Authentication Forms
# =============================================================================



class PasswordLoginForm(forms.Form):
    """Form for password-based login."""

    email = forms.EmailField(
        widget=forms.EmailInput(
            attrs={
                "class": INPUT_CLASS,
                "placeholder": "you@example.com",
                "autocomplete": "email",
            }
        ),
        label="Email address",
    )
    password = forms.CharField(
        widget=forms.PasswordInput(
            attrs={
                "class": INPUT_CLASS,
                "placeholder": "Your password",
                "autocomplete": "current-password",
            }
        ),
        label="Password",
    )


class SetPasswordForm(forms.Form):
    """Form for setting or changing password.

    Enforces the AUTH_PASSWORD_VALIDATORS policy — the settings-declared
    validators only apply through an explicit validate_password() call, which
    lives here. Pass ``user`` so the similarity validator can compare the
    password against the account's email.
    """

    password = forms.CharField(
        min_length=8,
        widget=forms.PasswordInput(
            attrs={
                "class": INPUT_CLASS,
                "placeholder": "New password",
                "autocomplete": "new-password",
            }
        ),
        label="New Password",
        help_text="At least 8 characters, not entirely numeric, and not a common password",
    )
    password_confirm = forms.CharField(
        widget=forms.PasswordInput(
            attrs={
                "class": INPUT_CLASS,
                "placeholder": "Confirm new password",
                "autocomplete": "new-password",
            }
        ),
        label="Confirm Password",
    )

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user

    def clean(self):
        from django.contrib.auth import password_validation

        cleaned_data = super().clean()
        password = cleaned_data.get("password")
        confirm = cleaned_data.get("password_confirm")
        if password and confirm and password != confirm:
            raise forms.ValidationError("Passwords do not match.")
        if password:
            try:
                password_validation.validate_password(password, user=self.user)
            except forms.ValidationError as exc:
                self.add_error("password", exc)
        return cleaned_data


class AdminSetupForm(forms.Form):
    """Form for initial admin setup with password.

    Enforces AUTH_PASSWORD_VALIDATORS (see SetPasswordForm) — the instance's
    first superuser deserves at least the standard policy.
    """

    email = forms.EmailField(
        widget=forms.EmailInput(
            attrs={
                "class": INPUT_CLASS,
                "placeholder": "admin@example.com",
                "autocomplete": "email",
            }
        ),
        label="Admin Email",
    )
    password = forms.CharField(
        min_length=8,
        widget=forms.PasswordInput(
            attrs={
                "class": INPUT_CLASS,
                "placeholder": "Create a strong password",
                "autocomplete": "new-password",
            }
        ),
        label="Password",
        help_text="At least 8 characters, not entirely numeric, and not a common password",
    )
    password_confirm = forms.CharField(
        widget=forms.PasswordInput(
            attrs={
                "class": INPUT_CLASS,
                "placeholder": "Confirm password",
                "autocomplete": "new-password",
            }
        ),
        label="Confirm Password",
    )

    def clean(self):
        from django.contrib.auth import password_validation

        from core.models import User

        cleaned_data = super().clean()
        password = cleaned_data.get("password")
        confirm = cleaned_data.get("password_confirm")
        if password and confirm and password != confirm:
            raise forms.ValidationError("Passwords do not match.")
        if password:
            # Transient (unsaved) user so the similarity validator can compare
            # against the email being registered.
            probe = User(email=cleaned_data.get("email") or "")
            try:
                password_validation.validate_password(password, user=probe)
            except forms.ValidationError as exc:
                self.add_error("password", exc)
        return cleaned_data


# =============================================================================
# Services Forms
# =============================================================================


class StorageConnectionForm(forms.Form):
    """Create/edit one StorageConnection (credentials stored encrypted).

    Twin of ``AIProviderForm``: many credentialed rows, one selected per role
    (``is_default`` for backups, ``GlobalSettings.assets_storage`` for plugins).
    """

    from core.models import StorageConnection as _SC

    provider_type = forms.ChoiceField(
        choices=_SC.ProviderType.choices,
        initial=_SC.ProviderType.R2,
        widget=forms.Select(attrs={"class": INPUT_CLASS}),
        label="Provider",
    )

    name = forms.CharField(
        max_length=100,
        widget=forms.TextInput(
            attrs={"class": INPUT_CLASS, "placeholder": "e.g. Backups (R2)"}
        ),
        label="Name",
    )

    endpoint_url = forms.CharField(
        required=False,
        max_length=500,
        widget=forms.TextInput(
            attrs={"class": INPUT_CLASS, "placeholder": "https://…"}
        ),
        label="Endpoint URL",
        help_text="Leave empty for AWS S3. Required for R2, MinIO, Spaces, B2, etc.",
    )

    region = forms.CharField(
        required=False,
        max_length=50,
        initial="us-east-1",
        widget=forms.TextInput(attrs={"class": INPUT_CLASS, "placeholder": "us-east-1"}),
        label="Region",
    )

    bucket = forms.CharField(
        max_length=255,
        widget=forms.TextInput(
            attrs={"class": INPUT_CLASS, "placeholder": "my-bucket"}
        ),
        label="Bucket",
    )

    access_key = forms.CharField(
        required=False,
        widget=forms.PasswordInput(
            attrs={
                "class": INPUT_CLASS,
                "placeholder": "Leave blank to keep current",
                "autocomplete": "new-password",
            }
        ),
        label="Access Key",
    )

    secret_key = forms.CharField(
        required=False,
        widget=forms.PasswordInput(
            attrs={
                "class": INPUT_CLASS,
                "placeholder": "Leave blank to keep current",
                "autocomplete": "new-password",
            }
        ),
        label="Secret Key",
    )

    public_base_url = forms.CharField(
        required=False,
        max_length=500,
        widget=forms.TextInput(
            attrs={"class": INPUT_CLASS, "placeholder": "https://assets.example.com"}
        ),
        label="Public base URL",
        help_text="Set this to serve objects at permanent, hot-linkable URLs. Blank = "
        "objects are served via presigned URLs, which EXPIRE (default 1 hour).",
    )

    use_ssl = forms.BooleanField(
        required=False,
        initial=True,
        widget=forms.CheckboxInput(attrs={"class": CHECK_CLASS}),
        label="Use SSL/TLS",
        help_text="Recommended for security. Disable only for local development.",
    )

    path_style = forms.BooleanField(
        required=False,
        initial=False,
        widget=forms.CheckboxInput(attrs={"class": CHECK_CLASS}),
        label="Path-style addressing",
        help_text="Required for MinIO and some S3-compatible providers.",
    )

    is_default = forms.BooleanField(
        required=False,
        initial=False,
        widget=forms.CheckboxInput(attrs={"class": CHECK_CLASS}),
        label="Use for backups",
        help_text="Scheduled and manual backups upload through this connection.",
    )

    enabled = forms.BooleanField(
        required=False,
        initial=True,
        widget=forms.CheckboxInput(attrs={"class": CHECK_CLASS}),
        label="Enabled",
    )

    def __init__(self, *args, instance=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance = instance
        if instance:
            self.fields["provider_type"].initial = instance.provider_type
            self.fields["name"].initial = instance.name
            self.fields["endpoint_url"].initial = instance.endpoint_url
            self.fields["region"].initial = instance.region or "us-east-1"
            self.fields["bucket"].initial = instance.bucket
            self.fields["public_base_url"].initial = instance.public_base_url
            self.fields["use_ssl"].initial = instance.use_ssl
            self.fields["path_style"].initial = instance.path_style
            self.fields["is_default"].initial = instance.is_default
            self.fields["enabled"].initial = instance.enabled

    def clean_name(self):
        from core.models import StorageConnection

        name = self.cleaned_data["name"].strip()
        qs = StorageConnection.objects.filter(name__iexact=name)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError("A connection with this name already exists.")
        return name

    def clean_public_base_url(self):
        url = (self.cleaned_data.get("public_base_url") or "").strip().rstrip("/")
        if url and not url.startswith(("http://", "https://")):
            raise forms.ValidationError("Must start with http:// or https://")
        return url

    def clean(self):
        from core.models import STORAGE_PRESETS, StorageConnection
        from core.services.s3_service import is_safe_endpoint_url

        cleaned = super().clean()
        ptype = cleaned.get("provider_type")
        if not ptype:
            return cleaned
        preset = STORAGE_PRESETS.get(ptype, {})

        # Endpoint: AWS derives its own from the region; everything else needs one.
        endpoint = (cleaned.get("endpoint_url") or "").strip()
        if ptype == StorageConnection.ProviderType.S3:
            endpoint = ""
        elif not endpoint:
            self.add_error("endpoint_url", "An endpoint URL is required for this provider.")
        if endpoint:
            # Validate here as well as in get_client so a bad endpoint is a form
            # error at save time, not a mystery failure on the next backup run.
            is_safe, error_msg = is_safe_endpoint_url(endpoint)
            if not is_safe:
                self.add_error("endpoint_url", error_msg)
        cleaned["endpoint_url"] = endpoint

        if not cleaned.get("region"):
            cleaned["region"] = preset.get("region_default", "us-east-1")

        # Credentials: required on create; blank on edit keeps the stored ones.
        has_saved = bool(
            self.instance
            and self.instance.access_key_encrypted
            and self.instance.secret_key_encrypted
        )
        if not has_saved:
            if not cleaned.get("access_key"):
                self.add_error("access_key", "An access key is required.")
            if not cleaned.get("secret_key"):
                self.add_error("secret_key", "A secret key is required.")

        return cleaned

    def save(self):
        """Create or update the StorageConnection row."""
        from core.models import StorageConnection
        from core.services.encryption_service import EncryptionService

        connection = self.instance or StorageConnection()
        connection.provider_type = self.cleaned_data["provider_type"]
        connection.name = self.cleaned_data["name"]
        connection.endpoint_url = self.cleaned_data.get("endpoint_url") or ""
        connection.region = self.cleaned_data.get("region") or "us-east-1"
        connection.bucket = self.cleaned_data["bucket"]
        connection.public_base_url = self.cleaned_data.get("public_base_url") or ""
        connection.use_ssl = self.cleaned_data.get("use_ssl", True)
        connection.path_style = self.cleaned_data.get("path_style", False)
        connection.is_default = self.cleaned_data.get("is_default", False)
        connection.enabled = self.cleaned_data.get("enabled", True)

        # Only overwrite credentials when new values are provided.
        access_key = self.cleaned_data.get("access_key")
        if access_key:
            connection.access_key_encrypted = EncryptionService.encrypt(access_key)
        secret_key = self.cleaned_data.get("secret_key")
        if secret_key:
            connection.secret_key_encrypted = EncryptionService.encrypt(secret_key)

        # StorageConnection.save() enforces the single-default invariant.
        connection.save()
        return connection


class AssetsStorageForm(forms.Form):
    """Pick which connection the plugin storage seam (StorageAPI) uses.

    Separate from the backup default on purpose: blank here means the seam is
    unavailable, and there is deliberately no fallback to the backup connection.
    """

    assets_storage = forms.ModelChoiceField(
        required=False,
        queryset=None,  # set in __init__ (avoids import-time DB access)
        empty_label="Not configured — storage seam unavailable",
        widget=forms.Select(attrs={"class": INPUT_CLASS}),
        label="Assets connection",
        help_text="Plugins read and write here through StorageAPI, each confined to "
        "its own apps/<plugin>/ prefix.",
    )

    def __init__(self, *args, instance=None, **kwargs):
        from django.db.models import Q
        from core.models import StorageConnection

        super().__init__(*args, **kwargs)

        # Enabled rows, PLUS whatever is currently selected even if it has since
        # been disabled. Without the second half, disabling the assets connection
        # drops it out of the queryset, so the card renders as "not configured"
        # and the next save of this form silently clears the selection — losing
        # the choice rather than just its availability (and re-submitting the
        # disabled id would fail with an opaque "not a valid choice").
        selected_id = instance.assets_storage_id if instance else None
        self.fields["assets_storage"].queryset = StorageConnection.objects.filter(
            Q(enabled=True) | Q(pk=selected_id)
        ).order_by("name")
        if instance:
            self.fields["assets_storage"].initial = selected_id

    def save(self, instance):
        instance.assets_storage = self.cleaned_data.get("assets_storage")
        instance.save(update_fields=["assets_storage"])
        return instance


class AISettingsForm(forms.Form):
    """Master AI toggle + active provider selection (Services → AI Provider)."""

    claude_enabled = forms.BooleanField(
        required=False,
        initial=False,
        widget=forms.CheckboxInput(attrs={"class": CHECK_CLASS}),
        label="Enable AI for scripts",
    )

    active_provider = forms.UUIDField(
        required=False,
        widget=forms.Select(attrs={"class": INPUT_CLASS}),
        label="Active provider",
        help_text="Used by scripts, Py AI, and connection tests.",
    )

    def __init__(self, *args, instance=None, **kwargs):
        super().__init__(*args, **kwargs)
        from core.models import AIProvider

        choices = [("", "— none —")] + [
            (str(p.id), f"{p.name} ({p.get_provider_type_display()})")
            for p in AIProvider.objects.all()
        ]
        self.fields["active_provider"].widget.choices = choices
        if instance:
            self.fields["claude_enabled"].initial = instance.claude_enabled
            self.fields["active_provider"].initial = instance.active_ai_provider_id

    def clean_active_provider(self):
        # A valid-UUID-but-nonexistent id (e.g. the provider was deleted in
        # another tab between render and submit) must surface as a form error —
        # otherwise save() would silently store None ("AI switched off") while
        # the view reports success.
        from core.models import AIProvider

        provider_id = self.cleaned_data.get("active_provider")
        if provider_id and not AIProvider.objects.filter(pk=provider_id).exists():
            raise forms.ValidationError(
                "That provider no longer exists — it may have been deleted in "
                "another tab. Refresh and pick an existing provider."
            )
        return provider_id

    def save(self, instance):
        """Save AI settings to the GlobalSettings instance."""
        from core.models import AIProvider

        instance.claude_enabled = self.cleaned_data.get("claude_enabled", False)
        provider_id = self.cleaned_data.get("active_provider")
        instance.active_ai_provider = (
            AIProvider.objects.filter(pk=provider_id).first() if provider_id else None
        )
        instance.save()
        return instance


class AIProviderForm(forms.Form):
    """Create/edit one AIProvider profile (credential stored encrypted)."""

    from core.models import AIProvider as _AIP

    provider_type = forms.ChoiceField(
        choices=_AIP.ProviderType.choices,
        initial=_AIP.ProviderType.ANTHROPIC,
        widget=forms.Select(attrs={"class": INPUT_CLASS}),
        label="Provider",
    )

    name = forms.CharField(
        max_length=100,
        widget=forms.TextInput(
            attrs={"class": INPUT_CLASS, "placeholder": "e.g. My Z.AI plan"}
        ),
        label="Name",
    )

    base_url = forms.CharField(
        required=False,
        max_length=255,
        widget=forms.TextInput(
            attrs={"class": INPUT_CLASS, "placeholder": "https://…"}
        ),
        label="Endpoint URL",
        help_text="Prefilled per provider; required for custom endpoints. Not used for Anthropic.",
    )

    auth_method = forms.ChoiceField(
        required=False,
        choices=_AIP.AuthMethod.choices,
        initial=_AIP.AuthMethod.SUBSCRIPTION,
        widget=forms.Select(attrs={"class": INPUT_CLASS}),
        label="Authentication method",
        help_text="Anthropic only: subscription token from `claude setup-token` (starts with sk-ant-oat01-) or an API key from console.anthropic.com.",
    )

    credential = forms.CharField(
        required=False,
        widget=forms.PasswordInput(
            attrs={
                "class": INPUT_CLASS,
                "placeholder": "Leave blank to keep current",
                "autocomplete": "new-password",
            }
        ),
        label="Credential",
    )

    default_model = forms.CharField(
        required=False,
        max_length=100,
        widget=forms.TextInput(
            attrs={"class": INPUT_CLASS, "placeholder": "optional"}
        ),
        label="Default model",
        help_text="Used whenever this provider is active. Blank = account/endpoint default.",
    )

    def __init__(self, *args, instance=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance = instance
        if instance:
            self.fields["provider_type"].initial = instance.provider_type
            self.fields["name"].initial = instance.name
            self.fields["base_url"].initial = instance.base_url
            self.fields["auth_method"].initial = instance.auth_method
            self.fields["default_model"].initial = instance.default_model

    def clean_name(self):
        from core.models import AIProvider

        name = self.cleaned_data["name"].strip()
        qs = AIProvider.objects.filter(name__iexact=name)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError("A provider with this name already exists.")
        return name

    def clean(self):
        from core.models import AIProvider, PROVIDER_PRESETS

        cleaned = super().clean()
        ptype = cleaned.get("provider_type")
        if not ptype:
            return cleaned
        preset = PROVIDER_PRESETS.get(ptype, {})

        # Endpoint URL: prefill from preset; custom must supply one.
        base_url = (cleaned.get("base_url") or "").strip()
        if ptype == AIProvider.ProviderType.ANTHROPIC:
            base_url = ""
        elif not base_url:
            base_url = preset.get("base_url", "")
            if not base_url:
                self.add_error("base_url", "An endpoint URL is required for this provider.")
        cleaned["base_url"] = base_url

        # Auth method only means something for Anthropic.
        if ptype != AIProvider.ProviderType.ANTHROPIC:
            cleaned["auth_method"] = AIProvider.AuthMethod.API_KEY
        elif not cleaned.get("auth_method"):
            cleaned["auth_method"] = AIProvider.AuthMethod.SUBSCRIPTION

        # Credential: required on create except when the preset has a default
        # (Ollama). On edit, blank keeps the stored one.
        has_saved = bool(self.instance and self.instance.credential_encrypted)
        if not cleaned.get("credential") and not has_saved:
            if not preset.get("default_credential"):
                self.add_error("credential", "A credential is required for this provider.")

        return cleaned

    def save(self):
        """Create or update the AIProvider row."""
        from core.models import AIProvider
        from core.services.encryption_service import EncryptionService

        provider = self.instance or AIProvider()
        provider.provider_type = self.cleaned_data["provider_type"]
        provider.name = self.cleaned_data["name"]
        provider.base_url = self.cleaned_data.get("base_url") or ""
        provider.auth_method = self.cleaned_data["auth_method"]
        provider.default_model = self.cleaned_data.get("default_model") or ""

        # Only overwrite the credential when a new value is provided.
        credential = self.cleaned_data.get("credential")
        if credential:
            provider.credential_encrypted = EncryptionService.encrypt(credential)

        provider.save()
        return provider


class SecretProviderForm(forms.Form):
    """Create/edit one SecretProvider profile (External Secret Providers).

    The adapter-specific credential/config inputs are NOT declared as Django
    fields — they arrive as ``f_<name>`` POST keys and are validated against the
    selected backend's declarative ``fields`` specs. So adding a new adapter needs
    zero form changes: the registry drives the type dropdown, the rendered inputs,
    and this validation. Credential fields follow the same preserve-on-edit rule
    as AIProviderForm (blank keeps the stored value).
    """

    FIELD_PREFIX = "f_"

    provider_type = forms.ChoiceField(
        widget=forms.Select(attrs={"class": INPUT_CLASS}),
        label="Provider",
    )
    name = forms.CharField(
        max_length=100,
        widget=forms.TextInput(
            attrs={"class": INPUT_CLASS, "placeholder": "e.g. Prod Vault"}
        ),
        label="Name",
    )
    cache_ttl = forms.IntegerField(
        min_value=0,
        initial=300,
        widget=forms.NumberInput(attrs={"class": INPUT_CLASS}),
        label="Cache TTL (seconds)",
        help_text="How long to cache a fetched value in-process. 0 disables caching.",
    )
    on_error = forms.ChoiceField(
        widget=forms.Select(attrs={"class": INPUT_CLASS}),
        label="On fetch error",
        help_text="Fail the run, or serve the last cached value if one exists.",
    )

    def __init__(self, *args, instance=None, **kwargs):
        from core.services.secret_backends import list_backends

        super().__init__(*args, **kwargs)
        self.instance = instance
        self.fields["provider_type"].choices = [
            (b.provider_key, b.label or b.provider_key) for b in list_backends()
        ]
        self.fields["on_error"].choices = SecretProvider.OnError.choices
        if instance is not None and not self.is_bound:
            self.fields["provider_type"].initial = instance.provider_type
            self.fields["name"].initial = instance.name
            self.fields["cache_ttl"].initial = instance.cache_ttl
            self.fields["on_error"].initial = instance.on_error

    def _selected_backend(self):
        from core.services.secret_backends import SecretResolutionError, get_backend

        ptype = None
        if self.is_bound:
            ptype = self.data.get("provider_type")
        if not ptype and self.instance is not None:
            ptype = self.instance.provider_type
        if not ptype:
            return None
        try:
            return get_backend(ptype)
        except SecretResolutionError:
            return None

    def clean_provider_type(self):
        from core.services.secret_backends import SecretResolutionError, get_backend

        ptype = self.cleaned_data["provider_type"]
        try:
            get_backend(ptype)
        except SecretResolutionError:
            raise forms.ValidationError("Unknown provider type.")
        return ptype

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        qs = SecretProvider.objects.filter(name__iexact=name)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError("A provider with this name already exists.")
        return name

    def clean(self):
        cleaned = super().clean()
        backend = self._selected_backend()
        if backend is None:
            return cleaned

        config = {}
        # Preserve-on-edit: start from the stored credentials and override only the
        # fields the user re-typed, so editing config without re-entering secrets works.
        creds = dict(self.instance.get_credentials()) if self.instance else {}
        for spec in backend.fields:
            raw = (self.data.get(self.FIELD_PREFIX + spec["name"], "") or "").strip()
            if spec.get("kind") == "credential":
                if raw:
                    creds[spec["name"]] = raw
                if spec.get("required") and not creds.get(spec["name"]):
                    self.add_error(None, f"{spec['label']} is required.")
            else:  # config
                config[spec["name"]] = raw
                if spec.get("required") and not raw:
                    self.add_error(None, f"{spec['label']} is required.")
        cleaned["config"] = config
        cleaned["credentials"] = creds
        return cleaned

    def save(self):
        provider = self.instance or SecretProvider()
        provider.provider_type = self.cleaned_data["provider_type"]
        provider.name = self.cleaned_data["name"]
        provider.cache_ttl = self.cleaned_data["cache_ttl"]
        provider.on_error = self.cleaned_data["on_error"]
        provider.config = self.cleaned_data.get("config", {})
        provider.set_credentials(self.cleaned_data.get("credentials", {}))
        provider.save()
        return provider


class RecaptchaSettingsForm(forms.Form):
    """Form for Google reCAPTCHA v2 login-protection configuration."""

    recaptcha_enabled = forms.BooleanField(
        required=False,
        initial=False,
        widget=forms.CheckboxInput(attrs={"class": CHECK_CLASS}),
        label="Require reCAPTCHA on login",
    )

    recaptcha_site_key = forms.CharField(
        required=False,
        max_length=255,
        widget=forms.TextInput(
            attrs={
                "class": INPUT_CLASS + " font-mono",
                "placeholder": "6Lc...",
                "autocomplete": "off",
            }
        ),
        label="Site key",
        help_text="The public site key from the reCAPTCHA admin console (v2 “I'm not a robot”).",
    )

    recaptcha_secret_key = forms.CharField(
        required=False,
        widget=forms.PasswordInput(
            attrs={
                "class": INPUT_CLASS + " font-mono",
                "placeholder": "Leave blank to keep current",
                "autocomplete": "new-password",
            }
        ),
        label="Secret key",
        help_text="The secret key (kept server-side, encrypted at rest).",
    )

    def __init__(self, *args, instance=None, **kwargs):
        super().__init__(*args, **kwargs)
        if instance:
            self.fields["recaptcha_enabled"].initial = instance.recaptcha_enabled
            self.fields["recaptcha_site_key"].initial = instance.recaptcha_site_key
        self._instance = instance

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get("recaptcha_enabled"):
            if not cleaned_data.get("recaptcha_site_key"):
                self.add_error(
                    "recaptcha_site_key",
                    "Site key is required to enable reCAPTCHA.",
                )
            # Secret key must be provided now or already saved.
            has_saved_secret = bool(
                self._instance and self._instance.recaptcha_secret_key_encrypted
            )
            if not cleaned_data.get("recaptcha_secret_key") and not has_saved_secret:
                self.add_error(
                    "recaptcha_secret_key",
                    "Secret key is required to enable reCAPTCHA.",
                )
        return cleaned_data

    def save(self, instance):
        """Save reCAPTCHA settings to the GlobalSettings instance."""
        from core.services.encryption_service import EncryptionService

        instance.recaptcha_enabled = self.cleaned_data.get("recaptcha_enabled", False)
        instance.recaptcha_site_key = self.cleaned_data.get("recaptcha_site_key") or ""

        # Only overwrite the secret when a new value is provided.
        secret_key = self.cleaned_data.get("recaptcha_secret_key")
        if secret_key:
            instance.recaptcha_secret_key_encrypted = EncryptionService.encrypt(secret_key)

        instance.save(
            update_fields=[
                "recaptcha_enabled",
                "recaptcha_site_key",
                "recaptcha_secret_key_encrypted",
                "updated_at",
            ]
        )
        return instance


class S3BackupScheduleForm(forms.Form):
    """Form for S3 scheduled backup configuration."""

    from core.models import GlobalSettings

    WEEKDAY_CHOICES = [
        (0, "Monday"),
        (1, "Tuesday"),
        (2, "Wednesday"),
        (3, "Thursday"),
        (4, "Friday"),
        (5, "Saturday"),
        (6, "Sunday"),
    ]

    s3_backup_enabled = forms.BooleanField(
        required=False,
        initial=False,
        widget=forms.CheckboxInput(
            attrs={
                "class": CHECK_CLASS,
            }
        ),
        label="Enable Scheduled Backups",
    )

    s3_backup_schedule = forms.ChoiceField(
        choices=GlobalSettings.S3BackupSchedule.choices,
        initial=GlobalSettings.S3BackupSchedule.DISABLED,
        widget=forms.Select(
            attrs={
                "class": INPUT_CLASS,
            }
        ),
        label="Schedule Frequency",
    )

    s3_backup_time = forms.TimeField(
        initial="02:00",
        widget=forms.TimeInput(
            attrs={
                "class": INPUT_CLASS,
                "type": "time",
            }
        ),
        label="Backup Time",
        help_text="Time to run backups (in instance timezone)",
    )

    s3_backup_day = forms.ChoiceField(
        choices=WEEKDAY_CHOICES,
        initial=0,
        widget=forms.Select(
            attrs={
                "class": INPUT_CLASS,
            }
        ),
        label="Day of Week",
        help_text="For weekly backups",
    )

    s3_backup_prefix = forms.CharField(
        required=False,
        max_length=255,
        initial="pyrunner-backups/",
        widget=forms.TextInput(
            attrs={
                "class": INPUT_CLASS,
                "placeholder": "pyrunner-backups/",
            }
        ),
        label="S3 Path Prefix",
        help_text="Path prefix for backup files in the bucket",
    )

    s3_backup_retention_count = forms.IntegerField(
        min_value=0,
        initial=7,
        widget=forms.NumberInput(
            attrs={
                "class": INPUT_CLASS,
                "min": "0",
            }
        ),
        label="Retention Count",
        help_text="Keep the last N backups (0 = keep all)",
    )

    s3_backup_include_runs = forms.BooleanField(
        required=False,
        initial=False,
        widget=forms.CheckboxInput(
            attrs={
                "class": CHECK_CLASS,
            }
        ),
        label="Include Run History",
    )

    s3_backup_max_runs = forms.IntegerField(
        min_value=0,
        initial=1000,
        widget=forms.NumberInput(
            attrs={
                "class": INPUT_CLASS,
                "min": "0",
            }
        ),
        label="Max Runs",
        help_text="Maximum runs to include (0 = all)",
    )

    s3_backup_include_datastores = forms.BooleanField(
        required=False,
        initial=True,
        widget=forms.CheckboxInput(
            attrs={
                "class": CHECK_CLASS,
            }
        ),
        label="Include DataStores",
    )

    def __init__(self, *args, instance=None, **kwargs):
        """Initialize form with existing settings."""
        super().__init__(*args, **kwargs)
        if instance:
            self.fields["s3_backup_enabled"].initial = instance.s3_backup_enabled
            self.fields["s3_backup_schedule"].initial = instance.s3_backup_schedule
            self.fields["s3_backup_time"].initial = instance.s3_backup_time
            self.fields["s3_backup_day"].initial = instance.s3_backup_day
            self.fields["s3_backup_prefix"].initial = instance.s3_backup_prefix or "pyrunner-backups/"
            self.fields["s3_backup_retention_count"].initial = instance.s3_backup_retention_count
            self.fields["s3_backup_include_runs"].initial = instance.s3_backup_include_runs
            self.fields["s3_backup_max_runs"].initial = instance.s3_backup_max_runs
            self.fields["s3_backup_include_datastores"].initial = instance.s3_backup_include_datastores

    def save(self, instance):
        """Save the backup schedule settings."""
        from core.services.backup_schedule_service import BackupScheduleService

        instance.s3_backup_enabled = self.cleaned_data.get("s3_backup_enabled", False)
        instance.s3_backup_schedule = self.cleaned_data.get("s3_backup_schedule")
        instance.s3_backup_time = self.cleaned_data.get("s3_backup_time")
        instance.s3_backup_day = int(self.cleaned_data.get("s3_backup_day", 0))
        instance.s3_backup_prefix = self.cleaned_data.get("s3_backup_prefix") or "pyrunner-backups/"
        instance.s3_backup_retention_count = self.cleaned_data.get("s3_backup_retention_count", 7)
        instance.s3_backup_include_runs = self.cleaned_data.get("s3_backup_include_runs", False)
        instance.s3_backup_max_runs = self.cleaned_data.get("s3_backup_max_runs", 1000)
        instance.s3_backup_include_datastores = self.cleaned_data.get("s3_backup_include_datastores", True)

        instance.save()

        # Sync the django-q2 schedule
        BackupScheduleService.sync_schedule()

        return instance


class ChannelForm(forms.Form):
    """Create / edit a chat Channel (Channels subsystem; Phase 1 = Telegram).

    A plain Form (like StorageConnectionForm) because credentials are encrypted +
    fingerprinted on save. Provider choices are limited to *registered* providers,
    so the picker grows automatically as providers are added.
    """

    name = forms.CharField(
        max_length=120,
        widget=forms.TextInput(
            attrs={"class": INPUT_CLASS, "placeholder": "Ops Alerts"}
        ),
        help_text="A label for this connection.",
    )
    provider = forms.ChoiceField(
        choices=[],  # populated in __init__ from registered providers
        widget=forms.Select(attrs={"class": INPUT_CLASS}),
    )
    bot_token = forms.CharField(
        required=False,
        widget=forms.PasswordInput(
            attrs={
                "class": INPUT_CLASS + " font-mono",
                "placeholder": "Leave blank to keep current",
                "autocomplete": "new-password",
            }
        ),
        label="Bot token",
        help_text="Telegram: the token from @BotFather (e.g. 123456:ABC-DEF...).",
    )
    default_target = forms.CharField(
        required=False,
        widget=forms.TextInput(
            attrs={"class": INPUT_CLASS + " font-mono", "placeholder": "e.g. 123456789"}
        ),
        label="Default chat ID",
        help_text="Where notifications are sent. Use 'Find chat ID' after saving.",
    )
    enabled = forms.BooleanField(
        required=False,
        initial=True,
        widget=forms.CheckboxInput(attrs={"class": CHECK_CLASS}),
    )

    def __init__(self, *args, instance=None, workspace=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance = instance
        self.workspace = workspace

        from core.models import Channel
        from core.services.channels import list_providers

        registered = set(list_providers())
        self.fields["provider"].choices = [
            (value, label)
            for value, label in Channel.Provider.choices
            if value in registered
        ]

        if instance is not None:
            self.fields["name"].initial = instance.name
            self.fields["provider"].initial = instance.provider
            self.fields["default_target"].initial = instance.default_target
            self.fields["enabled"].initial = instance.enabled
            # Provider is fixed on edit — changing it would orphan the credentials.
            self.fields["provider"].disabled = True

    def clean_name(self):
        from core.models import Channel

        name = (self.cleaned_data.get("name") or "").strip()
        qs = Channel.objects.filter(workspace=self.workspace, name=name)
        if self.instance is not None:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError("A channel with this name already exists.")
        return name

    def clean(self):
        cleaned = super().clean()
        bot_token = (cleaned.get("bot_token") or "").strip()

        # Bot token is required on create, optional on edit (blank = keep current).
        if self.instance is None and not bot_token:
            self.add_error("bot_token", "A bot token is required.")
            return cleaned

        if bot_token:
            from core.models import Channel
            from core.services.channels import get_provider

            provider_key = self.instance.provider if self.instance else cleaned.get("provider")
            if provider_key:
                identity = get_provider(provider_key).identity_for_fingerprint(
                    {"bot_token": bot_token}
                )
                fingerprint = Channel.fingerprint_for(provider_key, identity)
                if fingerprint:
                    clash = Channel.objects.filter(creds_fingerprint=fingerprint)
                    if self.instance is not None:
                        clash = clash.exclude(pk=self.instance.pk)
                    if clash.exists():
                        self.add_error(
                            "bot_token",
                            "This bot is already connected as another channel "
                            "(one bot = one channel).",
                        )
        return cleaned

    def save(self, *, created_by=None):
        from core.models import Channel
        from core.services.channels import get_provider

        instance = self.instance or Channel(
            workspace=self.workspace, created_by=created_by
        )
        if self.instance is None:
            instance.provider = self.cleaned_data["provider"]
        instance.name = self.cleaned_data["name"]
        instance.enabled = self.cleaned_data.get("enabled", False)

        config = dict(instance.config or {})
        config["default_target"] = (self.cleaned_data.get("default_target") or "").strip()
        instance.config = config

        bot_token = (self.cleaned_data.get("bot_token") or "").strip()
        if bot_token:
            creds = {"bot_token": bot_token}
            identity = get_provider(instance.provider).identity_for_fingerprint(creds)
            instance.set_credentials(creds, identity=identity)

        instance.save()
        return instance


class ChannelInboundForm(forms.Form):
    """Configure a channel's inbound handler + approval/cap settings.

    Both inbound handlers are available: ``script`` (run a script) and ``pyai``
    (the read-only Py AI assistant).
    """

    inbound_enabled = forms.BooleanField(
        required=False, widget=forms.CheckboxInput(attrs={"class": CHECK_CLASS})
    )
    inbound_handler = forms.ChoiceField(
        required=False,
        choices=[("", "Notify only (no inbound handling)"), ("script", "Run a script")],
        widget=forms.Select(attrs={"class": INPUT_CLASS}),
        label="When a message arrives",
    )
    inbound_target_id = forms.ChoiceField(
        required=False,
        widget=forms.Select(attrs={"class": INPUT_CLASS}),
        label="Script to run",
    )
    inbound_access = forms.ChoiceField(
        choices=[
            ("approval", "Approval inbox (recommended) — only approved senders"),
            ("open", "Open — anyone who passes signature verification"),
        ],
        widget=forms.Select(attrs={"class": INPUT_CLASS}),
        label="Who can use it",
    )
    daily_reply_cap = forms.IntegerField(
        required=False,
        min_value=0,
        widget=forms.NumberInput(attrs={"class": INPUT_CLASS, "min": 0}),
        label="Daily reply cap",
        help_text="Max handler replies per day (0 = unlimited).",
    )

    def __init__(self, *args, channel=None, workspace=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.channel = channel

        from core.models import GlobalSettings, Script

        # Offer Py AI as a handler only when it's enabled.
        if GlobalSettings.get_settings().pyai_enabled:
            self.fields["inbound_handler"].choices = self.fields["inbound_handler"].choices + [
                ("pyai", "Ask Py AI")
            ]

        scripts = (
            Script.objects.for_workspace(workspace)
            .filter(archived_at__isnull=True)
            .order_by("name")
        )
        self.fields["inbound_target_id"].choices = [("", "— select a script —")] + [
            (str(s.id), s.name) for s in scripts
        ]

        if channel is not None and not self.is_bound:
            self.fields["inbound_enabled"].initial = channel.inbound_enabled
            self.fields["inbound_handler"].initial = channel.inbound_handler
            self.fields["inbound_target_id"].initial = (
                str(channel.inbound_target_id) if channel.inbound_target_id else ""
            )
            self.fields["inbound_access"].initial = channel.inbound_access
            self.fields["daily_reply_cap"].initial = channel.daily_reply_cap

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("inbound_enabled") and cleaned.get("inbound_handler") == "script":
            if not cleaned.get("inbound_target_id"):
                self.add_error("inbound_target_id", "Choose a script to run.")
        return cleaned


class PyAISettingsForm(forms.Form):
    """Configure the built-in Py AI assistant (instance-global, superuser-only)."""

    pyai_enabled = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={"class": CHECK_CLASS}),
        label="Enable Py AI",
    )
    pyai_model = forms.CharField(
        required=False,
        max_length=100,
        widget=forms.TextInput(
            attrs={"class": INPUT_CLASS, "placeholder": "claude-sonnet-4-6 (optional)"}
        ),
        label="Model",
        help_text="Optional. Blank uses the Claude default model.",
    )
    pyai_system_prompt = forms.CharField(
        required=False,
        widget=forms.Textarea(
            attrs={"class": INPUT_CLASS, "rows": 3,
                   "placeholder": "Optional extra instruction for Py AI"}
        ),
        label="Extra system instruction",
    )

    def __init__(self, *args, instance=None, **kwargs):
        super().__init__(*args, **kwargs)
        if instance is not None and not self.is_bound:
            self.fields["pyai_enabled"].initial = instance.pyai_enabled
            self.fields["pyai_model"].initial = instance.pyai_model
            self.fields["pyai_system_prompt"].initial = instance.pyai_system_prompt

    def save(self, instance):
        instance.pyai_enabled = self.cleaned_data.get("pyai_enabled", False)
        instance.pyai_model = self.cleaned_data.get("pyai_model") or ""
        instance.pyai_system_prompt = self.cleaned_data.get("pyai_system_prompt") or ""
        instance.save(update_fields=["pyai_enabled", "pyai_model", "pyai_system_prompt", "updated_at"])
        return instance


class LibraryForm(forms.ModelForm):
    """Create/edit a Library's metadata (its CODE lives in revisions, not here).

    The ``key`` stays editable after creation, matching DataStore's name: both are
    identifiers scripts reference. The help text carries the warning, because
    renaming a key breaks every `from <key> import ...` already written.
    """

    def __init__(self, *args, workspace=None, **kwargs):
        # The active workspace scopes the key-uniqueness check (keys are unique
        # per workspace, not globally).
        self._workspace = workspace
        super().__init__(*args, **kwargs)

    class Meta:
        model = Library
        fields = ["key", "name", "description"]
        widgets = {
            "key": forms.TextInput(
                attrs={
                    "class": INPUT_CLASS + " font-mono",
                    "placeholder": "my_helpers",
                }
            ),
            "name": forms.TextInput(
                attrs={
                    "class": INPUT_CLASS,
                    "placeholder": "My shared helpers",
                }
            ),
            "description": forms.Textarea(
                attrs={
                    "class": INPUT_CLASS,
                    "rows": 2,
                    "placeholder": "What does this library provide?",
                }
            ),
        }
        labels = {
            "key": "Import name",
            "name": "Display name",
            "description": "Description",
        }
        help_texts = {
            "key": "Scripts import it as: from <key>.module import thing. "
            "Changing this breaks imports in scripts already using it.",
        }

    def clean_key(self):
        from core.models.library import validate_library_key

        key = self.cleaned_data.get("key", "").strip()
        # The shared validator the SDK also calls — identifier rules plus the
        # pyrunner*/stdlib reserved bans, so form and SDK can never disagree.
        validate_library_key(key)

        qs = Library.objects.filter(key__iexact=key)
        if self._workspace is not None:
            qs = qs.filter(workspace=self._workspace)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError(
                "A library with this import name already exists in this workspace."
            )
        return key
