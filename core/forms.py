"""
Forms for the core app.
"""
from django import forms
from core.models import Script, Environment


class ScriptForm(forms.ModelForm):
    """Form for creating and editing scripts."""

    class Meta:
        model = Script
        fields = ["name", "description", "code", "environment", "timeout_seconds", "is_enabled"]
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
        }
        labels = {
            "name": "Script Name",
            "description": "Description",
            "code": "Python Code",
            "environment": "Environment",
            "timeout_seconds": "Timeout (seconds)",
            "is_enabled": "Enabled",
        }
        help_texts = {
            "timeout_seconds": "Maximum execution time (1-3600 seconds)",
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
