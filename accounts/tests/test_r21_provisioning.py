"""
R21 — accounts are created only by EXEC or ADMIN, and the first sign-in forces
a password change. No self-registration. The temporary password is
system-generated, emailed once, never shown to its creator, and expires after
7 days. While must_change_password is true, middleware blocks every page
except the change-password form — including the admin. Role is mandatory.
"""

import datetime
import re
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Role, User
from accounts.provisioning import (
    TEMPORARY_PASSWORD_LIFETIME,
    create_user_account,
    reissue_temporary_password,
)
from core.models import AuditAction, AuditEntry

PASSWORD_LINE = re.compile(r"Temporary password: (\S+)")


def make_user(role, name, **extra):
    extra.setdefault("must_change_password", False)
    return User.objects.create_user(
        email=f"{name}@imperialeastafrica.tz", password="x", role=role,
        first_name=name.title(), last_name="Test", **extra,
    )


def emailed_password(message):
    return PASSWORD_LINE.search(message.body).group(1)


class ProvisioningFixtures:
    @classmethod
    def setUpTestData(cls):
        cls.md = make_user(Role.EXECUTIVE, "faraja")
        cls.admin_user = make_user(Role.ADMIN, "janeth", is_staff=True, is_superuser=True)
        cls.others = [
            make_user(Role.HEAD_OF_OPERATIONS, "mariam"),
            make_user(Role.PROJECT_MANAGER, "gift"),
            make_user(Role.AUDITOR, "sylvia"),
            make_user(Role.FINANCE, "anna"),
            make_user(Role.CLEARING_AGENT, "agent"),
        ]

    def provision(self, by=None, email="alice@imperialeastafrica.tz", role=Role.PROJECT_MANAGER):
        return create_user_account(
            created_by=by or self.md, email=email, first_name="Alice", last_name="Joseph", role=role
        )


class WhoCanCreateAccountsTests(ProvisioningFixtures, TestCase):
    def test_exec_and_admin_can_create_accounts(self):
        self.provision(by=self.md, email="a@imperialeastafrica.tz")
        self.provision(by=self.admin_user, email="b@imperialeastafrica.tz")
        self.assertEqual(User.objects.filter(email__in=["a@imperialeastafrica.tz", "b@imperialeastafrica.tz"]).count(), 2)

    def test_every_other_role_is_refused(self):
        for user in self.others:
            with self.subTest(role=user.role), self.assertRaises(PermissionDenied):
                self.provision(by=user)
        self.assertFalse(User.objects.filter(email="alice@imperialeastafrica.tz").exists())
        self.assertEqual(mail.outbox, [])

    def test_a_superuser_without_a_provisioning_role_is_refused(self):
        hops_superuser = make_user(Role.HEAD_OF_OPERATIONS, "dinna", is_staff=True, is_superuser=True)
        with self.assertRaises(PermissionDenied):
            self.provision(by=hops_superuser)

    def test_there_is_no_sign_up_page(self):
        for path in ("/signup/", "/register/", "/accounts/signup/", "/account/register/"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)


