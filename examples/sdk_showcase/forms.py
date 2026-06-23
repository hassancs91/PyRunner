"""
Setup form for the SDK Showcase plugin.

Plain ``forms.Form`` (no model) — the plugin persists everything through the SDK,
so there is nothing to ModelForm. Inputs reuse the console token classes so the
page matches the rest of PyRunner.
"""

from django import forms

INPUT_CLASS = (
    "w-full px-3.5 py-2.5 bg-ink border border-line rounded-lg text-text text-sm "
    "placeholder-faint/60 focus:outline-none focus:ring-2 focus:ring-ok/30 "
    "focus:border-ok/60 transition-colors"
)

# form-field name -> the clean env-var the secret injects under (one demo secret).
SECRET_FIELDS = {"demo_token": "DEMO_TOKEN"}


class SetupForm(forms.Form):
    environment = forms.ChoiceField(
        choices=[], widget=forms.Select(attrs={"class": INPUT_CLASS}),
        help_text="Any environment works — the demo worker uses only the standard library.",
    )
    demo_token = forms.CharField(
        label="Demo secret (DEMO_TOKEN)", required=False,
        widget=forms.TextInput(attrs={
            "class": INPUT_CLASS,
            "placeholder": "any value — stored encrypted, injected into the script",
        }),
        help_text="Stored as an owner-scoped Secret and injected into the demo script as $DEMO_TOKEN.",
    )
    message = forms.CharField(
        label="Demo message", required=False, initial="hello from the data store",
        widget=forms.TextInput(attrs={"class": INPUT_CLASS}),
        help_text="Non-secret config saved to the data store; the worker prints it.",
    )
    steps = forms.IntegerField(
        label="Worker steps", min_value=1, max_value=30, initial=5,
        widget=forms.NumberInput(attrs={"class": INPUT_CLASS}),
        help_text="How many heartbeat steps the worker loops — so you can watch progress and test Stop.",
    )

    def __init__(self, *args, environments=None, has_secret=False, **kwargs):
        super().__init__(*args, **kwargs)
        names = [e.name for e in (environments or [])]
        self.fields["environment"].choices = [(n, n) for n in names]
        if not names:
            self.fields["environment"].required = False
        # The secret is required only on first setup; once stored, blank keeps it.
        if has_secret:
            self.fields["demo_token"].widget.attrs["placeholder"] = "configured — leave blank to keep"
