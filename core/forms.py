"""
Forms for the core app.
"""
import re
from zoneinfo import available_timezones

from django import forms
from django.utils.text import slugify

from core.models import Script, Environment, ScriptSchedule
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


class ScheduleForm(forms.ModelForm):
    """Form for configuring script schedules."""

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

    def clean_daily_times_input(self):
        """Parse and validate daily times input."""
        value = self.cleaned_data.get("daily_times_input", "").strip()
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

        return cleaned_data

    def save(self, commit=True):
        instance = super().save(commit=False)

        # Set daily_times from parsed input
        instance.daily_times = self.cleaned_data.get("daily_times_input", [])

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
