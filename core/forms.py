"""
Forms for the core app.
"""
import re
from zoneinfo import available_timezones

from django import forms
from django.utils.text import slugify

from core.models import Script, Environment, ScriptSchedule, Tag, DataStore, DataStoreEntry, DataStoreAPIToken
from core.services import EnvironmentService


# Regex pattern for secret key validation (uppercase, numbers, underscores, must start with letter)
SECRET_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")


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
    """Generate timezone choices with common ones first."""
    all_tz = sorted(available_timezones())
    common = [(tz, tz) for tz in COMMON_TIMEZONES if tz in all_tz]
    others = [(tz, tz) for tz in all_tz if tz not in COMMON_TIMEZONES]
    return [("", "---")] + common + [("---", "─" * 20)] + others


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
            "notify_on",
            "notify_email",
            "notify_webhook_url",
            "notify_webhook_enabled",
        ]
        widgets = {
            "name": forms.TextInput(
                attrs={
                    "class": "w-full px-4 py-3 bg-code-bg border border-code-border rounded-lg text-code-text placeholder-code-muted/50 focus:outline-none focus:ring-2 focus:ring-code-accent/50 focus:border-code-accent",
                    "placeholder": "My Script Name",
                }
            ),
            "description": forms.Textarea(
                attrs={
                    "class": "w-full px-4 py-3 bg-code-bg border border-code-border rounded-lg text-code-text placeholder-code-muted/50 focus:outline-none focus:ring-2 focus:ring-code-accent/50 focus:border-code-accent",
                    "rows": 2,
                    "placeholder": "What does this script do?",
                }
            ),
            "code": forms.Textarea(
                attrs={
                    "class": "w-full px-4 py-3 bg-code-bg border border-code-border rounded-lg text-code-text font-mono text-sm placeholder-code-muted/50 focus:outline-none focus:ring-2 focus:ring-code-accent/50 focus:border-code-accent",
                    "rows": 15,
                    "placeholder": '# Your Python code here\nprint("Hello, World!")',
                }
            ),
            "environment": forms.Select(
                attrs={
                    "class": "w-full px-4 py-3 bg-code-bg border border-code-border rounded-lg text-code-text focus:outline-none focus:ring-2 focus:ring-code-accent/50 focus:border-code-accent",
                }
            ),
            "tags": forms.CheckboxSelectMultiple(
                attrs={
                    "class": "tag-checkbox",
                }
            ),
            "timeout_seconds": forms.NumberInput(
                attrs={
                    "class": "w-full px-4 py-3 bg-code-bg border border-code-border rounded-lg text-code-text focus:outline-none focus:ring-2 focus:ring-code-accent/50 focus:border-code-accent",
                    "min": 1,
                    "max": 3600,
                }
            ),
            "is_enabled": forms.CheckboxInput(
                attrs={
                    "class": "w-5 h-5 text-code-accent bg-code-bg border-code-border rounded focus:ring-code-accent focus:ring-2",
                }
            ),
            "notify_on": forms.Select(
                attrs={
                    "class": "w-full px-4 py-3 bg-code-bg border border-code-border rounded-lg text-code-text focus:outline-none focus:ring-2 focus:ring-code-accent/50 focus:border-code-accent",
                }
            ),
            "notify_email": forms.EmailInput(
                attrs={
                    "class": "w-full px-4 py-3 bg-code-bg border border-code-border rounded-lg text-code-text placeholder-code-muted/50 focus:outline-none focus:ring-2 focus:ring-code-accent/50 focus:border-code-accent",
                    "placeholder": "Override default email (optional)",
                }
            ),
            "notify_webhook_url": forms.URLInput(
                attrs={
                    "class": "w-full px-4 py-3 bg-code-bg border border-code-border rounded-lg text-code-text placeholder-code-muted/50 focus:outline-none focus:ring-2 focus:ring-code-accent/50 focus:border-code-accent",
                    "placeholder": "https://your-service.com/webhook",
                }
            ),
            "notify_webhook_enabled": forms.CheckboxInput(
                attrs={
                    "class": "w-5 h-5 text-code-accent bg-code-bg border-code-border rounded focus:ring-code-accent focus:ring-2",
                }
            ),
        }
        labels = {
            "name": "Script Name",
            "description": "Description",
            "code": "Python Code",
            "environment": "Environment",
            "tags": "Tags",
            "timeout_seconds": "Timeout (seconds)",
            "is_enabled": "Enabled",
            "notify_on": "Notify On",
            "notify_email": "Notification Email",
            "notify_webhook_url": "Webhook URL",
            "notify_webhook_enabled": "Enable Webhook",
        }
        help_texts = {
            "timeout_seconds": "Maximum execution time (1-3600 seconds)",
            "notify_email": "Leave empty to use global default",
            "notify_webhook_url": "URL to POST notifications to when script completes",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Only show active environments
        self.fields["environment"].queryset = Environment.objects.filter(is_active=True)

    def clean_code(self):
        code = self.cleaned_data.get("code", "").strip()
        if not code:
            raise forms.ValidationError("Script code cannot be empty.")
        return code

    def clean_timeout_seconds(self):
        timeout = self.cleaned_data.get("timeout_seconds")
        if timeout is not None and (timeout < 1 or timeout > 3600):
            raise forms.ValidationError("Timeout must be between 1 and 3600 seconds.")
        return timeout


class TagForm(forms.ModelForm):
    """Form for creating and editing tags."""

    class Meta:
        model = Tag
        fields = ["name", "color"]
        widgets = {
            "name": forms.TextInput(
                attrs={
                    "class": "w-full px-4 py-3 bg-code-bg border border-code-border rounded-lg text-code-text placeholder-code-muted/50 focus:outline-none focus:ring-2 focus:ring-code-accent/50 focus:border-code-accent",
                    "placeholder": "Tag name",
                }
            ),
            "color": forms.Select(
                attrs={
                    "class": "w-full px-4 py-3 bg-code-bg border border-code-border rounded-lg text-code-text focus:outline-none focus:ring-2 focus:ring-code-accent/50 focus:border-code-accent",
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

    # Custom field for daily times (comma-separated input)
    daily_times_input = forms.CharField(
        required=False,
        widget=forms.TextInput(
            attrs={
                "class": "w-full px-4 py-3 bg-code-bg border border-code-border rounded-lg text-code-text placeholder-code-muted/50 focus:outline-none focus:ring-2 focus:ring-code-accent/50",
                "placeholder": "09:00, 18:00",
            }
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
            attrs={
                "class": "w-full px-4 py-3 bg-code-bg border border-code-border rounded-lg text-code-text placeholder-code-muted/50 focus:outline-none focus:ring-2 focus:ring-code-accent/50",
                "placeholder": "09:00, 18:00",
            }
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
            attrs={
                "class": "w-full px-4 py-3 bg-code-bg border border-code-border rounded-lg text-code-text placeholder-code-muted/50 focus:outline-none focus:ring-2 focus:ring-code-accent/50",
                "placeholder": "09:00, 18:00",
            }
        ),
        label="Run Times",
        help_text="Comma-separated times in HH:MM format (24-hour)",
    )

    timezone = forms.ChoiceField(
        choices=get_timezone_choices,
        initial="UTC",
        widget=forms.Select(
            attrs={
                "class": "w-full px-4 py-3 bg-code-bg border border-code-border rounded-lg text-code-text focus:outline-none focus:ring-2 focus:ring-code-accent/50",
            }
        ),
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
            "interval_minutes": forms.Select(
                attrs={
                    "class": "w-full px-4 py-3 bg-code-bg border border-code-border rounded-lg text-code-text focus:outline-none focus:ring-2 focus:ring-code-accent/50",
                }
            ),
            "is_active": forms.CheckboxInput(
                attrs={
                    "class": "w-5 h-5 text-code-accent bg-code-bg border-code-border rounded focus:ring-code-accent focus:ring-2",
                }
            ),
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

        if commit:
            instance.save()

        return instance


class EnvironmentCreateForm(forms.ModelForm):
    """Form for creating a new environment."""

    python_path = forms.ChoiceField(
        choices=[],
        widget=forms.Select(
            attrs={
                "class": "w-full px-4 py-3 bg-code-bg border border-code-border rounded-lg text-code-text focus:outline-none focus:ring-2 focus:ring-code-accent/50 focus:border-code-accent",
            }
        ),
        label="Python Version",
        help_text="Select Python installation to use for this environment",
    )

    class Meta:
        model = Environment
        fields = ["name", "description"]
        widgets = {
            "name": forms.TextInput(
                attrs={
                    "class": "w-full px-4 py-3 bg-code-bg border border-code-border rounded-lg text-code-text placeholder-code-muted/50 focus:outline-none focus:ring-2 focus:ring-code-accent/50 focus:border-code-accent",
                    "placeholder": "My Environment",
                }
            ),
            "description": forms.Textarea(
                attrs={
                    "class": "w-full px-4 py-3 bg-code-bg border border-code-border rounded-lg text-code-text placeholder-code-muted/50 focus:outline-none focus:ring-2 focus:ring-code-accent/50 focus:border-code-accent",
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
                attrs={
                    "class": "w-full px-4 py-3 bg-code-bg border border-code-border rounded-lg text-code-text placeholder-code-muted/50 focus:outline-none focus:ring-2 focus:ring-code-accent/50 focus:border-code-accent",
                    "placeholder": "My Environment",
                }
            ),
            "description": forms.Textarea(
                attrs={
                    "class": "w-full px-4 py-3 bg-code-bg border border-code-border rounded-lg text-code-text placeholder-code-muted/50 focus:outline-none focus:ring-2 focus:ring-code-accent/50 focus:border-code-accent",
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
            attrs={
                "class": "w-full px-4 py-3 bg-code-bg border border-code-border rounded-lg text-code-text font-mono placeholder-code-muted/50 focus:outline-none focus:ring-2 focus:ring-code-accent/50 focus:border-code-accent",
                "placeholder": "requests==2.31.0",
            }
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
                "class": "w-full px-4 py-3 bg-code-bg border border-code-border rounded-lg text-code-text font-mono text-sm placeholder-code-muted/50 focus:outline-none focus:ring-2 focus:ring-code-accent/50 focus:border-code-accent",
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
        widget=forms.FileInput(
            attrs={
                "class": "block w-full text-sm text-code-muted file:mr-4 file:py-2 file:px-4 file:rounded-lg file:border-0 file:text-sm file:font-semibold file:bg-code-accent file:text-white hover:file:bg-code-accent/90 cursor-pointer",
                "accept": ".txt",
            }
        ),
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
            if not line or line.startswith("#") or line.startswith("-"):
                continue
            # Extract package spec (first word before any whitespace)
            pkg_spec = line.split()[0] if line.split() else ""
            if pkg_spec and not EnvironmentService.validate_package_spec(pkg_spec):
                raise forms.ValidationError(f"Invalid package specification: {line}")

        return cleaned_data


class SecretCreateForm(forms.Form):
    """Form for creating a new secret."""

    key = forms.CharField(
        max_length=100,
        widget=forms.TextInput(
            attrs={
                "class": "w-full px-4 py-3 bg-code-bg border border-code-border rounded-lg text-code-text font-mono placeholder-code-muted/50 focus:outline-none focus:ring-2 focus:ring-code-accent/50 focus:border-code-accent uppercase",
                "placeholder": "API_KEY",
                "autocomplete": "off",
            }
        ),
        label="Key Name",
        help_text="Uppercase letters, numbers, and underscores only. Must start with a letter.",
    )

    value = forms.CharField(
        widget=forms.Textarea(
            attrs={
                "class": "w-full px-4 py-3 bg-code-bg border border-code-border rounded-lg text-code-text font-mono placeholder-code-muted/50 focus:outline-none focus:ring-2 focus:ring-code-accent/50 focus:border-code-accent",
                "rows": 3,
                "placeholder": "sk-your-secret-value-here",
                "autocomplete": "off",
            }
        ),
        label="Secret Value",
        help_text="The secret value (will be encrypted at rest)",
    )

    description = forms.CharField(
        required=False,
        widget=forms.Textarea(
            attrs={
                "class": "w-full px-4 py-3 bg-code-bg border border-code-border rounded-lg text-code-text placeholder-code-muted/50 focus:outline-none focus:ring-2 focus:ring-code-accent/50 focus:border-code-accent",
                "rows": 2,
                "placeholder": "What is this secret used for?",
            }
        ),
        label="Description",
        help_text="Optional description to help remember what this secret is for",
    )

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

        # Check if key already exists
        from core.models import Secret

        if Secret.objects.filter(key=key).exists():
            raise forms.ValidationError(f"A secret with key '{key}' already exists.")

        return key

    def clean_value(self):
        """Validate the secret value."""
        value = self.cleaned_data.get("value", "")

        if not value:
            raise forms.ValidationError("Secret value is required.")

        # Reasonable max length for secrets
        if len(value) > 10000:
            raise forms.ValidationError(
                "Secret value is too long (max 10,000 characters)."
            )

        return value


class SecretEditForm(forms.Form):
    """Form for editing an existing secret (value and description only)."""

    value = forms.CharField(
        required=False,
        widget=forms.Textarea(
            attrs={
                "class": "w-full px-4 py-3 bg-code-bg border border-code-border rounded-lg text-code-text font-mono placeholder-code-muted/50 focus:outline-none focus:ring-2 focus:ring-code-accent/50 focus:border-code-accent",
                "rows": 3,
                "placeholder": "Leave blank to keep current value",
                "autocomplete": "off",
            }
        ),
        label="New Secret Value",
        help_text="Leave blank to keep the current value",
    )

    description = forms.CharField(
        required=False,
        widget=forms.Textarea(
            attrs={
                "class": "w-full px-4 py-3 bg-code-bg border border-code-border rounded-lg text-code-text placeholder-code-muted/50 focus:outline-none focus:ring-2 focus:ring-code-accent/50 focus:border-code-accent",
                "rows": 2,
                "placeholder": "What is this secret used for?",
            }
        ),
        label="Description",
    )

    def clean_value(self):
        """Validate the secret value if provided."""
        value = self.cleaned_data.get("value", "")

        if value and len(value) > 10000:
            raise forms.ValidationError(
                "Secret value is too long (max 10,000 characters)."
            )

        return value


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
                "class": "w-full px-4 py-3 bg-code-bg border border-code-border rounded-lg text-code-text placeholder-code-muted/50 focus:outline-none focus:ring-2 focus:ring-code-accent/50 focus:border-code-accent",
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
                "class": "w-full px-4 py-3 bg-code-bg border border-code-border rounded-lg text-code-text focus:outline-none focus:ring-2 focus:ring-code-accent/50 focus:border-code-accent",
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
                "class": "w-full px-4 py-3 bg-code-bg border border-code-border rounded-lg text-code-text placeholder-code-muted/50 focus:outline-none focus:ring-2 focus:ring-code-accent/50 focus:border-code-accent",
                "placeholder": "username@example.com",
            }
        ),
        label="SMTP Username",
    )

    smtp_password = forms.CharField(
        required=False,
        widget=forms.PasswordInput(
            attrs={
                "class": "w-full px-4 py-3 bg-code-bg border border-code-border rounded-lg text-code-text placeholder-code-muted/50 focus:outline-none focus:ring-2 focus:ring-code-accent/50 focus:border-code-accent",
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
                "class": "w-5 h-5 text-code-accent bg-code-bg border-code-border rounded focus:ring-code-accent focus:ring-2",
            }
        ),
        label="Use TLS",
    )

    smtp_from_email = forms.EmailField(
        required=False,
        widget=forms.EmailInput(
            attrs={
                "class": "w-full px-4 py-3 bg-code-bg border border-code-border rounded-lg text-code-text placeholder-code-muted/50 focus:outline-none focus:ring-2 focus:ring-code-accent/50 focus:border-code-accent",
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
                "class": "w-full px-4 py-3 bg-code-bg border border-code-border rounded-lg text-code-text placeholder-code-muted/50 focus:outline-none focus:ring-2 focus:ring-code-accent/50 focus:border-code-accent",
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
                "class": "w-full px-4 py-3 bg-code-bg border border-code-border rounded-lg text-code-text placeholder-code-muted/50 focus:outline-none focus:ring-2 focus:ring-code-accent/50 focus:border-code-accent",
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
                "class": "w-full px-4 py-3 bg-code-bg border border-code-border rounded-lg text-code-text placeholder-code-muted/50 focus:outline-none focus:ring-2 focus:ring-code-accent/50 focus:border-code-accent",
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

    from core.models import GlobalSettings

    DATE_FORMAT_CHOICES = [
        (GlobalSettings.DateFormat.ISO, "YYYY-MM-DD (ISO)"),
        (GlobalSettings.DateFormat.US, "MM/DD/YYYY (US)"),
        (GlobalSettings.DateFormat.EU, "DD/MM/YYYY (EU)"),
        (GlobalSettings.DateFormat.DOT, "DD.MM.YYYY"),
    ]

    TIME_FORMAT_CHOICES = [
        (GlobalSettings.TimeFormat.H24, "24-hour (14:30)"),
        (GlobalSettings.TimeFormat.H12, "12-hour (2:30 PM)"),
    ]

    instance_name = forms.CharField(
        max_length=100,
        required=False,
        widget=forms.TextInput(
            attrs={
                "class": "w-full px-4 py-3 bg-code-bg border border-code-border rounded-lg text-code-text placeholder-code-muted/50 focus:outline-none focus:ring-2 focus:ring-code-accent/50 focus:border-code-accent",
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
                "class": "w-full px-4 py-3 bg-code-bg border border-code-border rounded-lg text-code-text focus:outline-none focus:ring-2 focus:ring-code-accent/50 focus:border-code-accent",
            }
        ),
        label="Timezone",
        help_text="Default timezone for displaying dates and times",
    )

    date_format = forms.ChoiceField(
        choices=DATE_FORMAT_CHOICES,
        widget=forms.Select(
            attrs={
                "class": "w-full px-4 py-3 bg-code-bg border border-code-border rounded-lg text-code-text focus:outline-none focus:ring-2 focus:ring-code-accent/50 focus:border-code-accent",
            }
        ),
        label="Date Format",
    )

    time_format = forms.ChoiceField(
        choices=TIME_FORMAT_CHOICES,
        widget=forms.Select(
            attrs={
                "class": "w-full px-4 py-3 bg-code-bg border border-code-border rounded-lg text-code-text focus:outline-none focus:ring-2 focus:ring-code-accent/50 focus:border-code-accent",
            }
        ),
        label="Time Format",
    )

    admin_url_slug = forms.CharField(
        max_length=100,
        required=False,
        widget=forms.TextInput(
            attrs={
                "class": "w-full px-4 py-3 bg-code-bg border border-code-border rounded-lg text-code-text placeholder-code-muted/50 focus:outline-none focus:ring-2 focus:ring-code-accent/50 focus:border-code-accent",
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
            self.fields["date_format"].initial = instance.date_format
            self.fields["time_format"].initial = instance.time_format
            self.fields["admin_url_slug"].initial = instance.admin_url_slug

    def save(self, instance):
        """Save the general settings to the GlobalSettings instance."""
        instance.instance_name = self.cleaned_data.get("instance_name") or "PyRunner"
        instance.timezone = self.cleaned_data.get("timezone") or "UTC"
        instance.date_format = self.cleaned_data.get("date_format")
        instance.time_format = self.cleaned_data.get("time_format")
        instance.admin_url_slug = self.cleaned_data.get("admin_url_slug") or "django-admin"
        instance.save(update_fields=[
            "instance_name", "timezone", "date_format", "time_format", "admin_url_slug", "updated_at"
        ])
        return instance


class LogRetentionForm(forms.Form):
    """Form for log retention settings."""

    retention_days = forms.IntegerField(
        min_value=0,
        initial=0,
        widget=forms.NumberInput(
            attrs={
                "class": "w-full px-4 py-3 bg-code-bg border border-code-border rounded-lg text-code-text focus:outline-none focus:ring-2 focus:ring-code-accent/50 focus:border-code-accent",
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
                "class": "w-full px-4 py-3 bg-code-bg border border-code-border rounded-lg text-code-text focus:outline-none focus:ring-2 focus:ring-code-accent/50 focus:border-code-accent",
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
                "class": "w-5 h-5 text-code-accent bg-code-bg border-code-border rounded focus:ring-code-accent focus:ring-2",
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
                "class": "w-full px-4 py-3 bg-code-bg border border-code-border rounded-lg text-code-text focus:outline-none focus:ring-2 focus:ring-code-accent/50 focus:border-code-accent",
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
                "class": "w-full px-4 py-3 bg-code-bg border border-code-border rounded-lg text-code-text focus:outline-none focus:ring-2 focus:ring-code-accent/50 focus:border-code-accent",
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
                "class": "w-full px-4 py-3 bg-code-bg border border-code-border rounded-lg text-code-text focus:outline-none focus:ring-2 focus:ring-code-accent/50 focus:border-code-accent",
                "min": 60,
                "max": 86400,
            }
        ),
        label="Retry Delay (seconds)",
        help_text="Time before retrying a failed/timed-out task. Should be greater than timeout.",
    )

    q_queue_limit = forms.IntegerField(
        min_value=5,
        max_value=100,
        initial=20,
        widget=forms.NumberInput(
            attrs={
                "class": "w-full px-4 py-3 bg-code-bg border border-code-border rounded-lg text-code-text focus:outline-none focus:ring-2 focus:ring-code-accent/50 focus:border-code-accent",
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


class BackupCreateForm(forms.Form):
    """Form for configuring backup creation."""

    include_runs = forms.BooleanField(
        required=False,
        initial=True,
        widget=forms.CheckboxInput(
            attrs={
                "class": "w-5 h-5 text-code-accent bg-code-bg border-code-border rounded focus:ring-code-accent focus:ring-2",
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
                "class": "w-full px-4 py-2 bg-code-bg text-code-text border border-code-border rounded-lg focus:ring-2 focus:ring-code-accent focus:border-transparent",
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
                "class": "w-5 h-5 text-code-accent bg-code-bg border-code-border rounded focus:ring-code-accent focus:ring-2",
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
                "class": "block w-full text-sm text-code-text file:mr-4 file:py-2 file:px-4 file:rounded-lg file:border-0 file:text-sm file:font-semibold file:bg-code-accent file:text-white hover:file:bg-opacity-90",
                "accept": ".json",
            }
        ),
        label="Backup file",
        help_text="JSON backup file (.json)",
    )

    restore_runs = forms.BooleanField(
        required=False,
        initial=True,
        widget=forms.CheckboxInput(
            attrs={
                "class": "w-5 h-5 text-code-accent bg-code-bg border-code-border rounded focus:ring-code-accent focus:ring-2",
            }
        ),
        label="Restore run history",
        help_text="Import execution history from backup",
    )

    confirm_delete = forms.BooleanField(
        required=True,
        widget=forms.CheckboxInput(
            attrs={
                "class": "w-5 h-5 text-red-500 bg-code-bg border-code-border rounded focus:ring-red-500 focus:ring-2",
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

    class Meta:
        model = DataStore
        fields = ["name", "description"]
        widgets = {
            "name": forms.TextInput(
                attrs={
                    "class": "w-full px-4 py-3 bg-code-bg border border-code-border rounded-lg text-code-text placeholder-code-muted/50 focus:outline-none focus:ring-2 focus:ring-code-accent/50 focus:border-code-accent",
                    "placeholder": "my_data_store",
                }
            ),
            "description": forms.Textarea(
                attrs={
                    "class": "w-full px-4 py-3 bg-code-bg border border-code-border rounded-lg text-code-text placeholder-code-muted/50 focus:outline-none focus:ring-2 focus:ring-code-accent/50 focus:border-code-accent",
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
        # Check uniqueness (excluding current instance for edits)
        qs = DataStore.objects.filter(name__iexact=name)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError("A data store with this name already exists.")
        return name


class DataStoreEntryForm(forms.Form):
    """Form for creating and editing data store entries."""

    key = forms.CharField(
        max_length=255,
        widget=forms.TextInput(
            attrs={
                "class": "w-full px-4 py-3 bg-code-bg border border-code-border rounded-lg text-code-text font-mono placeholder-code-muted/50 focus:outline-none focus:ring-2 focus:ring-code-accent/50 focus:border-code-accent",
                "placeholder": "my_key",
            }
        ),
        label="Key",
        help_text="Unique identifier for this entry",
    )

    value = forms.CharField(
        widget=forms.Textarea(
            attrs={
                "class": "w-full px-4 py-3 bg-code-bg border border-code-border rounded-lg text-code-text font-mono text-sm placeholder-code-muted/50 focus:outline-none focus:ring-2 focus:ring-code-accent/50 focus:border-code-accent",
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


class DataStoreAPITokenForm(forms.ModelForm):
    """Form for creating API tokens."""

    class Meta:
        model = DataStoreAPIToken
        fields = ["name", "datastore", "expires_at"]
        widgets = {
            "name": forms.TextInput(
                attrs={
                    "class": "w-full px-4 py-3 bg-code-bg border border-code-border rounded-lg text-code-text placeholder-code-muted/50 focus:outline-none focus:ring-2 focus:ring-code-accent/50 focus:border-code-accent",
                    "placeholder": "My Dashboard Token",
                }
            ),
            "datastore": forms.Select(
                attrs={
                    "class": "w-full px-4 py-3 bg-code-bg border border-code-border rounded-lg text-code-text focus:outline-none focus:ring-2 focus:ring-code-accent/50 focus:border-code-accent",
                }
            ),
            "expires_at": forms.DateTimeInput(
                attrs={
                    "class": "w-full px-4 py-3 bg-code-bg border border-code-border rounded-lg text-code-text focus:outline-none focus:ring-2 focus:ring-code-accent/50 focus:border-code-accent",
                    "type": "datetime-local",
                },
                format="%Y-%m-%dT%H:%M",
            ),
        }
        labels = {
            "name": "Token Name",
            "datastore": "Scope",
            "expires_at": "Expires At",
        }
        help_texts = {
            "name": "A friendly name to identify this token",
            "datastore": "Leave empty for access to all datastores, or select a specific datastore",
            "expires_at": "Optional. Leave empty for no expiration.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Make datastore optional with a clear empty choice
        self.fields["datastore"].required = False
        self.fields["datastore"].empty_label = "All Datastores (Global Access)"
        self.fields["expires_at"].required = False


# =============================================================================
# Authentication Forms
# =============================================================================

INPUT_CLASS = "w-full px-4 py-3 bg-code-bg border border-code-border rounded-lg text-code-text placeholder-code-muted/50 focus:outline-none focus:ring-2 focus:ring-code-accent/50 focus:border-code-accent"


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
    """Form for setting or changing password."""

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
        help_text="Minimum 8 characters",
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

    def clean(self):
        cleaned_data = super().clean()
        password = cleaned_data.get("password")
        confirm = cleaned_data.get("password_confirm")
        if password and confirm and password != confirm:
            raise forms.ValidationError("Passwords do not match.")
        return cleaned_data


class AdminSetupForm(forms.Form):
    """Form for initial admin setup with password."""

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
        help_text="Minimum 8 characters",
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
        cleaned_data = super().clean()
        password = cleaned_data.get("password")
        confirm = cleaned_data.get("password_confirm")
        if password and confirm and password != confirm:
            raise forms.ValidationError("Passwords do not match.")
        return cleaned_data
