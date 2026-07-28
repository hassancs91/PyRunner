"""
ScriptForm environment preselection.

``Script.environment`` is a required FK with no model default, so the create
form used to render on the empty "---------" choice even when a default
environment existed — every new script forced a manual pick. The form now
seeds ``initial["environment"]`` from the active default environment, on the
unbound create form only: bound submissions and edit forms (whose initial
comes from the instance) are untouched.
"""

from django.test import TestCase

from core.forms import ScriptForm
from core.models import Environment, Script


class ScriptFormEnvironmentPreselectTests(TestCase):
    def test_create_form_preselects_default_environment(self):
        Environment.objects.create(name="other", path="other", is_active=True)
        default = Environment.objects.create(
            name="default", path="default", is_active=True, is_default=True
        )
        form = ScriptForm()
        self.assertEqual(form.initial.get("environment"), default.pk)

    def test_create_form_without_default_environment_stays_empty(self):
        Environment.objects.create(name="only", path="only", is_active=True)
        form = ScriptForm()
        self.assertNotIn("environment", form.initial)

    def test_inactive_default_is_not_preselected(self):
        Environment.objects.create(
            name="retired", path="retired", is_active=False, is_default=True
        )
        form = ScriptForm()
        self.assertNotIn("environment", form.initial)

    def test_edit_form_keeps_instance_environment(self):
        Environment.objects.create(
            name="default", path="default", is_active=True, is_default=True
        )
        env = Environment.objects.create(name="mine", path="mine", is_active=True)
        script = Script.objects.create(
            name="s", code="print('x')", environment=env, timeout_seconds=60
        )
        form = ScriptForm(instance=script)
        self.assertEqual(form.initial.get("environment"), env.pk)

    def test_explicit_initial_wins_over_default(self):
        Environment.objects.create(
            name="default", path="default", is_active=True, is_default=True
        )
        env = Environment.objects.create(name="chosen", path="chosen", is_active=True)
        form = ScriptForm(initial={"environment": env.pk})
        self.assertEqual(form.initial.get("environment"), env.pk)
