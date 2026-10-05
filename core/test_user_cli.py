"""
``manage.py user`` — account recovery from the shell.

The command is the way back in when the web console can't help (no email
configured, login lockout, no admin who can sign in), so these tests drive the
real login view where it matters: a reset password must actually sign in, old
sessions must actually be signed out, and a lockout must actually be lifted.
"""

from io import StringIO
from unittest import mock

from axes.models import AccessAttempt
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.urls import reverse

from core.models import (
    Environment,
    GlobalSettings,
    PasswordResetToken,
    User,
    UserInvite,
    Workspace,
    WorkspaceMembership,
)

OLD = "correct-horse-battery-staple-91"
NEW = "another-long-sturdy-passphrase-42"
ADMIN = "admin@example.com"
COMMAND = "core.management.commands.user"


def run(*args):
    """Run ``manage.py user <args>`` and return what it printed."""
    out = StringIO()
    call_command("user", *args, stdout=out, stderr=StringIO())
    return out.getvalue()


def typing(*answers):
    """A terminal whose password prompts are answered with ``answers`` in order."""
    return mock.patch.multiple(
        f"{COMMAND}.Command", _stdin_is_tty=mock.Mock(return_value=True)
    ), mock.patch(f"{COMMAND}.getpass.getpass", side_effect=list(answers))


class UserCommandTestCase(TestCase):
    def setUp(self):
        gs = GlobalSettings.get_settings()
        gs.setup_completed = True
        gs.save()
        Environment.objects.get_or_create(
            name="default", defaults={"is_default": True, "python_version": "3.12"}
        )
        self.admin = User.objects.create_user(
            username=ADMIN, email=ADMIN, password=OLD, is_superuser=True, is_staff=True
        )

    def run_typing(self, answers, *args):
        tty, getpass = typing(*answers)
        with tty, getpass:
            return run(*args)

    def sign_in(self, email, password):
        return self.client.post(
            reverse("auth:login"),
            {"action": "password", "email": email, "password": password},
        )

    def signed_in(self):
        return "_auth_user_id" in self.client.session


class ResetPasswordTests(UserCommandTestCase):
    def test_new_password_signs_in_and_old_one_does_not(self):
        self.run_typing([NEW, NEW], "reset-password", ADMIN)

        self.sign_in(ADMIN, OLD)
        self.assertFalse(self.signed_in())
        self.sign_in(ADMIN, NEW)
        self.assertTrue(self.signed_in())

    def test_generate_prints_a_password_that_signs_in(self):
        out = run("reset-password", ADMIN, "--generate")

        password = out.split("Password: ")[1].split()[0]
        self.sign_in(ADMIN, password)
        self.assertTrue(self.signed_in())

    def test_prompted_password_is_never_printed(self):
        out = self.run_typing([NEW, NEW], "reset-password", ADMIN)
        self.assertNotIn(NEW, out)

    def test_existing_sessions_are_signed_out(self):
        self.client.force_login(
            self.admin, backend="django.contrib.auth.backends.ModelBackend"
        )
        self.assertEqual(self.client.get(reverse("auth:change_password")).status_code, 200)

        run("reset-password", ADMIN, "--generate")

        self.assertEqual(self.client.get(reverse("auth:change_password")).status_code, 302)

    def test_lifts_a_login_lockout(self):
        for _ in range(settings.AXES_FAILURE_LIMIT):
            self.sign_in(ADMIN, "wrong-password")
        self.sign_in(ADMIN, OLD)
        self.assertFalse(self.signed_in())  # locked out despite the right password

        self.run_typing([NEW, NEW], "reset-password", ADMIN)

        self.sign_in(ADMIN, NEW)
        self.assertTrue(self.signed_in())

    def test_expires_outstanding_emailed_reset_links(self):
        token = PasswordResetToken.create_for_user(self.admin)
        self.assertTrue(token.is_valid())

        run("reset-password", ADMIN, "--generate")

        token.refresh_from_db()
        self.assertFalse(token.is_valid())

    def test_email_is_matched_case_insensitively(self):
        self.run_typing([NEW, NEW], "reset-password", "Admin@Example.com")

        self.admin.refresh_from_db()
        self.assertTrue(self.admin.check_password(NEW))

    def test_weak_passwords_are_refused_and_nothing_changes(self):
        with self.assertRaises(CommandError):
            self.run_typing(["12345678"] * 6, "reset-password", ADMIN)

        self.admin.refresh_from_db()
        self.assertTrue(self.admin.check_password(OLD))

    def test_mismatch_reprompts(self):
        self.run_typing([NEW, "typo", NEW, NEW], "reset-password", ADMIN)

        self.admin.refresh_from_db()
        self.assertTrue(self.admin.check_password(NEW))

    def test_without_a_terminal_it_points_at_generate(self):
        with mock.patch(f"{COMMAND}.Command._stdin_is_tty", return_value=False):
            with self.assertRaisesMessage(CommandError, "--generate"):
                run("reset-password", ADMIN)

        self.admin.refresh_from_db()
        self.assertTrue(self.admin.check_password(OLD))

    def test_unknown_email_is_an_error(self):
        with self.assertRaisesMessage(CommandError, "No account"):
            run("reset-password", "nobody@example.com", "--generate")


