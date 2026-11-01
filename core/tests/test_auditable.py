"""
The Auditable base writes an AuditEntry for every create, update, status
change and delete (R7), attributed to the acting user, in the same
transaction as the change.
"""

from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, models, transaction
from django.test import TestCase
from django.test.utils import isolate_apps
from django.urls import reverse

from core.audit import audit_actor
from core.models import AuditAction, AuditBypassError, AuditEntry, Auditable, Client


def entries_for(obj_or_pk, model="core.Client"):
    pk = getattr(obj_or_pk, "pk", obj_or_pk)
    return AuditEntry.objects.filter(model=model, object_id=str(pk)).order_by("id")


class AuditableClientTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.md = get_user_model().objects.create_user(
            email="faraja@imperialeastafrica.tz", password="x", role="EXEC"
        )

    def test_create_writes_create_entry_with_every_field(self):
        client = Client.objects.create(name="Muhimbili National Hospital", short_name="MNH")
        [entry] = entries_for(client)
        self.assertEqual(entry.action, AuditAction.CREATE)
        self.assertIsNone(entry.actor)  # no actor set → recorded as system
        self.assertEqual(entry.changes["name"], [None, "Muhimbili National Hospital"])
        self.assertEqual(entry.changes["short_name"], [None, "MNH"])
        self.assertIn("created_at", entry.changes)
        self.assertNotIn("updated_at", entry.changes)  # excluded as noise
        self.assertEqual(entry.object_repr, "Muhimbili National Hospital (MNH)")

    def test_actor_from_save_kwarg(self):
        client = Client(name="TBS")
        client.save(actor=self.md)
        self.assertEqual(entries_for(client).get().actor, self.md)

    def test_actor_from_context(self):
        with audit_actor(self.md):
            client = Client.objects.create(name="TBS")
        self.assertEqual(entries_for(client).get().actor, self.md)

    def test_update_records_only_changed_fields(self):
        client = Client.objects.create(name="Medical Stores Department")
        client.short_name = "MSD"
        client.save(actor=self.md)
        create, update = entries_for(client)
        self.assertEqual(update.action, AuditAction.UPDATE)
        self.assertEqual(update.actor, self.md)
        self.assertEqual(update.changes, {"short_name": ["", "MSD"]})

    def test_save_without_changes_writes_nothing(self):
        client = Client.objects.create(name="NIMR")
        client.save()
        self.assertEqual(entries_for(client).count(), 1)

    def test_update_fields_is_respected(self):
        client = Client.objects.create(name="DIT")
        client.short_name = "DIT"
        client.name = "not saved"
        client.save(update_fields=["short_name"])
        self.assertEqual(entries_for(client).last().changes, {"short_name": ["", "DIT"]})
        client.refresh_from_db()
        self.assertEqual(client.name, "DIT")

    def test_delete_writes_delete_entry_that_outlives_the_object(self):
        client = Client.objects.create(name="TANAPA")
        pk = client.pk
        client.delete(actor=self.md)
        self.assertFalse(Client.objects.filter(pk=pk).exists())
        entry = entries_for(pk).last()
        self.assertEqual(entry.action, AuditAction.DELETE)
        self.assertEqual(entry.actor, self.md)
        self.assertEqual(entry.changes["name"], ["TANAPA", None])

    def test_queryset_delete_audits_each_row(self):
        a = Client.objects.create(name="TCAA")
        b = Client.objects.create(name="GASCO")
        Client.objects.filter(pk__in=[a.pk, b.pk]).delete()
        for pk in (a.pk, b.pk):
            self.assertEqual(entries_for(pk).last().action, AuditAction.DELETE)

    def test_queryset_update_is_refused(self):
        client = Client.objects.create(name="MUHAS")
        with self.assertRaises(AuditBypassError):
            Client.objects.filter(pk=client.pk).update(name="tampered")
        client.refresh_from_db()
        self.assertEqual(client.name, "MUHAS")

    def test_bulk_update_is_refused(self):
        client = Client.objects.create(name="MUHAS")
        client.name = "tampered"
        with self.assertRaises(AuditBypassError):
            Client.objects.bulk_update([client], ["name"])

    def test_bulk_create_audits_each_row(self):
        with audit_actor(self.md):
            created = Client.objects.bulk_create(
                [Client(name="Mkwawa University"), Client(name="University of Dar es Salaam")]
            )
        for client in created:
            entry = entries_for(client).get()
            self.assertEqual(entry.action, AuditAction.CREATE)
            self.assertEqual(entry.actor, self.md)

    def test_rolled_back_change_leaves_no_entry(self):
        with self.assertRaises(RuntimeError), transaction.atomic():
            Client.objects.create(name="Government Chemist Laboratory Authority")
            raise RuntimeError
        self.assertFalse(AuditEntry.objects.filter(model="core.Client").exists())

    def test_client_name_is_unique_case_insensitively(self):
        Client.objects.create(name="Jakaya Kikwete Cardiac Institute")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Client.objects.create(name="JAKAYA KIKWETE CARDIAC INSTITUTE")


class AuditActorMiddlewareTests(TestCase):
    def test_admin_save_is_attributed_to_the_logged_in_user(self):
        user = get_user_model().objects.create_superuser(
            email="root@imperialeastafrica.tz", password="x", role="ADMIN",
            must_change_password=False,
        )
        self.client.force_login(user)
        response = self.client.post(
            reverse("admin:core_client_add"), {"name": "Mt. Meru Regional Referral Hospital"}
        )
        self.assertEqual(response.status_code, 302)
        client = Client.objects.get()
        self.assertEqual(entries_for(client).get().actor, user)


@skipUnless(connection.vendor == "postgresql", "creates a throwaway table inside a transaction")
class StatusChangeActionTests(TestCase):
    """A save that changes a field named `status` is recorded as STATUS_CHANGE."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._isolation = isolate_apps("core")
        cls._isolation.enable()

        class Widget(Auditable):
            status = models.CharField(max_length=32, default="DRAFTING")
            note = models.CharField(max_length=50, blank=True)

            class Meta:
                app_label = "core"

        cls.Widget = Widget
        with connection.schema_editor() as editor:
            editor.create_model(Widget)

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()  # rolls back the table with the class transaction
        cls._isolation.disable()

    def test_status_change_action(self):
        w = self.Widget.objects.create()
        w.note = "x"
        w.save()
        w.status = "COSTING"
        w.save()
        actions = list(entries_for(w, "core.Widget").values_list("action", flat=True))
        self.assertEqual(
            actions, [AuditAction.CREATE, AuditAction.UPDATE, AuditAction.STATUS_CHANGE]
        )
        self.assertEqual(
            entries_for(w, "core.Widget").last().changes, {"status": ["DRAFTING", "COSTING"]}
        )
