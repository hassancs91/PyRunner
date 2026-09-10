"""
General settings tests — the Settings → General card must DO what it says.

Regression for review 8.1: instance_name, timezone, date_format and
time_format were stored, validated, and backed up but consumed by nothing —
the header hardcoded "PyRun", emails hardcoded "[PyRun]", and all
datetimes rendered in UTC. Now instance_name drives the header/title/email
subjects, timezone drives display (TimezoneMiddleware) and scheduled-backup
times, and the two format fields are gone. Also covers review 6.3: the
timezone dropdown's visual separator used to be a submittable value.
"""

from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone as dj_timezone

from core.forms import GeneralSettingsForm
from core.middleware import TimezoneMiddleware
from core.models import Environment, GlobalSettings, User, Workspace, WorkspaceMembership
from core.services.notification_service import NotificationService


class TimezoneMiddlewareTests(TestCase):
    def tearDown(self):
        dj_timezone.deactivate()

    def _active_tz_during_request(self) -> str:
        captured = {}

        def get_response(request):
            captured["tz"] = dj_timezone.get_current_timezone_name()
            return None

        TimezoneMiddleware(get_response)(RequestFactory().get("/"))
        return captured["tz"]

    def test_instance_timezone_is_activated(self):
        gs = GlobalSettings.get_settings()
        gs.timezone = "Asia/Tokyo"
        gs.save()
        self.assertEqual(self._active_tz_during_request(), "Asia/Tokyo")

    def test_bad_timezone_falls_back_to_utc(self):
        gs = GlobalSettings.get_settings()
        gs.timezone = "Not/AZone"
        gs.save()
        self.assertEqual(self._active_tz_during_request(), "UTC")


class InstanceNameTests(TestCase):
    def test_header_shows_instance_name(self):
        gs = GlobalSettings.get_settings()
        gs.instance_name = "Acme Ops"
        gs.setup_completed = True
        gs.save()
        Environment.objects.get_or_create(
            name="default", defaults={"is_default": True, "python_version": "3.12"}
        )
        admin = User.objects.create(
            email="admin@example.com", is_superuser=True, is_staff=True
        )
        WorkspaceMembership.ensure(
            admin, Workspace.get_default(), role=WorkspaceMembership.ROLE_OWNER
        )
        self.client.force_login(admin)

        resp = self.client.get(reverse("cpanel:services"))

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Acme Ops")

    def test_email_subject_uses_instance_name(self):
        gs = GlobalSettings.get_settings()
        gs.instance_name = "Acme Ops"
        gs.save()
        self.assertEqual(NotificationService._subject("Test Email"), "[Acme Ops] Test Email")

    def test_email_subject_falls_back_to_product_name(self):
        gs = GlobalSettings.get_settings()
        gs.instance_name = ""
        gs.save()
        self.assertEqual(NotificationService._subject("Test Email"), "[PyRun] Test Email")


class TimezoneChoicesTests(TestCase):
    """The dropdown separator must not be a submittable value (review 6.3)."""

    def test_separator_is_not_a_valid_choice(self):
        form = GeneralSettingsForm(data={"timezone": "---"})
        self.assertFalse(form.is_valid())
        self.assertIn("timezone", form.errors)

    def test_real_timezone_validates_and_saves(self):
        form = GeneralSettingsForm(
            data={"timezone": "Asia/Tokyo", "instance_name": "Acme Ops"}
        )
        self.assertTrue(form.is_valid(), form.errors)
        gs = form.save(GlobalSettings.get_settings())
        self.assertEqual(gs.timezone, "Asia/Tokyo")
        self.assertEqual(gs.instance_name, "Acme Ops")
