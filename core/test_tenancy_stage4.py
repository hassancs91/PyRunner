"""
Tenancy Stage 4 — RBAC role-gating + member management + workspace-delete-block.

Covers the plan's role matrix (leak rows 16 & 17): a plain Member cannot manage
members or delete a workspace; an Admin can manage members but not delete; an
Owner can delete (but never the default workspace, and never one that still owns
data — SET_NULL would orphan it invisibly); a superuser is cross-workspace. Also
the last-owner guards (a workspace must always keep an owner) and that a
non-member targeting a workspace 404s (no existence disclosure).
"""

from django.test import TestCase
from django.urls import reverse

from core.models import (
    Environment,
    Script,
    User,
    Workspace,
    WorkspaceMembership,
)
from unittest import mock


def _mock_setup(test):
    for target in (
        "core.services.setup_service.SetupService.is_setup_needed",
        "core.services.setup_service.SetupService.needs_admin_setup",
    ):
        p = mock.patch(target, return_value=False)
        p.start()
        test.addCleanup(p.stop)


def _member(user, workspace, role):
    """Set ``user``'s role in ``workspace`` (overriding the signal's default)."""
    m, _ = WorkspaceMembership.objects.update_or_create(
        user=user, workspace=workspace, defaults={"role": role}
    )
    return m


class _Fixture(TestCase):
    def setUp(self):
        _mock_setup(self)
        self.ws = Workspace.objects.create(name="Acme")

        self.owner = User.objects.create(email="owner@example.com")
        self.admin = User.objects.create(email="admin@example.com")
        self.plain = User.objects.create(email="member@example.com")
        self.outsider = User.objects.create(email="outsider@example.com")
        self.super = User.objects.create(email="root@example.com", is_superuser=True)

        _member(self.owner, self.ws, WorkspaceMembership.ROLE_OWNER)
        _member(self.admin, self.ws, WorkspaceMembership.ROLE_ADMIN)
        _member(self.plain, self.ws, WorkspaceMembership.ROLE_MEMBER)
        # outsider gets only the signal's default-workspace membership.

    def _login(self, user):
        self.client.force_login(user)


class RenameGatingTests(_Fixture):
    def _rename(self, name="Renamed"):
        return self.client.post(
            reverse("cpanel:workspace_rename", args=[self.ws.id]), {"name": name}
        )

    def test_member_cannot_rename(self):
        self._login(self.plain)
        self.assertEqual(self._rename().status_code, 403)

    def test_admin_can_rename(self):
        self._login(self.admin)
        self.assertEqual(self._rename("ByAdmin").status_code, 302)
        self.ws.refresh_from_db()
        self.assertEqual(self.ws.name, "ByAdmin")

    def test_owner_can_rename(self):
        self._login(self.owner)
        self.assertEqual(self._rename("ByOwner").status_code, 302)

    def test_superuser_can_rename(self):
        self._login(self.super)
        self.assertEqual(self._rename("BySuper").status_code, 302)

    def test_outsider_404(self):
        # Not a member → 404 (no existence disclosure), not 403.
        self._login(self.outsider)
        self.assertEqual(self._rename().status_code, 404)


class MembersPageGatingTests(_Fixture):
    def _members(self):
        return self.client.get(reverse("cpanel:workspace_members", args=[self.ws.id]))

    def test_member_cannot_view(self):
        self._login(self.plain)
        self.assertEqual(self._members().status_code, 403)

    def test_admin_can_view(self):
        self._login(self.admin)
        self.assertEqual(self._members().status_code, 200)

    def test_outsider_404(self):
        self._login(self.outsider)
        self.assertEqual(self._members().status_code, 404)


