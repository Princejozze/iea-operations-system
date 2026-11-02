"""
Quote entry and selection, §5 step 13 and §7.

  - a quote is the supplier's original document AND the typed figures; neither alone
  - the document is the item's QUOTATION attachment, so it counts toward R2
  - selecting copies the figures onto the item, sets selected_quote, re-costs, audits
  - exactly one quote per item is SELECTED (database constraint)
  - while a quote is selected, the item's copied figures cannot drift from it
  - specification_offered is exposed on the item for the QC audit
"""

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.db import IntegrityError, connection, transaction
from django.db.models.deletion import ProtectedError
from django.test import TestCase

from core.models import AuditAction, AuditBypassError, AuditEntry
from items.models import AttachmentKind, Item, ItemAttachment, Quote, QuoteStatus as QS
from items.services.quotes import enter_quote
from suppliers.models import Supplier
from tenders.models import GuardedFieldError, TenderStatus as S, TransitionError

from .helpers import D, ItemFixtures


class QuoteEntryTests(ItemFixtures, TestCase):
    def test_the_document_and_the_figures_are_stored_together(self):
        item = self.make_item(self.costing())
        quote = self.quote(
            item, incoterm="FOB", shipping_applies=True, country_of_origin="Germany", make_brand="Eppendorf",
            model_part_number="5424 R", specification_offered="24 x 1.5 ml, 21,130 x g", lead_time_days=45,
        )
        self.assertEqual(quote.status, QS.RECEIVED)
        self.assertEqual(quote.entered_by, self.pm)
        self.assertEqual(quote.quoted_unit_price, D("1500.00"))
        evidence = quote.original_document
        self.assertEqual((evidence.item, evidence.kind, evidence.uploaded_by), (item, AttachmentKind.QUOTATION, self.pm))
        self.assertEqual(evidence.file.read(), b"%PDF-1.4 supplier quotation")
        created = AuditEntry.objects.get(model="items.Quote", object_id=str(quote.pk), action=AuditAction.CREATE)
        self.assertEqual(created.actor, self.pm)

    def test_no_quote_without_its_document(self):
        item = self.make_item()
        for document in (None, ContentFile(b"", name="empty.pdf"), ContentFile(b"%PDF", name="")):
            with self.subTest(document=document):
                with self.assertRaises(ValidationError) as ctx:
                    enter_quote(
                        item, supplier=self.supplier, user=self.pm, quoted_unit_price=D("10"), currency="USD",
                        original_document=document,
                    )
                self.assertIn("original_document", ctx.exception.message_dict)
        self.assertFalse(Quote.objects.exists())
        self.assertFalse(ItemAttachment.objects.exists())

    def test_no_quote_without_its_figures_and_nothing_is_stored(self):
        item = self.make_item()
        with self.assertRaises(ValidationError) as ctx:
            self.quote(item, quoted_unit_price=None, currency="")
        self.assertEqual(set(ctx.exception.message_dict), {"quoted_unit_price", "currency"})
        self.assertFalse(ItemAttachment.objects.exists())  # the file was never written

    def test_bad_figures_are_refused(self):
        item = self.make_item()
        for fields in ({"quoted_unit_price": D("-1")}, {"currency": "XYZ"}, {"incoterm": "DDP"}):
            with self.subTest(**fields), self.assertRaises(ValidationError):
                self.quote(item, **fields)

    def test_incoterm_defaults_to_cif(self):
        self.assertEqual(self.quote(self.make_item()).incoterm, "CIF")

    def test_only_this_tenders_pm_enters_quotes(self):
        item = self.make_item()
        for user in (self.pm2, self.auditor, self.md, self.finance):
            with self.subTest(user=user.email), self.assertRaises(ValidationError) as ctx:
                self.quote(item, user=user)
            self.assertIn("entered_by", ctx.exception.message_dict)

    def test_the_document_must_be_a_quotation_of_the_same_item(self):
        item, other = self.make_item(), self.make_item()
        for attachment in (self.attach(item, AttachmentKind.BROCHURE), self.attach(other, AttachmentKind.QUOTATION)):
            with self.subTest(kind=attachment.kind), self.assertRaises(ValidationError) as ctx:
                Quote.objects.create(
                    item=item, supplier=self.supplier, original_document=attachment, quoted_unit_price=D("1"),
                    currency="USD", entered_by=self.pm,
                )
            self.assertIn("original_document", ctx.exception.message_dict)

    def test_bulk_create_is_refused(self):
        with self.assertRaises(AuditBypassError):
            Quote.objects.bulk_create([])

    def test_the_document_cannot_be_deleted_from_under_its_quote(self):
        quote = self.quote(self.make_item())
        with self.assertRaises(ProtectedError), transaction.atomic():
            quote.original_document.delete()

    def test_a_received_quote_can_be_corrected_and_the_correction_is_audited(self):
        quote = self.quote(self.make_item())
        quote.quoted_unit_price = D("1450.00")
        quote.save(actor=self.pm)
        entry = AuditEntry.objects.filter(model="items.Quote", object_id=str(quote.pk), action=AuditAction.UPDATE).get()
        self.assertEqual(entry.changes["quoted_unit_price"], ["1500.0000", "1450.0000"])

    def test_status_is_never_set_by_assignment(self):
        quote = self.quote(self.make_item())
        quote.status = QS.SELECTED
        with self.assertRaises(GuardedFieldError):
            quote.save()


