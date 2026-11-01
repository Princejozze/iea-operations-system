"""
Seed TenderCategory from real legacy usage (CLAUDE.md §8 "Tender category").

Historic counts, where §8 gives them: ANALYTICAL 99, METROLOGY 17,
ENERGY AND RENEWABLE 16, MECHANICAL 15.

Historical models bypass Auditable.save(), so this writes the AuditEntry for
each seeded row itself (R7: every create is audited). actor=None means "the
system". Re-running is safe: existing names are skipped.
"""

from django.db import migrations
from django.utils import timezone

CATEGORIES = [
    "ANALYTICAL",
    "METROLOGY",
    "ENERGY AND RENEWABLE",
    "MECHANICAL",
    "CLINICAL LAB",
    "METEOROLOGY",
    "WATER TREATMENT",
    "AVIATION",
    "CHEMICAL/HYDROCARBON LAB",
    "INDUSTRIAL",
    "TEXTILE",
    "LEATHER",
    "PPE",
    "SPARE PARTS",
    "METALLURGY",
    "TESTING EQUIPMENT",
    "CLEANING SUPPLIES",
]


def seed(apps, schema_editor):
    TenderCategory = apps.get_model("core", "TenderCategory")
    AuditEntry = apps.get_model("core", "AuditEntry")
    now = timezone.now()
    for name in CATEGORIES:
        if TenderCategory.objects.filter(name__iexact=name).exists():
            continue
        category = TenderCategory.objects.create(name=name)
        AuditEntry.objects.create(
            actor=None,
            action="CREATE",
            model="core.TenderCategory",
            object_id=str(category.pk),
            object_repr=name,
            changes={"id": [None, category.pk], "name": [None, name]},
            timestamp=now,
        )


class Migration(migrations.Migration):
    dependencies = [("core", "0003_tendercategory")]

    # Not reversible on purpose: removing categories that tenders may already
    # reference would be destructive. Un-applying 0003 drops the table anyway.
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
