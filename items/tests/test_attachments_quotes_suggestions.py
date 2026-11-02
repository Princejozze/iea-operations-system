"""ItemAttachment, Quote and SupplierSuggestion (§5 step 11, §7)."""

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.test import SimpleTestCase, TestCase

from core.models import AuditAction, AuditBypassError, AuditEntry
from items.models import (
    AttachmentKind, Item, ItemAttachment, SuggestionStatus as SS, SupplierSuggestion,
)
from suppliers.models import Supplier
from tenders.models import GuardedFieldError, TransitionError

from .helpers import D, ItemFixtures


class AttachmentTests(ItemFixtures, TestCase):
    def test_kinds_are_the_three_in_the_spec(self):
        self.assertEqual(set(AttachmentKind.values), {"BROCHURE", "QUOTATION", "DATASHEET"})

    def test_every_kind_attaches_and_is_reachable_as_the_tender_gate_expects(self):
        item = self.make_item()
        for kind in AttachmentKind.values:
            self.attach(item, kind)
        self.assertEqual(set(item.attachments.values_list("kind", flat=True)), set(AttachmentKind.values))

    def test_an_unknown_kind_is_refused(self):
        item = self.make_item()
        with self.assertRaises(ValidationError):
            ItemAttachment.objects.create(
                item=item, kind="PHOTO", uploaded_by=self.pm, file=ContentFile(b"x", name="x.jpg")
            )
        with self.assertRaises(IntegrityError), transaction.atomic():
            ItemAttachment.objects.bulk_create(
                [ItemAttachment(item=item, kind="PHOTO", uploaded_by=self.pm, file="x.jpg")]
            )

    def test_creating_and_deleting_are_both_audited(self):
        item = self.make_item()
        attachment = self.attach(item, AttachmentKind.QUOTATION)
        pk = str(attachment.pk)
        created = AuditEntry.objects.get(model="items.ItemAttachment", object_id=pk, action=AuditAction.CREATE)
        self.assertEqual(created.changes["kind"], [None, "QUOTATION"])
        attachment.delete(actor=self.pm)
        deleted = AuditEntry.objects.get(model="items.ItemAttachment", object_id=pk, action=AuditAction.DELETE)
        self.assertEqual(deleted.actor, self.pm)  # R7: attachment delete writes an AuditEntry
        self.assertEqual(deleted.changes["kind"], ["QUOTATION", None])

    def test_an_item_cannot_be_deleted_while_it_has_attachments_or_quotes(self):
        item = self.make_item()
        self.attach(item, AttachmentKind.BROCHURE)
        with self.assertRaises(Exception) as ctx, transaction.atomic():
            item.delete()
        self.assertEqual(type(ctx.exception).__name__, "ProtectedError")


def suggestion_fields(case, tender, **over):
    fields = dict(
        tender=tender, scope="COMPLETE_UNIT", suggested_name="Hettich Zentrifugen", country="Germany",
        website="https://www.hettich.com", rationale="Makes 24-place refrigerated benchtop centrifuges.",
        source_urls=["https://www.hettich.com/en/products"], confidence=D("0.8"), requested_by=case.pm,
    )
    fields.update(over)
    return fields


class SuggestionCreationTests(ItemFixtures, TestCase):
    def make(self, tender=None, **over):
        tender = tender or self.drafting()
        suggestion = SupplierSuggestion(**suggestion_fields(self, tender, **over))
        suggestion.save()
        return suggestion

    def test_a_suggestion_is_only_a_suggestion(self):
        before = Supplier.objects.count()
        suggestion = self.make()
        self.assertEqual(suggestion.status, SS.SUGGESTED)
        self.assertIsNone(suggestion.linked_supplier)
        self.assertEqual(Supplier.objects.count(), before)  # never a Supplier row until accepted

    def test_rationale_and_sources_are_mandatory(self):
        for over in (
            {"rationale": ""}, {"rationale": "  "}, {"source_urls": []}, {"source_urls": "https://x.com"},
            {"source_urls": ["not a url"]}, {"source_urls": ["https://ok.com", 5]}, {"suggested_name": " "},
            {"scope": "BUNDLE"}, {"confidence": D("1.5")}, {"confidence": D("-0.1")},
        ):
            with self.subTest(**{k: str(v) for k, v in over.items()}), self.assertRaises(ValidationError):
                self.make(**over)
        self.assertFalse(SupplierSuggestion.objects.exists())

    def test_only_a_pm_asks_for_suggestions(self):
        for user in (self.md, self.hops, self.auditor, self.finance, self.admin_user):
            with self.subTest(role=user.role), self.assertRaises(ValidationError):
                self.make(requested_by=user)

    def test_it_can_cover_a_whole_unit_of_items_on_its_own_tender(self):
        tender = self.drafting()
        a, b = self.make_item(tender), self.make_item(tender)
        suggestion = self.make(tender)
        suggestion.items.add(a, b)
        self.assertEqual(set(suggestion.items.all()), {a, b})
        self.assertEqual(list(a.supplier_suggestions.all()), [suggestion])

    def test_it_cannot_cover_another_tenders_items(self):
        tender = self.drafting()
        stranger = self.make_item(self.drafting())
        suggestion = self.make(tender)
        # m2m add() runs without a savepoint, so contain each refused call in its own transaction.
        with self.assertRaises(ValidationError), transaction.atomic():
            suggestion.items.add(stranger)
        with self.assertRaises(ValidationError), transaction.atomic():  # the reverse accessor is no way round it
            stranger.supplier_suggestions.add(suggestion)
        self.assertFalse(suggestion.items.exists())

    def test_status_and_supplier_are_written_only_by_accept_and_reject(self):
        suggestion = self.make()
        suggestion.status = SS.ACCEPTED
        with self.assertRaises(GuardedFieldError):
            suggestion.save()
        with self.assertRaises(GuardedFieldError):
            self.make(status=SS.ACCEPTED)
        with self.assertRaises(GuardedFieldError):
            self.make(linked_supplier=self.supplier)
        with self.assertRaises(AuditBypassError):
            SupplierSuggestion.objects.filter(pk=suggestion.pk).update(status=SS.ACCEPTED)

    def test_the_database_refuses_accepted_without_a_supplier_and_a_supplier_without_accepted(self):
        suggestion = self.make()
        for status, supplier in ((SS.ACCEPTED, None), (SS.REJECTED, self.supplier), (SS.SUGGESTED, self.supplier)):
            with self.subTest(status=status, supplier=bool(supplier)), self.assertRaises(IntegrityError), transaction.atomic():
                suggestion.status, suggestion.linked_supplier = status, supplier
                with suggestion._system_write():  # bypass the model guard to prove the constraint stands alone
                    suggestion.save()


