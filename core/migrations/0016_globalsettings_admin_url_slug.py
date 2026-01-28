from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0015_datastore'),
    ]

    operations = [
        migrations.AddField(
            model_name='globalsettings',
            name='admin_url_slug',
            field=models.CharField(
                default='django-admin',
                help_text='URL path for Django admin interface (requires restart)',
                max_length=100,
            ),
        ),
    ]
