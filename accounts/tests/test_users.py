"""User model rules from §2: rank, one identity per address, audited manager."""

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from accounts.models import ROLE_RANK, EmailAlias, Role, User
from core.models import AuditBypassError


def make(role, email, **extra):
    return User.objects.create_user(email=email, password="x", role=role, **extra)


class RankTests(TestCase):
    def test_rank_follows_role(self):
        expected = {"EXEC": 1, "HOPS": 2, "PM": 3, "AUDIT": 3, "FIN": 3, "CLEAR": 4, "ADMIN": None}
        for role, rank in expected.items():
            with self.subTest(role=role):
                self.assertEqual(make(role, f"{role.lower()}@imperialeastafrica.tz").rank, rank)

    def test_rank_moves_with_a_role_change(self):
        user = make(Role.PROJECT_MANAGER, "p@imperialeastafrica.tz")
        user.role = Role.HEAD_OF_OPERATIONS
        user.save()
        user.refresh_from_db()
        self.assertEqual(user.rank, 2)

    def test_database_refuses_a_rank_that_disagrees_with_role(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.bulk_create([User(email="raw@imperialeastafrica.tz", role=Role.PROJECT_MANAGER, rank=1)])

    def test_rank_table_covers_every_role(self):
        self.assertEqual(set(ROLE_RANK), set(Role.values))


class EmailIdentityTests(TestCase):
    def setUp(self):
        self.gift = make(Role.PROJECT_MANAGER, "Gift.Minja@ImperialEastAfrica.tz")

    def test_email_is_stored_lower_case_and_unique_case_insensitively(self):
        self.assertEqual(self.gift.email, "gift.minja@imperialeastafrica.tz")
        with self.assertRaises((ValidationError, IntegrityError)), transaction.atomic():
            make(Role.AUDITOR, "GIFT.MINJA@imperialeastafrica.tz")

    def test_alias_cannot_be_anyone_s_primary_email(self):
        other = make(Role.AUDITOR, "sylvia@imperialeastafrica.tz")
        with self.assertRaises(ValidationError):
            EmailAlias.objects.create(user=other, address="gift.minja@IMPERIALEASTAFRICA.tz")
        with self.assertRaises(ValidationError):
            EmailAlias.objects.create(user=self.gift, address="gift.minja@imperialeastafrica.tz")

    def test_alias_is_unique_across_users(self):
        EmailAlias.objects.create(user=self.gift, address="gift@komeeastafrica.tz")
        other = make(Role.AUDITOR, "sylvia@imperialeastafrica.tz")
        with self.assertRaises((ValidationError, IntegrityError)), transaction.atomic():
            EmailAlias.objects.create(user=other, address="GIFT@komeeastafrica.tz")

    def test_primary_email_cannot_take_an_existing_alias(self):
        EmailAlias.objects.create(user=self.gift, address="gift@keapharma.co.tz")
        with self.assertRaises(ValidationError):
            make(Role.AUDITOR, "gift@keapharma.co.tz")
        self.gift.email = "gift@keapharma.co.tz"  # nor can its own owner
        with self.assertRaises(ValidationError):
            self.gift.save()

    def test_aliases_cannot_sign_in(self):
        EmailAlias.objects.create(user=self.gift, address="gift@komeeastafrica.tz")
        self.gift.must_change_password = False
        self.gift.save()
        self.assertFalse(self.client.login(username="gift@komeeastafrica.tz", password="x"))
        self.assertTrue(self.client.login(username="gift.minja@imperialeastafrica.tz", password="x"))


class ManagerTests(TestCase):
    def test_manager_combines_user_manager_with_audited_queryset(self):
        make(Role.PROJECT_MANAGER, "p@imperialeastafrica.tz")
        with self.assertRaises(AuditBypassError):
            User.objects.filter(email="p@imperialeastafrica.tz").update(role=Role.EXECUTIVE)
        self.assertTrue(hasattr(User.objects, "create_superuser"))
        self.assertEqual(User.objects.get_by_natural_key("P@imperialeastafrica.tz").role, Role.PROJECT_MANAGER)

    def test_new_users_must_change_password_by_default(self):
        self.assertTrue(make(Role.FINANCE, "f@imperialeastafrica.tz").must_change_password)