class TemporaryPasswordTests(ProvisioningFixtures, TestCase):
    def test_new_account_state(self):
        before = timezone.now()
        user = self.provision()
        self.assertTrue(user.must_change_password)
        expected = before + TEMPORARY_PASSWORD_LIFETIME
        self.assertAlmostEqual(
            user.temporary_password_expires_at.timestamp(), expected.timestamp(), delta=5
        )
        self.assertEqual(TEMPORARY_PASSWORD_LIFETIME, datetime.timedelta(days=7))

    def test_password_is_emailed_once_to_the_new_user_only(self):
        user = self.provision()
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, [user.email])
        self.assertNotIn(self.md.email, message.to + message.cc + message.bcc)
        self.assertIn(user.email, message.body)
        password = emailed_password(message)
        self.assertTrue(user.check_password(password))

    def test_password_is_never_returned_or_stored_readably(self):
        user = self.provision()
        password = emailed_password(mail.outbox[0])
        self.assertIsInstance(user, User)  # the creator gets the account, not the password
        self.assertNotIn(password, user.password)  # only a hash is stored
        for entry in AuditEntry.objects.filter(model="accounts.User", object_id=str(user.pk)):
            self.assertNotIn(password, str(entry.changes))
            self.assertEqual(entry.changes["password"][1], "[redacted]")

    def test_creation_is_audited_against_the_creator(self):
        user = self.provision(by=self.admin_user)
        entry = AuditEntry.objects.get(model="accounts.User", object_id=str(user.pk))
        self.assertEqual(entry.action, AuditAction.CREATE)
        self.assertEqual(entry.actor, self.admin_user)
        self.assertEqual(entry.changes["role"], [None, Role.PROJECT_MANAGER])

    def test_if_the_email_cannot_be_sent_no_account_is_created(self):
        with mock.patch("accounts.provisioning.send_mail", side_effect=OSError("SMTP down")):
            with self.assertRaises(OSError):
                self.provision()
        self.assertFalse(User.objects.filter(email="alice@imperialeastafrica.tz").exists())

    def test_reissue_replaces_the_password_and_resets_expiry(self):
        user = self.provision()
        old = emailed_password(mail.outbox[0])
        reissue_temporary_password(issued_by=self.md, user=user)
        user.refresh_from_db()
        new = emailed_password(mail.outbox[1])
        self.assertNotEqual(old, new)
        self.assertFalse(user.check_password(old))
        self.assertTrue(user.check_password(new))
        self.assertTrue(user.must_change_password)

    def test_only_exec_or_admin_can_reissue(self):
        user = self.provision()
        for other in self.others:
            with self.subTest(role=other.role), self.assertRaises(PermissionDenied):
                reissue_temporary_password(issued_by=other, user=user)


class RoleIsMandatoryTests(ProvisioningFixtures, TestCase):
    def test_provisioning_without_role_is_refused(self):
        with self.assertRaises(ValidationError):
            self.provision(role="")
        self.assertEqual(mail.outbox, [])

    def test_role_is_in_required_fields_for_createsuperuser(self):
        self.assertIn("role", get_user_model().REQUIRED_FIELDS)

    def test_manager_refuses_a_roleless_user(self):
        with self.assertRaises(ValueError):
            User.objects.create_superuser(email="root@imperialeastafrica.tz", password="x")

    def test_model_refuses_an_invalid_role(self):
        with self.assertRaises(ValidationError):
            User(email="x@imperialeastafrica.tz", role="CEO").save()

    def test_database_refuses_a_roleless_row(self):
        # bulk_create skips save(); the CHECK constraint is the last line.
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.bulk_create([User(email="raw@imperialeastafrica.tz", role="")])


class FirstSignInTests(ProvisioningFixtures, TestCase):
    def setUp(self):
        self.user = self.provision()
        self.temp = emailed_password(mail.outbox[0])

    def sign_in(self, password=None, email=None):
        return self.client.post(
            reverse("login"), {"username": email or self.user.email, "password": password or self.temp}
        )

    def test_sign_in_with_the_emailed_password_lands_on_change_password(self):
        response = self.sign_in()
        self.assertEqual(response.status_code, 302)
        response = self.client.get(reverse("dashboard"))
        self.assertRedirects(response, reverse("password_change"))

    def test_email_is_matched_case_insensitively(self):
        self.sign_in(email="ALICE@ImperialEastAfrica.tz")
        self.assertIn("_auth_user_id", self.client.session)

    def test_every_other_page_is_blocked_including_admin(self):
        self.sign_in()
        for path in ("/", "/admin/", "/admin/accounts/user/", "/admin/password_change/", "/anything/"):
            with self.subTest(path=path):
                self.assertRedirects(
                    self.client.get(path), reverse("password_change"), fetch_redirect_response=False
                )
        response = self.client.post("/admin/accounts/user/add/", {"email": "x@imperialeastafrica.tz"})
        self.assertRedirects(response, reverse("password_change"), fetch_redirect_response=False)

    def test_change_password_page_and_logout_are_reachable(self):
        self.sign_in()
        self.assertEqual(self.client.get(reverse("password_change")).status_code, 200)
        self.assertEqual(self.client.post(reverse("logout")).status_code, 302)

    def test_changing_the_password_unlocks_the_account_and_is_audited(self):
        self.sign_in()
        response = self.client.post(
            reverse("password_change"),
            {"old_password": self.temp, "new_password1": "Centrifuge-Mwanza-2026", "new_password2": "Centrifuge-Mwanza-2026"},
        )
        self.assertRedirects(response, reverse("dashboard"))
        self.user.refresh_from_db()
        self.assertFalse(self.user.must_change_password)
        self.assertIsNone(self.user.temporary_password_expires_at)
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)

        entry = AuditEntry.objects.filter(
            model="accounts.User", object_id=str(self.user.pk), action=AuditAction.UPDATE,
            changes__has_key="must_change_password",
        ).get()
        self.assertEqual(entry.actor, self.user)
        self.assertEqual(entry.changes["must_change_password"], [True, False])
        self.assertEqual(entry.changes["password"], ["[redacted]", "[redacted]"])

    def test_the_temporary_password_cannot_be_reused_as_the_new_one(self):
        self.sign_in()
        self.client.post(
            reverse("password_change"),
            {"old_password": self.temp, "new_password1": self.temp, "new_password2": self.temp},
        )
        self.user.refresh_from_db()
        self.assertTrue(self.user.must_change_password)