class CreateTests(UserCommandTestCase):
    def test_creates_a_user_who_can_sign_in(self):
        self.run_typing([NEW, NEW], "create", "New.Person@Example.com")

        user = User.objects.get(email="new.person@example.com")  # stored lowercase
        self.assertFalse(user.is_superuser)
        self.assertTrue(user.is_verified)
        membership = WorkspaceMembership.objects.get(user=user, workspace=Workspace.get_default())
        self.assertEqual(membership.role, WorkspaceMembership.ROLE_MEMBER)
        self.sign_in("new.person@example.com", NEW)
        self.assertTrue(self.signed_in())

    def test_admin_flag_makes_an_admin_who_owns_the_default_workspace(self):
        run("create", "second@example.com", "--admin", "--generate")

        user = User.objects.get(email="second@example.com")
        self.assertTrue(user.is_superuser)
        membership = WorkspaceMembership.objects.get(user=user, workspace=Workspace.get_default())
        self.assertEqual(membership.role, WorkspaceMembership.ROLE_OWNER)

    def test_existing_email_is_refused(self):
        with self.assertRaisesMessage(CommandError, "already exists"):
            run("create", "ADMIN@example.com", "--generate")
        self.assertEqual(User.objects.count(), 1)

    def test_invalid_email_is_refused(self):
        with self.assertRaisesMessage(CommandError, "not a valid email"):
            run("create", "not-an-email", "--generate")

    def test_failed_password_prompt_creates_nothing(self):
        with self.assertRaises(CommandError):
            self.run_typing(["12345678"] * 6, "create", "new@example.com")
        self.assertFalse(User.objects.filter(email="new@example.com").exists())

    def test_revokes_a_pending_invite_for_the_same_email(self):
        UserInvite.create_invite("new@example.com", created_by=self.admin)

        out = run("create", "new@example.com", "--generate")

        self.assertFalse(UserInvite.objects.filter(email="new@example.com").exists())
        self.assertIn("Revoked", out)


class RoleTests(UserCommandTestCase):
    def setUp(self):
        super().setUp()
        self.member = User.objects.create_user(
            username="member@example.com", email="member@example.com", password=OLD
        )

    def test_promote_makes_an_admin_who_owns_the_default_workspace(self):
        run("promote", "member@example.com")

        self.member.refresh_from_db()
        self.assertTrue(self.member.is_superuser)
        membership = WorkspaceMembership.objects.get(
            user=self.member, workspace=Workspace.get_default()
        )
        self.assertEqual(membership.role, WorkspaceMembership.ROLE_OWNER)

    def test_demote_removes_admin_rights(self):
        run("promote", "member@example.com")
        run("demote", ADMIN)

        self.admin.refresh_from_db()
        self.assertFalse(self.admin.is_superuser)
        self.assertFalse(self.admin.is_staff)

    def test_the_last_admin_cannot_be_demoted(self):
        with self.assertRaisesMessage(CommandError, "only admin"):
            run("demote", ADMIN)

        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_superuser)

    def test_demoting_a_non_admin_changes_nothing(self):
        self.assertIn("not an admin", run("demote", "member@example.com"))


class UnlockAndListTests(UserCommandTestCase):
    def test_unlock_lets_the_right_password_sign_in_again(self):
        for _ in range(settings.AXES_FAILURE_LIMIT):
            self.sign_in(ADMIN, "wrong-password")
        self.sign_in(ADMIN, OLD)
        self.assertFalse(self.signed_in())

        run("unlock", ADMIN)

        self.assertFalse(AccessAttempt.objects.filter(username=ADMIN).exists())
        self.sign_in(ADMIN, OLD)
        self.assertTrue(self.signed_in())

    def test_unlock_with_nothing_to_clear_says_so(self):
        self.assertIn("no failed logins", run("unlock", ADMIN))

    def test_list_shows_each_account_with_role_and_password_state(self):
        User.objects.create(email="invited@example.com")  # no password set

        lines = run("list").splitlines()

        self.assertEqual(lines[0].split(), ["EMAIL", "ROLE", "PASSWORD", "LAST", "LOGIN"])
        self.assertEqual(lines[1].split(), [ADMIN, "admin", "set", "never"])
        self.assertEqual(lines[2].split(), ["invited@example.com", "user", "not", "set", "never"])
