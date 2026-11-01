"""
CLAUDE.md §8:
  * Tender.category   free text  -> FK to core.TenderCategory (a lookup table)
  * bid_security_required (boolean) + free-text bid_security_type
        -> one BidSecurityType enum field; NOT_APPLICABLE replaces "required = false"

There is no production data yet, so on a real database this converts zero rows.
It still converts any rows it finds rather than dropping them, and writes an
AuditEntry for every tender it changes (historical models skip Auditable.save(),
so R7 has to be honoured here by hand).

Data rules:
  category text   -> matching TenderCategory (case-insensitive); an unknown value
                     becomes a new category, so nothing is lost.
  required = False                       -> NOT_APPLICABLE
  text mentioning guarantee / insurance|bond / declaration / letter|proof|funds
                                         -> BANK_GUARANTEE / INSURANCE_BOND /
                                            TENDER_SECURING_DECLARATION /
                                            BANK_LETTER_PROOF_OF_FUNDS
  anything else                          -> "" (unverified; the PM re-confirms)
The data step is forward-only.
"""

import django.db.models.deletion
from django.db import migrations, models
from django.utils import timezone

BID_SECURITY_VALUES = [
    "NOT_APPLICABLE",
    "TENDER_SECURING_DECLARATION",
    "BANK_LETTER_PROOF_OF_FUNDS",
    "BANK_GUARANTEE",
    "INSURANCE_BOND",
]


def map_bid_security(required, text):
    text = (text or "").strip().lower()
    if required is False:
        return "NOT_APPLICABLE"
    if "declaration" in text:
        return "TENDER_SECURING_DECLARATION"
    if "insurance" in text or "bond" in text:
        return "INSURANCE_BOND"
    if "guarantee" in text:
        return "BANK_GUARANTEE"
    if "letter" in text or "proof" in text or "funds" in text:
        return "BANK_LETTER_PROOF_OF_FUNDS"
    return ""


def convert(apps, schema_editor):
    Tender = apps.get_model("tenders", "Tender")
    TenderCategory = apps.get_model("core", "TenderCategory")
    AuditEntry = apps.get_model("core", "AuditEntry")
    now = timezone.now()

    def audit(model, obj, action, changes, repr_):
        AuditEntry.objects.create(
            actor=None, action=action, model=model, object_id=str(obj.pk),
            object_repr=repr_[:255], changes=changes, timestamp=now,
        )

    for tender in Tender.objects.all():
        changes = {}

        name = " ".join((tender.category or "").split()).upper()
        if name:
            category = TenderCategory.objects.filter(name__iexact=name).first()
            if category is None:
                category = TenderCategory.objects.create(name=name)
                audit("core.TenderCategory", category, "CREATE",
                      {"id": [None, category.pk], "name": [None, name]}, name)
            tender.category_new_id = category.pk
            changes["category_id"] = [tender.category, category.pk]

        new_type = map_bid_security(tender.bid_security_required, tender.bid_security_type)
        if new_type != tender.bid_security_type:
            changes["bid_security_type"] = [tender.bid_security_type, new_type]
        tender.bid_security_type = new_type

        if changes:
            tender.save(update_fields=["category_new", "bid_security_type"])
            audit("tenders.Tender", tender, "UPDATE", changes, tender.reference)


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0004_seed_tender_categories"),
        ("tenders", "0001_initial"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="tender", name="tenders_bid_security_type_when_required"
        ),
        migrations.AddField(
            model_name="tender",
            name="category_new",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="tenders", to="core.tendercategory",
            ),
        ),
        migrations.RunPython(convert, migrations.RunPython.noop),
        migrations.RemoveField(model_name="tender", name="category"),
        migrations.RenameField(model_name="tender", old_name="category_new", new_name="category"),
        migrations.RemoveField(model_name="tender", name="bid_security_required"),
        migrations.AlterField(
            model_name="tender",
            name="bid_security_type",
            field=models.CharField(
                blank=True, max_length=32,
                choices=[
                    ("NOT_APPLICABLE", "Not applicable"),
                    ("TENDER_SECURING_DECLARATION", "Tender securing declaration"),
                    ("BANK_LETTER_PROOF_OF_FUNDS", "Bank letter / proof of funds"),
                    ("BANK_GUARANTEE", "Bank guarantee"),
                    ("INSURANCE_BOND", "Insurance bond"),
                ],
            ),
        ),
        migrations.AddConstraint(
            model_name="tender",
            constraint=models.CheckConstraint(
                condition=models.Q(("bid_security_type__in", ["", *BID_SECURITY_VALUES])),
                name="tenders_bid_security_type_valid",
            ),
        ),
    ]