class ExpiryTests(ProvisioningFixtures, TestCase):
    def setUp(self):
        self.user = self.provision()
        self.temp = emailed_password(mail.outbox[0])

    def expire(self):
        self.user.refresh_from_db()
        self.user.temporary_password_expires_at = timezone.now() - datetime.timedelta(seconds=1)
        self.user.save()

    def test_expired_temporary_password_cannot_sign_in(self):
        self.expire()
        self.client.post(reverse("login"), {"username": self.user.email, "password": self.temp})
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_an_open_session_ends_when_the_temporary_password_expires(self):
        self.client.post(reverse("login"), {"username": self.user.email, "password": self.temp})
        self.assertIn("_auth_user_id", self.client.session)
        self.expire()
        response = self.client.get(reverse("dashboard"))
        self.assertFalse(response.wsgi_request.user.is_authenticated)
        self.assertRedirects(response, f"{reverse('login')}?next=/", fetch_redirect_response=False)

    def test_a_reissued_password_works_again(self):
        self.expire()
        reissue_temporary_password(issued_by=self.md, user=self.user)
        new = emailed_password(mail.outbox[-1])
        self.client.post(reverse("login"), {"username": self.user.email, "password": new})
        self.assertIn("_auth_user_id", self.client.session)


class AdminProvisioningTests(ProvisioningFixtures, TestCase):
    def test_add_form_has_no_password_field_and_provisions(self):
        self.client.force_login(self.admin_user)
        page = self.client.get(reverse("admin:accounts_user_add"))
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, 'name="password')
        response = self.client.post(
            reverse("admin:accounts_user_add"),
            {"first_name": "Anna", "last_name": "Matiku", "email": "anna.m@imperialeastafrica.tz", "role": Role.AUDITOR},
        )
        self.assertEqual(response.status_code, 302)
        user = User.objects.get(email="anna.m@imperialeastafrica.tz")
        self.assertTrue(user.must_change_password)
        self.assertEqual(mail.outbox[-1].to, [user.email])
        self.assertEqual(AuditEntry.objects.get(model="accounts.User", object_id=str(user.pk)).actor, self.admin_user)

    def test_admin_add_refused_for_superuser_without_provisioning_role(self):
        hops_superuser = make_user(Role.HEAD_OF_OPERATIONS, "dinna", is_staff=True, is_superuser=True)
        self.client.force_login(hops_superuser)
        self.assertEqual(self.client.get(reverse("admin:accounts_user_add")).status_code, 403)

    def test_admin_cannot_set_another_users_password(self):
        self.client.force_login(self.admin_user)
        target = self.others[0]
        response = self.client.get(f"/admin/accounts/user/{target.pk}/password/")
        self.assertIn(response.status_code, (302, 404))  # no password-setting view exists
        self.assertNotEqual(response.status_code, 200)