class QuoteSelectionTests(ItemFixtures, TestCase):
    def test_selecting_copies_the_figures_onto_the_item_and_costs_it(self):
        item = self.make_item(self.costing(), quantity=10, risk_pct=D("5"), labour_hours_per_unit=D("2"))
        self.assertFalse(item.is_costed)
        quote = self.quote(
            item, quoted_unit_price=D("100"), incoterm="FOB", shipping_applies=True, country_of_origin="Germany",
            make_brand="Eppendorf", model_part_number="5424 R",
        )
        item = quote.select(self.pm)
        item.refresh_from_db()
        quote.refresh_from_db()
        self.assertEqual(quote.status, QS.SELECTED)
        self.assertEqual(item.selected_quote, quote)
        self.assertEqual(
            (item.supplier, item.unit_cost, item.fob_unit_price, item.currency, item.incoterm, item.shipping_applies,
             item.country_of_origin, item.make_brand, item.model_part_number),
            (self.supplier, D("100"), D("100"), "USD", "FOB", True, "Germany", "Eppendorf", "5424 R"),
        )
        # The hand-worked figures from test_costing: 100 USD x 2500 x 10.
        self.assertEqual(item.import_material, D("2500000"))
        self.assertEqual(item.shipping, D("250000"))
        self.assertEqual(item.total_cost, D("1000"))
        self.assertTrue(item.is_costed)
        # The PM's own chain inputs are untouched.
        self.assertEqual((item.risk_pct, item.labour_hours_per_unit), (D("5"), D("2")))

    def test_a_local_quote_adds_no_shipping_duty_or_cnf(self):
        item = self.make_item(quantity=2)
        item = self.quote(item, quoted_unit_price=D("50000"), currency="TZS", shipping_applies=False).select(self.pm)
        item.refresh_from_db()
        self.assertEqual((item.shipping, item.duty, item.cnf), (0, 0, 0))
        self.assertEqual(item.import_material, D("100000"))

    def test_changing_the_selection_recosts_and_writes_an_audit_entry(self):
        item = self.make_item(quantity=10)
        first = self.quote(item, quoted_unit_price=D("100"))
        cheaper = self.quote(item, quoted_unit_price=D("80"), supplier=Supplier.objects.create(name="Hettich"))
        first.select(self.pm)
        item.refresh_from_db()
        before_total, snapshot = item.total_selling_price_tzs, item.rates_snapshot_at

        cheaper.select(self.pm)
        item.refresh_from_db()
        first.refresh_from_db()
        self.assertEqual((first.status, cheaper.status), (QS.RECEIVED, QS.SELECTED))
        self.assertEqual(item.selected_quote, cheaper)
        self.assertLess(item.total_selling_price_tzs, before_total)
        self.assertEqual(item.rates_snapshot_at, snapshot)  # company rates are not silently re-copied (§4)

        entry = AuditEntry.objects.filter(model="items.Item", object_id=str(item.pk)).order_by("-id").first()
        self.assertEqual(entry.actor, self.pm)
        self.assertEqual(entry.changes["selected_quote_id"], [first.pk, cheaper.pk])
        self.assertIn("total_selling_price_tzs", entry.changes)
        self.assertIn("unit_cost", entry.changes)

    def test_selecting_the_selected_quote_again_changes_nothing(self):
        item = self.make_item()
        quote = self.quote(item)
        quote.select(self.pm)
        count = AuditEntry.objects.count()
        quote.select(self.pm)
        self.assertEqual(AuditEntry.objects.count(), count)

    def test_the_database_allows_only_one_selected_quote_per_item(self):
        item = self.make_item()
        a, b = self.quote(item), self.quote(item)
        a.select(self.pm)
        with self.assertRaises(IntegrityError), transaction.atomic(), connection.cursor() as cur:
            cur.execute("UPDATE items_quote SET status = 'SELECTED' WHERE id = %s", [b.pk])

    def test_a_rejected_quote_cannot_be_selected_and_the_selected_one_cannot_be_rejected(self):
        item = self.make_item()
        chosen, other = self.quote(item), self.quote(item)
        chosen.select(self.pm)
        with self.assertRaises(TransitionError):
            chosen.reject(self.pm)
        other.reject(self.pm)
        self.assertEqual(other.status, QS.REJECTED)
        with self.assertRaises(TransitionError):
            other.select(self.pm)
        with self.assertRaises(TransitionError):
            other.reject(self.pm)

    def test_only_this_tenders_pm_selects_or_rejects(self):
        quote = self.quote(self.make_item())
        for user in (self.pm2, self.auditor, self.md):
            with self.subTest(user=user.email):
                with self.assertRaises(PermissionDenied):
                    quote.select(user)
                with self.assertRaises(PermissionDenied):
                    quote.reject(user)

    def test_selected_and_rejected_quotes_are_a_record(self):
        item = self.make_item()
        selected, rejected = self.quote(item), self.quote(item)
        selected.select(self.pm)
        rejected.reject(self.pm)
        for quote in (selected, rejected):
            quote.refresh_from_db()
            quote.quoted_unit_price = D("1")
            with self.subTest(status=quote.status), self.assertRaises(ValidationError):
                quote.save()
        selected.refresh_from_db()
        selected.notes = "Confirmed by phone"  # notes are not figures
        selected.save()

    def test_the_selected_quote_cannot_be_deleted(self):
        item = self.make_item()
        quote = self.quote(item)
        quote.select(self.pm)
        with self.assertRaises((ValidationError, ProtectedError)), transaction.atomic():
            quote.delete()

    def test_selected_quote_is_never_set_by_assignment(self):
        item = self.make_item()
        quote = self.quote(item)
        item.selected_quote = quote
        with self.assertRaises(GuardedFieldError):
            item.save()

    def test_while_a_quote_is_selected_the_item_figures_cannot_drift_from_it(self):
        item = self.make_item()
        self.quote(item, quoted_unit_price=D("100")).select(self.pm)
        for field, value in (("unit_cost", D("90")), ("fob_unit_price", D("90")), ("currency", "EUR"),
                             ("make_brand", "Other"), ("shipping_applies", False)):
            item = Item.objects.get(pk=item.pk)
            setattr(item, field, value)
            with self.subTest(field=field), self.assertRaises(ValidationError) as ctx:
                item.save()
            self.assertIn(field, ctx.exception.message_dict)
        # Inputs the quote does not supply stay the PM's to edit, and re-cost the item.
        item = Item.objects.get(pk=item.pk)
        item.quantity, item.risk_pct = 3, D("10")
        item.save()
        self.assertEqual(item.import_material, D("750000"))

    def test_without_a_selected_quote_the_item_can_still_be_priced_by_hand(self):
        item = self.priced_item()  # legacy and manual entry keep working
        self.assertTrue(item.is_costed)

    def test_specification_offered_comes_from_the_selected_quote(self):
        item = self.make_item()
        quote = self.quote(item, specification_offered="Capacity 620 kg")
        self.assertEqual(item.specification_offered, "")
        quote.select(self.pm)
        item.refresh_from_db()
        self.assertEqual(item.specification_offered, "Capacity 620 kg")

    def test_the_quote_document_satisfies_r2_quotation_attachment(self):
        tender = self.costing()
        item = self.complete_item(tender, attachments=(AttachmentKind.BROCHURE, AttachmentKind.DATASHEET))
        with self.assertRaises(TransitionError):
            tender.submit_for_audit(self.pm)  # no quotation yet
        self.quote(
            item, quoted_unit_price=D("1200"), country_of_origin="Germany", make_brand="Eppendorf",
            model_part_number="5424 R",
        ).select(self.pm)
        tender.submit_for_audit(self.pm)
        tender.refresh_from_db()
        self.assertEqual(tender.status, S.PENDING_AUDIT)
