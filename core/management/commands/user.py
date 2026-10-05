"""
``manage.py user`` — manage accounts from the server's shell.

The recovery path for when the web console can't help: a forgotten password on
an instance with no email configured, a login lockout, or no admin left who can
sign in. Shell access to the host is the trust boundary here — whoever can run
this already owns the instance.

    python manage.py user list
    python manage.py user reset-password you@example.com
    python manage.py user create new@example.com --admin
    python manage.py user promote you@example.com
    python manage.py user demote you@example.com
    python manage.py user unlock you@example.com

Passwords are prompted for, never taken as an argument, so they stay out of the
shell history and the process list. ``--generate`` prints a random one instead,
for shells without a terminal (``docker exec`` without ``-it``).
"""

import getpass
import logging
import secrets
import sys

from axes.utils import reset as reset_login_attempts
from django.contrib.auth import password_validation
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone

from core.models import PasswordResetToken, User, UserInvite, WorkspaceMembership

logger = logging.getLogger(__name__)

MAX_PASSWORD_TRIES = 3


class Command(BaseCommand):
    help = "Manage accounts from the shell: list, reset a password, create, promote, demote, unlock."

    def add_arguments(self, parser):
        actions = parser.add_subparsers(dest="action", metavar="action", required=True)

        actions.add_parser("list", help="Show every account.")

        reset = actions.add_parser(
            "reset-password",
            help="Set a new password for an account, sign out its sessions and clear its login lockout.",
        )
        reset.add_argument("email")
        self._add_generate_flag(reset)

        create = actions.add_parser(
            "create", help="Create an account with a password (no invite needed)."
        )
        create.add_argument("email")
        create.add_argument(
            "--admin", action="store_true", help="Make the new account an instance admin."
        )
        self._add_generate_flag(create)

        promote = actions.add_parser("promote", help="Make an account an instance admin.")
        promote.add_argument("email")

        demote = actions.add_parser(
            "demote", help="Remove admin rights from an account (never the last admin)."
        )
        demote.add_argument("email")

        unlock = actions.add_parser(
            "unlock", help="Clear the failed-login lockout for an account."
        )
        unlock.add_argument("email")

    @staticmethod
    def _add_generate_flag(parser):
        parser.add_argument(
            "--generate",
            action="store_true",
            help="Print a random password instead of prompting (for shells without a terminal).",
        )

    def handle(self, *args, **options):
        actions = {
            "list": self._list,
            "reset-password": self._reset_password,
            "create": self._create,
            "promote": self._promote,
            "demote": self._demote,
            "unlock": self._unlock,
        }
        actions[options["action"]](options)

    # --- Actions --------------------------------------------------------------

    def _list(self, options):
        users = list(User.objects.order_by("date_joined"))
        if not users:
            self.stdout.write(
                "No accounts yet. Create the first admin with: "
                "python manage.py user create <email> --admin"
            )
            return

        rows = [("EMAIL", "ROLE", "PASSWORD", "LAST LOGIN")]
        for user in users:
            rows.append((
                user.email,
                "admin" if user.is_superuser else "user",
                "set" if user.has_usable_password() else "not set",
                user.last_login.strftime("%Y-%m-%d %H:%M UTC") if user.last_login else "never",
            ))
        widths = [max(len(row[col]) for row in rows) for col in range(len(rows[0]))]
        for row in rows:
            self.stdout.write(
                "  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip()
            )

    def _reset_password(self, options):
        user = self._get_user(options["email"])
        password = self._new_password(user, generate=options["generate"])

        with transaction.atomic():
            user.set_password(password)
            user.save(update_fields=["password"])
            # An emailed reset link requested earlier must not outlive this reset.
            PasswordResetToken.objects.filter(user=user, used_at__isnull=True).update(
                expires_at=timezone.now()
            )
            # Someone who lost their password has usually locked themselves out too.
            reset_login_attempts(username=user.email.lower())

        logger.info("CLI: password reset for %s", user.email)
        self.stdout.write(self.style.SUCCESS(f"Password updated for {user.email}."))
        self.stdout.write("  Sessions signed in with the old password are now signed out.")
        self._show_generated(password, options)

    def _create(self, options):
        email = options["email"].strip().lower()
        try:
            validate_email(email)
        except ValidationError:
            raise CommandError(f"{options['email']!r} is not a valid email address.")
        if User.objects.filter(email__iexact=email).exists():
            raise CommandError(
                f"An account for {email} already exists. To set its password, run: "
                f"python manage.py user reset-password {email}"
            )

        # Read the password before touching the database, so a cancelled or
        # failed prompt leaves no half-made account behind.
        password = self._new_password(User(email=email), generate=options["generate"])
        is_admin = options["admin"]

        with transaction.atomic():
            # is_superuser is set at creation so the post_save membership hook
            # makes an admin the owner of the default workspace.
            user = User.objects.create_user(
                username=email,
                email=email,
                password=password,
                is_verified=True,
                is_staff=is_admin,
                is_superuser=is_admin,
            )
            # A pending invite would let its link set this account's password.
            revoked, _ = UserInvite.objects.filter(
                email__iexact=email, used_at__isnull=True
            ).delete()

        logger.info("CLI: account created for %s (admin=%s)", user.email, is_admin)
        role = "admin" if is_admin else "user"
        self.stdout.write(self.style.SUCCESS(f"Created {role} account {user.email}."))
        if revoked:
            self.stdout.write("  Revoked the pending invite for this email.")
        self._show_generated(password, options)

    def _promote(self, options):
        user = self._get_user(options["email"])
        was_admin = user.is_superuser

        user.is_superuser = True
        user.is_staff = True
        user.save(update_fields=["is_superuser", "is_staff"])
        # Same pairing as the bootstrap admin: an instance admin owns the
        # default workspace.
        WorkspaceMembership.ensure(user, role=WorkspaceMembership.ROLE_OWNER)

        if was_admin:
            self.stdout.write(f"{user.email} is already an admin.")
            return
        logger.info("CLI: %s promoted to admin", user.email)
        self.stdout.write(self.style.SUCCESS(f"{user.email} is now an admin."))

    def _demote(self, options):
        user = self._get_user(options["email"])
        if not user.is_superuser:
            self.stdout.write(f"{user.email} is not an admin.")
            return
        if not User.objects.filter(is_superuser=True, is_active=True).exclude(pk=user.pk).exists():
            raise CommandError(
                f"{user.email} is the only admin. Promote another account first: "
                "python manage.py user promote <email>"
            )

        user.is_superuser = False
        user.is_staff = False
        user.save(update_fields=["is_superuser", "is_staff"])

        logger.info("CLI: %s demoted from admin", user.email)
        self.stdout.write(self.style.SUCCESS(f"{user.email} is no longer an admin."))
        self.stdout.write("  Their workspace roles are unchanged.")

    def _unlock(self, options):
        user = self._get_user(options["email"])
        cleared = reset_login_attempts(username=user.email.lower())
        if cleared:
            logger.info("CLI: login lockout cleared for %s", user.email)
            self.stdout.write(self.style.SUCCESS(f"Cleared the login lockout for {user.email}."))
        else:
            self.stdout.write(f"{user.email} has no failed logins on record.")

    # --- Helpers --------------------------------------------------------------

    def _get_user(self, email):
        user = User.objects.filter(email__iexact=email.strip()).first()
        if user is None:
            raise CommandError(
                f"No account with the email {email!r}. "
                "Run `python manage.py user list` to see the accounts."
            )
        return user

    def _stdin_is_tty(self):
        return sys.stdin.isatty()

    def _new_password(self, user, generate):
        """Return a new password for ``user``: generated, or prompted for and
        checked against AUTH_PASSWORD_VALIDATORS."""
        if generate:
            return secrets.token_urlsafe(18)
        if not self._stdin_is_tty():
            raise CommandError(
                "There is no terminal to prompt for a password on. Run this with a "
                "terminal (docker exec -it ...) or pass --generate to print a random password."
            )

        try:
            for _ in range(MAX_PASSWORD_TRIES):
                password = getpass.getpass("New password: ")
                if password != getpass.getpass("Confirm new password: "):
                    self.stderr.write("Passwords do not match. Try again.")
                    continue
                try:
                    password_validation.validate_password(password, user=user)
                except ValidationError as exc:
                    self.stderr.write("\n".join(exc.messages))
                    continue
                return password
        except KeyboardInterrupt:
            raise CommandError("Cancelled. Nothing was changed.")
        raise CommandError(
            f"No valid password after {MAX_PASSWORD_TRIES} tries. Nothing was changed."
        )

    def _show_generated(self, password, options):
        if not options["generate"]:
            return
        self.stdout.write(f"  Password: {password}")
        self.stdout.write("  Sign in with it, then change it under Settings.")
