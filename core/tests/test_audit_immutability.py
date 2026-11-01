"""
R7 — the audit trail is immutable. An existing AuditEntry cannot be updated
or deleted through the ORM, through the admin, by a superuser, or (on
PostgreSQL) by raw SQL.
"""

from unittest import skipUnless

from django.contrib import admin as django_admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from django.db import DatabaseError, connection, transaction
from django.db.models import ProtectedError
from django.test import RequestFactory, TestCase
from django.urls import reverse

from core.admin import AuditEntryAdmin
from core.models import AuditAction, AuditEntry, AuditTrailImmutableError

ORIGINAL_CHANGES = {"name": [None, "Muhimbili National Hospital"]}


def make_entry(**kwargs):
    defaults = dict(
        action=AuditAction.CREATE,
        model="core.Client",
        object_id="1",
        object_repr="Muhimbili National Hospital",
        changes=ORIGINAL_CHANGES,
    )
    defaults.update(kwargs)
    return AuditEntry.objects.create(**defaults)


class ImmutabilityAssertions:
    def assertEntryUntouched(self, entry):
        stored = AuditEntry.objects.get(pk=entry.pk)  # also proves it still exists
        self.assertEqual(stored.action, AuditAction.CREATE)
        self.assertEqual(stored.object_id, "1")
        self.assertEqual(stored.changes, ORIGINAL_CHANGES)
        self.assertEqual(stored.timestamp, entry.timestamp)


class AuditEntryORMImmutabilityTests(ImmutabilityAssertions, TestCase):
    def setUp(self):
        self.entry = make_entry()

    def test_insert_is_allowed(self):
        self.assertTrue(AuditEntry.objects.filter(pk=self.entry.pk).exists())

    def test_save_on_existing_instance_raises(self):
        self.entry.action = AuditAction.DELETE
        with self.assertRaises(AuditTrailImmutableError):
            self.entry.save()
        self.assertEntryUntouched(self.entry)

    def test_save_on_freshly_fetched_instance_raises(self):
        fetched = AuditEntry.objects.get(pk=self.entry.pk)
        fetched.changes = {"tampered": True}
        with self.assertRaises(AuditTrailImmutableError):
            fetched.save()
        self.assertEntryUntouched(self.entry)

    def test_save_with_update_fields_raises(self):
        self.entry.object_repr = "tampered"
        with self.assertRaises(AuditTrailImmutableError):
            self.entry.save(update_fields=["object_repr"])
        self.assertEntryUntouched(self.entry)

    def test_save_with_force_update_raises(self):
        self.entry.object_repr = "tampered"
        with self.assertRaises(AuditTrailImmutableError):
            self.entry.save(force_update=True)
        self.assertEntryUntouched(self.entry)

    def test_reinserting_with_existing_pk_fails(self):
        # Copy the row and try to "overwrite" it by forcing the same pk.
        clone = AuditEntry(
            pk=self.entry.pk,
            action=AuditAction.DELETE,
            model="core.Client",
            object_id="1",
        )
        with self.assertRaises(DatabaseError), transaction.atomic():
            clone.save()  # force_insert → primary-key violation, not an update
        self.assertEntryUntouched(self.entry)

    def test_queryset_update_raises(self):
        with self.assertRaises(AuditTrailImmutableError):
            AuditEntry.objects.filter(pk=self.entry.pk).update(object_repr="tampered")
        with self.assertRaises(AuditTrailImmutableError):
            AuditEntry.objects.all().update(action=AuditAction.DELETE)
        self.assertEntryUntouched(self.entry)

    def test_bulk_update_raises(self):
        self.entry.object_repr = "tampered"
        with self.assertRaises(AuditTrailImmutableError):
            AuditEntry.objects.bulk_update([self.entry], ["object_repr"])
        self.assertEntryUntouched(self.entry)

    def test_update_or_create_on_existing_raises(self):
        with self.assertRaises(AuditTrailImmutableError):
            AuditEntry.objects.update_or_create(
                pk=self.entry.pk, defaults={"object_repr": "tampered"}
            )
        self.assertEntryUntouched(self.entry)

    def test_instance_delete_raises(self):
        with self.assertRaises(AuditTrailImmutableError):
            self.entry.delete()
        self.assertEntryUntouched(self.entry)

    def test_queryset_delete_raises(self):
        with self.assertRaises(AuditTrailImmutableError):
            AuditEntry.objects.filter(pk=self.entry.pk).delete()
        with self.assertRaises(AuditTrailImmutableError):
            AuditEntry.objects.all().delete()
        self.assertEntryUntouched(self.entry)

    def test_base_manager_is_also_append_only(self):
        with self.assertRaises(AuditTrailImmutableError):
            AuditEntry._base_manager.all().delete()
        with self.assertRaises(AuditTrailImmutableError):
            AuditEntry._base_manager.all().update(object_repr="tampered")
        self.assertEntryUntouched(self.entry)

    def test_deleting_the_actor_is_blocked_rather_than_rewriting_entries(self):
        # SET_NULL or CASCADE would update/delete audit rows; PROTECT refuses.
        user = get_user_model().objects.create_user(
            email="gift@imperialeastafrica.tz", password="x", role="PM"
        )
        entry = make_entry(actor=user)
        with self.assertRaises(ProtectedError):
            user.delete()
        self.assertEqual(AuditEntry.objects.get(pk=entry.pk).actor_id, user.pk)

    def test_only_view_permission_exists(self):
        ct = ContentType.objects.get_for_model(AuditEntry)
        codenames = set(
            Permission.objects.filter(content_type=ct).values_list("codename", flat=True)
        )
        self.assertEqual(codenames, {"view_auditentry"})


