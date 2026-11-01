"""§7 Supplier: master data, audited (R7), no dependency on tenders or items (§1)."""

import ast
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import SimpleTestCase, TestCase

from core.models import AuditAction, AuditEntry
from suppliers.models import Supplier


class SupplierFieldTests(TestCase):
    def test_only_name_is_required_and_is_oem_defaults_to_false(self):
        supplier = Supplier(name="Mettler Toledo")
        supplier.full_clean()  # blank email/phone/etc. are valid: legacy data is sparse (§12)
        supplier.save()
        self.assertFalse(supplier.is_oem)
        self.assertEqual(str(supplier), "Mettler Toledo")

    def test_all_section_7_fields_round_trip(self):
        Supplier.objects.create(
            name="Shimadzu", is_oem=True, contact_person="K. Tanaka",
            email="sales@shimadzu.example", phone="+81 75 823 1111", country="Japan", notes="MAF on file",
        )
        s = Supplier.objects.get()
        self.assertEqual(
            (s.is_oem, s.contact_person, s.email, s.phone, s.country, s.notes),
            (True, "K. Tanaka", "sales@shimadzu.example", "+81 75 823 1111", "Japan", "MAF on file"),
        )

    def test_name_is_required(self):
        with self.assertRaises(ValidationError):
            Supplier(name="").full_clean()

    def test_invalid_email_is_rejected(self):
        with self.assertRaises(ValidationError):
            Supplier(name="X Ltd", email="not-an-email").full_clean()

    def test_name_whitespace_is_collapsed_on_save(self):
        self.assertEqual(Supplier.objects.create(name="  Agilent   Technologies ").name, "Agilent Technologies")

    def test_names_are_unique_ignoring_case_and_spacing(self):
        Supplier.objects.create(name="Thermo Fisher")
        for duplicate in ("thermo fisher", "THERMO FISHER", " Thermo   Fisher "):
            with self.subTest(duplicate=duplicate), self.assertRaises(IntegrityError), transaction.atomic():
                Supplier.objects.create(name=duplicate)


class SupplierAuditTests(TestCase):
    def test_create_update_delete_are_audited_with_the_actor(self):
        user = get_user_model().objects.create_user(
            email="gift@imperialeastafrica.tz", password="x", role="PM"
        )
        s = Supplier(name="Sartorius")
        s.save(actor=user)
        s.is_oem = True
        s.save(actor=user)
        pk = str(s.pk)
        s.delete(actor=user)

        entries = AuditEntry.objects.filter(model="suppliers.Supplier", object_id=pk).order_by("id")
        self.assertEqual(
            [e.action for e in entries], [AuditAction.CREATE, AuditAction.UPDATE, AuditAction.DELETE]
        )
        self.assertTrue(all(e.actor == user for e in entries))
        self.assertEqual(entries[1].changes, {"is_oem": [False, True]})

    def test_queryset_update_is_refused(self):
        from core.models import AuditBypassError

        Supplier.objects.create(name="Waters")
        with self.assertRaises(AuditBypassError):
            Supplier.objects.update(is_oem=True)


class SupplierDependencyTests(SimpleTestCase):
    """§1/§4: suppliers imports only from apps above it, never tenders or items."""

    FORBIDDEN = {"tenders", "items", "audits", "approvals", "postaward", "comms", "tasks", "dashboards"}

    def test_suppliers_package_imports_nothing_below_it(self):
        root = Path(__file__).resolve().parent.parent
        for path in root.rglob("*.py"):
            if "migrations" in path.parts:
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    names = [node.module]
                for name in names:
                    with self.subTest(file=path.name, name=name):
                        self.assertNotIn(name.split(".")[0], self.FORBIDDEN)

    def test_supplier_has_no_foreign_keys_to_other_domain_models(self):
        relations = [f for f in Supplier._meta.get_fields() if f.is_relation and not f.auto_created]
        self.assertEqual(relations, [])
