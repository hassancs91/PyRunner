# Generated for Google reCAPTCHA v2 login protection

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0023_claude_usage"),
    ]

    operations = [
        migrations.AddField(
            model_name="globalsettings",
            name="recaptcha_enabled",
            field=models.BooleanField(
                default=False,
                help_text="Require Google reCAPTCHA v2 on the login page",
            ),
        ),
        migrations.AddField(
            model_name="globalsettings",
            name="recaptcha_site_key",
            field=models.CharField(
                blank=True,
                help_text="reCAPTCHA v2 site key (public)",
                max_length=255,
            ),
        ),
        migrations.AddField(
            model_name="globalsettings",
            name="recaptcha_secret_key_encrypted",
            field=models.TextField(
                blank=True,
                help_text="reCAPTCHA v2 secret key (encrypted)",
            ),
        ),
    ]