class AuditEntryAdminImmutabilityTests(ImmutabilityAssertions, TestCase):
    """A Django superuser gets every permission by default. Not here."""

    @classmethod
    def setUpTestData(cls):
        cls.superuser = get_user_model().objects.create_superuser(
            email="root@imperialeastafrica.tz", password="x", role="ADMIN",
            must_change_password=False,
        )

    def setUp(self):
        self.entry = make_entry()
        self.client.force_login(self.superuser)
        self.change_url = reverse("admin:core_auditentry_change", args=[self.entry.pk])
        self.delete_url = reverse("admin:core_auditentry_delete", args=[self.entry.pk])
        self.changelist_url = reverse("admin:core_auditentry_changelist")

    def test_superuser_can_view_the_trail(self):
        response = self.client.get(self.changelist_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Muhimbili National Hospital")

    def test_change_page_is_read_only_for_superuser(self):
        response = self.client.get(self.change_url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'name="_save"')
        self.assertNotContains(response, 'name="_continue"')

    def test_superuser_cannot_post_a_change(self):
        response = self.client.post(
            self.change_url,
            {
                "action": AuditAction.DELETE,
                "model": "core.Client",
                "object_id": "999",
                "object_repr": "tampered",
                "changes": "{}",
                "timestamp_0": "2026-01-01",
                "timestamp_1": "00:00:00",
            },
        )
        self.assertEqual(response.status_code, 403)
        self.assertEntryUntouched(self.entry)

    def test_superuser_cannot_open_delete_page(self):
        self.assertEqual(self.client.get(self.delete_url).status_code, 403)

    def test_superuser_cannot_post_delete(self):
        response = self.client.post(self.delete_url, {"post": "yes"})
        self.assertEqual(response.status_code, 403)
        self.assertEntryUntouched(self.entry)

    def test_superuser_cannot_bulk_delete_from_changelist(self):
        self.client.post(
            self.changelist_url,
            {"action": "delete_selected", "_selected_action": [self.entry.pk], "post": "yes"},
        )
        self.assertEntryUntouched(self.entry)

    def test_superuser_cannot_add(self):
        before = AuditEntry.objects.count()
        response = self.client.get(reverse("admin:core_auditentry_add"))
        self.assertEqual(response.status_code, 403)
        response = self.client.post(
            reverse("admin:core_auditentry_add"),
            {"action": AuditAction.CREATE, "model": "x.Y", "object_id": "1"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(AuditEntry.objects.filter(model="x.Y").exists())
        self.assertEqual(AuditEntry.objects.count(), before)

    def test_modeladmin_denies_superuser_at_method_level(self):
        request = RequestFactory().get("/")
        request.user = self.superuser
        model_admin = AuditEntryAdmin(AuditEntry, django_admin.site)
        self.assertFalse(model_admin.has_add_permission(request))
        self.assertFalse(model_admin.has_change_permission(request, self.entry))
        self.assertFalse(model_admin.has_delete_permission(request, self.entry))
        self.assertTrue(model_admin.has_view_permission(request, self.entry))
        self.assertEqual(model_admin.get_actions(request), {})


@skipUnless(connection.vendor == "postgresql", "database trigger is PostgreSQL-only")
class AuditEntryDatabaseTriggerTests(ImmutabilityAssertions, TestCase):
    """Even raw SQL that bypasses Django entirely is refused by the database."""

    def setUp(self):
        self.entry = make_entry()
        self.table = AuditEntry._meta.db_table

    def test_trigger_is_installed(self):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT tgname FROM pg_trigger WHERE tgrelid = %s::regclass AND NOT tgisinternal",
                [self.table],
            )
            names = {row[0] for row in cursor.fetchall()}
        self.assertIn("core_auditentry_append_only", names)

    def test_raw_sql_update_is_rejected(self):
        with self.assertRaises(DatabaseError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    f"UPDATE {self.table} SET object_repr = 'tampered' WHERE id = %s",
                    [self.entry.pk],
                )
        self.assertEntryUntouched(self.entry)

    def test_raw_sql_delete_is_rejected(self):
        with self.assertRaises(DatabaseError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(f"DELETE FROM {self.table} WHERE id = %s", [self.entry.pk])
        self.assertEntryUntouched(self.entry)

    def test_raw_sql_insert_is_still_allowed(self):
        before = AuditEntry.objects.count()
        make_entry(object_id="2")
        self.assertEqual(AuditEntry.objects.count(), before + 1)
