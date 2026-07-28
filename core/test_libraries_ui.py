"""
Script Libraries — Stage 2 (console UI) tests.

Covers the browser flow the plan names as the acceptance bar — create → edit
(two revisions) → attach → run → view the stamped version → diff → restore —
plus the guards that make the pages safe:

- workspace scoping on every fetch (the /w/<id>/ IDOR surface)
- delete refused while a script still imports the library, and for plugin-owned
  rows (the owned-resource convention)
- restore is append-only: it writes forward, never rewinds current_version
- the editor's save-with-no-changes says so honestly instead of claiming a new
  version
"""

import html
import json
import re
import uuid

from django.test import TestCase
from django.urls import reverse
from unittest import mock

from core.models import (
    Environment,
    Library,
    Run,
    Script,
    ScriptLibrary,
    User,
    Workspace,
    WorkspaceMembership,
)
from core.services.library_service import diff_module_maps

MODULES_V1 = {"helpers.py": "def greet(n):\n    return f'hi {n}'\n"}
MODULES_V2 = {"helpers.py": "def greet(n):\n    return f'hello {n}'\n"}


def _setup_wizard_off(test):
    for target in (
        "core.services.setup_service.SetupService.is_setup_needed",
        "core.services.setup_service.SetupService.needs_admin_setup",
    ):
        p = mock.patch(target, return_value=False)
        p.start()
        test.addCleanup(p.stop)


class DiffTests(TestCase):
    """The diff is server-side and directly testable — no browser needed."""

    def test_changed_module_shows_both_sides(self):
        [entry] = diff_module_maps(MODULES_V1, MODULES_V2)
        self.assertEqual(entry["filename"], "helpers.py")
        self.assertEqual(entry["status"], "changed")
        body = "\n".join(entry["diff_lines"])
        self.assertIn("-    return f'hi {n}'", body)
        self.assertIn("+    return f'hello {n}'", body)
        # Unchanged lines survive as context, so a diff is readable on its own.
        self.assertIn(" def greet(n):", body)

    def test_added_and_removed_modules_are_part_of_the_diff(self):
        diff = {
            e["filename"]: e["status"]
            for e in diff_module_maps({"gone.py": "x = 1"}, {"new.py": "y = 2"})
        }
        self.assertEqual(diff, {"gone.py": "removed", "new.py": "added"})

    def test_identical_maps_are_unchanged_with_no_noise(self):
        [entry] = diff_module_maps(MODULES_V1, dict(MODULES_V1))
        self.assertEqual(entry["status"], "unchanged")
        self.assertEqual(entry["diff_lines"], [])


