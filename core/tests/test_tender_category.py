"""§8 "Tender category": a lookup table in core, seeded from real usage, not an enum."""

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase

from core.models import AuditAction, AuditEntry, TenderCategory

# CLAUDE.md §8, verbatim.
SEEDED = {
    "ANALYTICAL", "METROLOGY", "ENERGY AND RENEWABLE", "MECHANICAL", "CLINICAL LAB",
    "METEOROLOGY", "WATER TREATMENT", "AVIATION", "CHEMICAL/HYDROCARBON LAB", "INDUSTRIAL",
    "TEXTILE", "LEATHER", "PPE", "SPARE PARTS", "METALLURGY", "TESTING EQUIPMENT",
    "CLEANING SUPPLIES",
}


class SeedTests(TestCase):
    def test_seeded_with_exactly_the_real_values(self):
        self.assertEqual(set(TenderCategory.objects.values_list("name", flat=True)), SEEDED)
        self.assertEqual(len(SEEDED), 17)

    def test_each_seeded_row_has_a_create_audit_entry_from_the_system(self):
        for category in TenderCategory.objects.all():
            with self.subTest(name=category.name):
                entry = AuditEntry.objects.get(model="core.TenderCategory", object_id=str(category.pk))
                self.assertEqual(entry.action, AuditAction.CREATE)
                self.assertIsNone(entry.actor)
                self.assertEqual(entry.changes["name"], [None, category.name])


class LookupTableBehaviourTests(TestCase):
    def test_a_new_category_needs_no_migration_and_is_audited(self):
        # get_user_model(), not `from accounts...`: core sits above accounts (§1).
        user = get_user_model().objects.create_user(
            email="faraja@imperialeastafrica.tz", password="x", role="EXEC"
        )
        category = TenderCategory(name="Cold Chain")
        category.save(actor=user)
        self.assertEqual(category.name, "COLD CHAIN")
        entry = AuditEntry.objects.get(model="core.TenderCategory", object_id=str(category.pk))
        self.assertEqual(entry.actor, user)

    def test_names_are_normalised_to_upper_case_single_spaced(self):
        self.assertEqual(TenderCategory.objects.create(name="  clinical   hematology ").name, "CLINICAL HEMATOLOGY")

    def test_names_are_unique_ignoring_case_and_spacing(self):
        for duplicate in ("metrology", "Metrology", " METROLOGY "):
            with self.subTest(duplicate=duplicate), self.assertRaises(IntegrityError), transaction.atomic():
                TenderCategory.objects.create(name=duplicate)