class MemberManagementTests(_Fixture):
    def test_admin_adds_existing_user(self):
        self._login(self.admin)
        resp = self.client.post(
            reverse("cpanel:workspace_member_add", args=[self.ws.id]),
            {"email": self.outsider.email, "role": "member"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(
            WorkspaceMembership.objects.filter(
                user=self.outsider, workspace=self.ws
            ).exists()
        )

    def test_member_cannot_add(self):
        self._login(self.plain)
        resp = self.client.post(
            reverse("cpanel:workspace_member_add", args=[self.ws.id]),
            {"email": self.outsider.email, "role": "member"},
        )
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(
            WorkspaceMembership.objects.filter(
                user=self.outsider, workspace=self.ws
            ).exists()
        )

    def test_add_unknown_email_no_membership(self):
        self._login(self.owner)
        before = WorkspaceMembership.objects.filter(workspace=self.ws).count()
        resp = self.client.post(
            reverse("cpanel:workspace_member_add", args=[self.ws.id]),
            {"email": "ghost@example.com", "role": "member"},
        )
        self.assertEqual(resp.status_code, 302)  # redirect with an error message
        self.assertEqual(
            WorkspaceMembership.objects.filter(workspace=self.ws).count(), before
        )

    def test_change_role(self):
        self._login(self.owner)
        m = WorkspaceMembership.objects.get(user=self.plain, workspace=self.ws)
        resp = self.client.post(
            reverse("cpanel:workspace_member_role", args=[self.ws.id, m.id]),
            {"role": "admin"},
        )
        self.assertEqual(resp.status_code, 302)
        m.refresh_from_db()
        self.assertEqual(m.role, "admin")

    def test_remove_member(self):
        self._login(self.admin)
        m = WorkspaceMembership.objects.get(user=self.plain, workspace=self.ws)
        resp = self.client.post(
            reverse("cpanel:workspace_member_remove", args=[self.ws.id, m.id])
        )
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(WorkspaceMembership.objects.filter(pk=m.id).exists())

    def test_cannot_demote_last_owner(self):
        self._login(self.super)
        m = WorkspaceMembership.objects.get(user=self.owner, workspace=self.ws)
        self.client.post(
            reverse("cpanel:workspace_member_role", args=[self.ws.id, m.id]),
            {"role": "member"},
        )
        m.refresh_from_db()
        self.assertEqual(m.role, "owner")  # refused — last owner

    def test_cannot_remove_last_owner(self):
        self._login(self.super)
        m = WorkspaceMembership.objects.get(user=self.owner, workspace=self.ws)
        self.client.post(
            reverse("cpanel:workspace_member_remove", args=[self.ws.id, m.id])
        )
        self.assertTrue(WorkspaceMembership.objects.filter(pk=m.id).exists())

    def test_can_remove_owner_when_another_exists(self):
        self._login(self.super)
        _member(self.admin, self.ws, WorkspaceMembership.ROLE_OWNER)  # 2nd owner
        m = WorkspaceMembership.objects.get(user=self.owner, workspace=self.ws)
        resp = self.client.post(
            reverse("cpanel:workspace_member_remove", args=[self.ws.id, m.id])
        )
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(WorkspaceMembership.objects.filter(pk=m.id).exists())


class DeleteTests(_Fixture):
    def _delete(self):
        return self.client.post(reverse("cpanel:workspace_delete", args=[self.ws.id]))

    def test_member_cannot_delete(self):
        self._login(self.plain)
        self.assertEqual(self._delete().status_code, 403)
        self.assertTrue(Workspace.objects.filter(pk=self.ws.id).exists())

    def test_admin_cannot_delete(self):
        # Delete is Owner-only — an Admin is refused.
        self._login(self.admin)
        self.assertEqual(self._delete().status_code, 403)
        self.assertTrue(Workspace.objects.filter(pk=self.ws.id).exists())

    def test_owner_deletes_empty_workspace(self):
        self._login(self.owner)
        self.assertEqual(self._delete().status_code, 302)
        self.assertFalse(Workspace.objects.filter(pk=self.ws.id).exists())

    def test_delete_blocked_if_non_empty(self):
        # Row 16: a workspace that still owns data cannot be deleted (SET_NULL
        # would orphan the rows where strict scoping makes them invisible).
        env = Environment.objects.create(name="e", path="s4env")
        Script.objects.create(name="s", code="x", environment=env, workspace=self.ws)
        self._login(self.owner)
        self.assertEqual(self._delete().status_code, 302)  # redirect w/ error
        self.assertTrue(Workspace.objects.filter(pk=self.ws.id).exists())

    def test_cannot_delete_default(self):
        default = Workspace.get_default()
        _member(self.owner, default, WorkspaceMembership.ROLE_OWNER)
        self._login(self.owner)
        resp = self.client.post(
            reverse("cpanel:workspace_delete", args=[default.id])
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(Workspace.objects.filter(pk=default.id).exists())

    def test_outsider_delete_404(self):
        self._login(self.outsider)
        self.assertEqual(self._delete().status_code, 404)


class CreateAndListTests(_Fixture):
    def test_member_cannot_create(self):
        self._login(self.plain)
        before = Workspace.objects.count()
        resp = self.client.post(
            reverse("cpanel:workspace_create"), {"name": "Sneaky"}
        )
        self.assertEqual(resp.status_code, 302)  # user_passes_test → login redirect
        self.assertEqual(Workspace.objects.count(), before)

    def test_superuser_can_create(self):
        self._login(self.super)
        resp = self.client.post(
            reverse("cpanel:workspace_create"), {"name": "NewCo"}
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(Workspace.objects.filter(name="NewCo").exists())

    def test_list_shows_only_members_workspaces(self):
        other = Workspace.objects.create(name="NotMine")
        self._login(self.plain)
        body = self.client.get(reverse("cpanel:workspace_list")).content.decode()
        self.assertIn("Acme", body)
        self.assertNotIn("NotMine", body)

    def test_create_button_superuser_only(self):
        self._login(self.plain)
        body = self.client.get(reverse("cpanel:workspace_list")).content.decode()
        self.assertNotIn("New workspace", body)
        self._login(self.super)
        body = self.client.get(reverse("cpanel:workspace_list")).content.decode()
        self.assertIn("New workspace", body)