class LibraryViewTests(TestCase):
    def setUp(self):
        _setup_wizard_off(self)
        self.ws = Workspace.get_default()
        self.user = User.objects.create(email="dev@example.com")
        # A membership in the default workspace may already exist (new users are
        # auto-enrolled), so upsert the role rather than assume.
        WorkspaceMembership.objects.update_or_create(
            user=self.user,
            workspace=self.ws,
            defaults={"role": WorkspaceMembership.ROLE_OWNER},
        )
        self.env = Environment.objects.create(
            name="e", path=f"env{uuid.uuid4().hex[:8]}"
        )
        self.client.force_login(self.user)

    def _library(self, key="demo_lib", modules=MODULES_V1):
        lib = Library.objects.create(key=key, name="Demo", workspace=self.ws)
        if modules:
            lib.save_revision(modules, created_by=self.user)
        return lib

    # -- create ----------------------------------------------------------- #

    def test_create_makes_an_empty_library_and_lands_in_the_editor(self):
        resp = self.client.post(
            reverse("cpanel:library_create"),
            {"key": "new_lib", "name": "New", "description": "d"},
        )
        library = Library.objects.get(key="new_lib")
        self.assertRedirects(resp, reverse("cpanel:library_edit", args=[library.pk]))
        # Metadata only: content comes from the first editor save.
        self.assertEqual(library.current_version, 0)
        self.assertEqual(library.workspace, self.ws)
        self.assertEqual(library.created_by, self.user)

    def test_create_rejects_a_stdlib_key_through_the_form(self):
        resp = self.client.post(
            reverse("cpanel:library_create"), {"key": "json", "name": "J"}
        )
        self.assertEqual(resp.status_code, 200)  # re-rendered with the error
        self.assertFalse(Library.objects.filter(key="json").exists())
        self.assertContains(resp, "standard-library module")

    def test_create_rejects_a_duplicate_key_in_the_same_workspace(self):
        self._library("taken_lib")
        resp = self.client.post(
            reverse("cpanel:library_create"), {"key": "taken_lib", "name": "X"}
        )
        self.assertContains(resp, "already exists in this workspace")
        self.assertEqual(Library.objects.filter(key="taken_lib").count(), 1)

    # -- edit / revisions -------------------------------------------------- #

    def test_editor_opens_with_the_head_revision(self):
        library = self._library()
        resp = self.client.get(reverse("cpanel:library_edit", args=[library.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(json.loads(resp.context["modules_json"]), MODULES_V1)

    def test_modules_json_survives_html_attribute_escaping(self):
        """The editor's whole contract: the module map round-trips through an HTML
        ``value="..."`` attribute. Asserting on the context would pass even if the
        RENDERED page were broken, so parse it back out of the real HTML — with
        content containing every character escaping touches.
        """
        tricky = {
            "helpers.py": (
                'S = "a & b <tag> \'x\'"\n'
                'P = "C:\\\\path"\n'
                "# </script> --> &amp;\n"
            )
        }
        library = self._library("tricky_lib", modules=tricky)

        body = self.client.get(
            reverse("cpanel:library_edit", args=[library.pk])
        ).content.decode()
        match = re.search(r'id="modules_json" value="([^"]*)"', body)
        self.assertIsNotNone(match, "modules_json field missing from rendered HTML")
        self.assertEqual(json.loads(html.unescape(match.group(1))), tricky)

    def test_editor_opens_a_starter_module_for_an_empty_library(self):
        library = self._library("fresh_lib", modules=None)
        resp = self.client.get(reverse("cpanel:library_edit", args=[library.pk]))
        modules = json.loads(resp.context["modules_json"])
        self.assertIn("helpers.py", modules)
        # The starter references the library's own key so the import shape is
        # copy-pasteable from the first second.
        self.assertIn("fresh_lib", modules["helpers.py"])

    def test_save_creates_a_revision(self):
        library = self._library()
        resp = self.client.post(
            reverse("cpanel:library_edit", args=[library.pk]),
            {
                "key": library.key,
                "name": "Demo",
                "description": "",
                "modules_json": json.dumps(MODULES_V2),
            },
        )
        self.assertRedirects(resp, reverse("cpanel:library_detail", args=[library.pk]))
        library.refresh_from_db()
        self.assertEqual(library.current_version, 2)
        self.assertEqual(library.head.modules, MODULES_V2)

    def test_save_with_no_code_change_says_so_and_writes_nothing(self):
        library = self._library()
        resp = self.client.post(
            reverse("cpanel:library_edit", args=[library.pk]),
            {
                "key": library.key,
                "name": "Renamed",
                "description": "",
                "modules_json": json.dumps(MODULES_V1),
            },
            follow=True,
        )
        library.refresh_from_db()
        self.assertEqual(library.current_version, 1)  # no churn
        self.assertEqual(library.name, "Renamed")  # metadata still saved
        self.assertContains(resp, "no new version was created")

    def test_save_with_an_illegal_module_name_is_refused(self):
        library = self._library()
        resp = self.client.post(
            reverse("cpanel:library_edit", args=[library.pk]),
            {
                "key": library.key,
                "name": "Demo",
                "description": "",
                "modules_json": json.dumps({"../escape.py": "x = 1"}),
            },
            follow=True,
        )
        library.refresh_from_db()
        self.assertEqual(library.current_version, 1)
        self.assertContains(resp, "Invalid module filename")

    def test_save_with_malformed_editor_payload_is_refused(self):
        library = self._library()
        resp = self.client.post(
            reverse("cpanel:library_edit", args=[library.pk]),
            {
                "key": library.key,
                "name": "Demo",
                "description": "",
                "modules_json": "{not json",
            },
            follow=True,
        )
        library.refresh_from_db()
        self.assertEqual(library.current_version, 1)
        self.assertContains(resp, "malformed module data")

    # -- revision view / diff / restore ------------------------------------ #

    def test_revision_view_shows_the_diff_against_head(self):
        library = self._library()
        library.save_revision(MODULES_V2, created_by=self.user)
        resp = self.client.get(reverse("cpanel:library_revision", args=[library.pk, 1]))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context["has_changes"])
        self.assertFalse(resp.context["is_head"])

    def test_revision_view_of_head_has_no_diff(self):
        library = self._library()
        resp = self.client.get(reverse("cpanel:library_revision", args=[library.pk, 1]))
        self.assertTrue(resp.context["is_head"])

    def test_missing_revision_redirects_with_a_message(self):
        library = self._library()
        resp = self.client.get(
            reverse("cpanel:library_revision", args=[library.pk, 99]), follow=True
        )
        self.assertContains(resp, "does not exist")

    def test_restore_writes_forward_and_never_rewinds(self):
        library = self._library()
        library.save_revision(MODULES_V2, created_by=self.user)

        resp = self.client.post(
            reverse("cpanel:library_revision_restore", args=[library.pk, 1]), follow=True
        )
        library.refresh_from_db()
        # v1's content is back — as v3. History is append-only, so the stamp on
        # any run pinned to v1 or v2 still resolves to what it actually ran.
        self.assertEqual(library.current_version, 3)
        self.assertEqual(library.head.modules, MODULES_V1)
        self.assertEqual(library.revisions.count(), 3)
        self.assertContains(resp, "Restored version 1 as new version 3")

    def test_restoring_the_current_content_is_a_no_op(self):
        library = self._library()
        resp = self.client.post(
            reverse("cpanel:library_revision_restore", args=[library.pk, 1]), follow=True
        )
        library.refresh_from_db()
        self.assertEqual(library.current_version, 1)
        self.assertContains(resp, "nothing to restore")

    # -- delete guards ----------------------------------------------------- #

    def test_delete_is_blocked_while_a_script_imports_it(self):
        library = self._library()
        script = Script.objects.create(
            name="worker", code="pass", environment=self.env, workspace=self.ws
        )
        ScriptLibrary.objects.create(script=script, library=library)

        resp = self.client.post(
            reverse("cpanel:library_delete", args=[library.pk]), follow=True
        )
        self.assertTrue(Library.objects.filter(pk=library.pk).exists())
        # The message must NAME the blocking script — "it's in use" is useless.
        self.assertContains(resp, "worker")

    def test_delete_works_once_detached(self):
        library = self._library()
        resp = self.client.post(
            reverse("cpanel:library_delete", args=[library.pk]), follow=True
        )
        self.assertFalse(Library.objects.filter(pk=library.pk).exists())
        self.assertContains(resp, "deleted")

    def test_delete_of_a_plugin_owned_library_is_blocked(self):
        library = self._library()
        Library.objects.filter(pk=library.pk).update(owner_plugin="someplugin")
        resp = self.client.post(
            reverse("cpanel:library_delete", args=[library.pk]), follow=True
        )
        self.assertTrue(Library.objects.filter(pk=library.pk).exists())
        # Assert the BLOCK MESSAGE, not just the slug: the page we land on also
        # renders an owner badge naming the plugin, so "someplugin" alone would
        # pass even if the refusal message never appeared.
        self.assertContains(resp, "uninstall the plugin to remove it")

    # -- workspace scoping -------------------------------------------------- #

    def test_another_workspaces_library_is_404(self):
        other_ws = Workspace.objects.create(name="Other")
        foreign = Library.objects.create(key="foreign_lib", name="F", workspace=other_ws)
        for name in ("library_detail", "library_edit"):
            self.assertEqual(
                self.client.get(reverse(f"cpanel:{name}", args=[foreign.pk])).status_code,
                404,
            )
        self.assertEqual(
            self.client.post(
                reverse("cpanel:library_delete", args=[foreign.pk])
            ).status_code,
            404,
        )

    def test_list_only_shows_the_active_workspace(self):
        self._library("mine_lib")
        other_ws = Workspace.objects.create(name="Other")
        Library.objects.create(key="theirs_lib", name="T", workspace=other_ws)

        resp = self.client.get(reverse("cpanel:library_list"))
        keys = [lib.key for lib in resp.context["libraries"]]
        self.assertIn("mine_lib", keys)
        self.assertNotIn("theirs_lib", keys)

    def test_login_is_required(self):
        self.client.logout()
        resp = self.client.get(reverse("cpanel:library_list"))
        self.assertEqual(resp.status_code, 302)


class ScriptFormAttachTests(TestCase):
    """The attach picker on the script form (the other half of the grammar)."""

    def setUp(self):
        _setup_wizard_off(self)
        self.ws = Workspace.get_default()
        self.user = User.objects.create(email="dev@example.com")
        # A membership in the default workspace may already exist (new users are
        # auto-enrolled), so upsert the role rather than assume.
        WorkspaceMembership.objects.update_or_create(
            user=self.user,
            workspace=self.ws,
            defaults={"role": WorkspaceMembership.ROLE_OWNER},
        )
        self.env = Environment.objects.create(
            name="e", path=f"env{uuid.uuid4().hex[:8]}"
        )
        self.library = Library.objects.create(
            key="demo_lib", name="Demo", workspace=self.ws
        )
        self.library.save_revision(MODULES_V1)
        self.client.force_login(self.user)

    def _script(self):
        return Script.objects.create(
            name="s", code="pass", environment=self.env, workspace=self.ws
        )

    def _post(self, script, library_ids):
        """Post the script edit form. Both the script AND schedule forms must be
        valid or the view re-renders and saves nothing — hence the schedule fields."""
        resp = self.client.post(
            reverse("cpanel:script_edit", args=[script.pk]),
            {
                "name": script.name,
                "description": "",
                "code": "pass",
                "environment": str(self.env.pk),
                "timeout_seconds": "3600",
                "is_enabled": "on",
                "isolation_mode": "inherit",
                "injection_mode": "all",
                "notify_on": "never",
                "run_mode": "interval",
                "interval_minutes": "60",
                "timezone": "UTC",
                "library_ids": library_ids,
            },
        )
        # A 200 means a form failed validation and nothing was saved — that would
        # make every assertion below a false negative.
        self.assertEqual(
            resp.status_code, 302, "script edit form did not save (re-rendered)"
        )
        return resp

    def test_attach_and_detach_through_the_form(self):
        script = self._script()
        self._post(script, [str(self.library.pk)])
        self.assertEqual(list(script.libraries.all()), [self.library])

        self._post(script, [])
        self.assertEqual(list(script.libraries.all()), [])

    def test_reposting_the_same_attachment_is_idempotent(self):
        script = self._script()
        self._post(script, [str(self.library.pk)])
        self._post(script, [str(self.library.pk)])
        self.assertEqual(ScriptLibrary.objects.filter(script=script).count(), 1)

    def test_a_foreign_workspaces_library_id_is_ignored(self):
        # Tenancy: the workspace filter in _reconcile_libraries is the guard —
        # a posted id from elsewhere must not attach.
        other_ws = Workspace.objects.create(name="Other")
        foreign = Library.objects.create(key="foreign_lib", name="F", workspace=other_ws)
        script = self._script()
        self._post(script, [str(foreign.pk)])
        self.assertEqual(list(script.libraries.all()), [])

    def test_edit_form_offers_workspace_libraries_and_marks_attached(self):
        script = self._script()
        ScriptLibrary.objects.create(script=script, library=self.library)
        resp = self.client.get(reverse("cpanel:script_edit", args=[script.pk]))
        self.assertIn(self.library, list(resp.context["available_libraries"]))
        self.assertEqual(resp.context["attached_library_ids"], [str(self.library.pk)])


class RunDetailStampTests(TestCase):
    def setUp(self):
        _setup_wizard_off(self)
        self.ws = Workspace.get_default()
        self.user = User.objects.create(email="dev@example.com")
        # A membership in the default workspace may already exist (new users are
        # auto-enrolled), so upsert the role rather than assume.
        WorkspaceMembership.objects.update_or_create(
            user=self.user,
            workspace=self.ws,
            defaults={"role": WorkspaceMembership.ROLE_OWNER},
        )
        self.env = Environment.objects.create(
            name="e", path=f"env{uuid.uuid4().hex[:8]}"
        )
        self.script = Script.objects.create(
            name="s", code="pass", environment=self.env, workspace=self.ws
        )
        self.client.force_login(self.user)

    def test_run_detail_shows_the_pinned_versions(self):
        run = Run.objects.create(
            script=self.script,
            workspace=self.ws,
            status=Run.Status.SUCCESS,
            library_versions={"demo_lib": 3},
        )
        resp = self.client.get(reverse("cpanel:run_detail", args=[run.pk]))
        self.assertContains(resp, "demo_lib")
        self.assertContains(resp, "v3")

    def test_run_without_libraries_shows_no_section(self):
        run = Run.objects.create(
            script=self.script, workspace=self.ws, status=Run.Status.SUCCESS
        )
        resp = self.client.get(reverse("cpanel:run_detail", args=[run.pk]))
        self.assertNotContains(resp, "pinned when this run was queued")