class SuggestionDecisionTests(ItemFixtures, TestCase):
    def make(self, **over):
        self.tender = self.drafting()
        suggestion = SupplierSuggestion(**suggestion_fields(self, self.tender, **over))
        suggestion.save()
        return suggestion

    def test_accepting_creates_a_supplier_and_links_it(self):
        suggestion = self.make()
        supplier = suggestion.accept(self.pm)
        suggestion.refresh_from_db()
        self.assertEqual((suggestion.status, suggestion.linked_supplier), (SS.ACCEPTED, supplier))
        self.assertEqual((supplier.name, supplier.country), ("Hettich Zentrifugen", "Germany"))
        self.assertIn(suggestion.rationale, supplier.notes)  # provenance travels with the supplier
        self.assertIn(self.tender.reference, supplier.notes)

    def test_accepting_links_an_existing_supplier_of_the_same_name_instead_of_duplicating(self):
        existing = Supplier.objects.create(name="HETTICH  Zentrifugen")
        before = Supplier.objects.count()
        suggestion = self.make()
        self.assertEqual(suggestion.accept(self.pm), existing)
        self.assertEqual(Supplier.objects.count(), before)

    def test_the_pm_can_name_the_supplier_to_link(self):
        suggestion = self.make()
        before = Supplier.objects.count()
        self.assertEqual(suggestion.accept(self.pm, supplier=self.supplier), self.supplier)
        self.assertEqual(Supplier.objects.count(), before)

    def test_rejecting_creates_no_supplier(self):
        suggestion = self.make()
        before = Supplier.objects.count()
        suggestion.reject(self.pm)
        self.assertEqual((suggestion.status, suggestion.linked_supplier), (SS.REJECTED, None))
        self.assertEqual(Supplier.objects.count(), before)

    def test_only_this_tenders_pm_decides(self):
        suggestion = self.make()
        before = Supplier.objects.count()
        for user in (self.pm2, self.md, self.hops, self.auditor, self.finance, self.admin_user):
            for method in (suggestion.accept, suggestion.reject):
                with self.subTest(role=user.role, who=user.first_name, method=method.__name__), self.assertRaises(PermissionDenied):
                    method(user)
        suggestion.refresh_from_db()
        self.assertEqual(suggestion.status, SS.SUGGESTED)
        self.assertEqual(Supplier.objects.count(), before)

    def test_a_decision_is_final(self):
        for first, second in (("accept", "reject"), ("accept", "accept"), ("reject", "accept"), ("reject", "reject")):
            with self.subTest(first=first, second=second):
                suggestion = self.make()
                getattr(suggestion, first)(self.pm)
                with self.assertRaises(TransitionError):
                    getattr(suggestion, second)(self.pm)

    def test_decisions_are_audited_to_the_pm(self):
        suggestion = self.make()
        suggestion.accept(self.pm)
        entry = AuditEntry.objects.filter(model="items.SupplierSuggestion", object_id=str(suggestion.pk)).first()
        self.assertEqual((entry.action, entry.actor), (AuditAction.STATUS_CHANGE, self.pm))
        self.assertEqual(entry.changes["status"], [SS.SUGGESTED, SS.ACCEPTED])


class VocabularyTests(SimpleTestCase):
    def test_suggestion_vocabulary_is_section_7(self):
        self.assertEqual(set(SS.values), {"SUGGESTED", "ACCEPTED", "REJECTED"})
