"""
The tender-state gates on item work, tested in both directions:

  R15  no item, costing or RFQ work before the MD registers the tender
  R9a  none while the tender is frozen awaiting an eligibility decision
  R4   none once the tender holds its rate lock
  §5   start_costing needs at least one item (and nothing more)
  R2   submit_for_audit refuses an unpriced or incomplete item sheet
"""

from django.apps import apps
from django.core.exceptions import ValidationError
from django.db import transaction
from django.test import TestCase
from django.utils import timezone

from items.models import AttachmentKind, Item, ItemAttachment, SupplierSuggestion
from tenders.models import TenderStatus as S, TransitionError, WORK_ALLOWED_STATUSES

from .helpers import D, ItemFixtures, force_status

# approvals sits below items (§1), so its models are resolved by name, never imported.
EligibilityReview = apps.get_model("approvals", "EligibilityReview")
Approval = apps.get_model("approvals", "Approval")


def attempt_in_own_transaction(attempt):
    """Django's delete collector runs without a savepoint, so a guard that raises
    during a delete poisons the surrounding (test) transaction. Contain each attempt."""
    with transaction.atomic():
        attempt()


def work_allowed_pairs():
    allowed = [s for s in S.values if s in WORK_ALLOWED_STATUSES]
    refused = [s for s in S.values if s not in WORK_ALLOWED_STATUSES and s != S.PENDING_ELIGIBILITY_DECISION]
    return allowed, refused


class ItemWorkByTenderStateTests(ItemFixtures, TestCase):
    def tender_in(self, status):
        extra = {"previous_status": S.DRAFTING} if status == S.PENDING_ELIGIBILITY_DECISION else {}
        if status in (S.PENDING_AUDIT, S.PENDING_EXECUTIVE_APPROVAL, S.READY_TO_SUBMIT, S.SUBMITTED, S.WON, S.LOST):
            extra["exchange_rate_locked_at"] = timezone.now()
        return self.new_tender(status, **extra)

    def seeded(self):
        """An item, attachment, quote and suggestion on a DRAFTING tender, to try writing to later."""
        tender = self.drafting()
        item = self.make_item(tender)
        attachment = self.attach(item, AttachmentKind.BROCHURE)
        quote = self.quote(item, quoted_unit_price=D("10"))
        suggestion = SupplierSuggestion(
            tender=tender, scope="SEPARATE_PART", suggested_name="Hettich", rationale="Makes it.",
            source_urls=["https://example.com/hettich"], requested_by=self.pm,
        )
        suggestion.save()
        suggestion.items.add(item)
        # Django refuses to delete an item that still has attachments or quotes (PROTECT), so
        # the "delete item" attempts target this bare one, which reaches our own guard.
        self.spare = self.make_item(tender, specification="Spare")
        return tender, item, attachment, quote, suggestion

    def attempts(self, tender, item, attachment, quote, suggestion):
        def new_suggestion():
            SupplierSuggestion(
                tender=tender, scope="SEPARATE_PART", suggested_name="Other", rationale="r",
                source_urls=["https://example.com/o"], requested_by=self.pm,
            ).save()

        def edit_item():
            item.specification += " (edited)"
            item.save()

        return {
            "create item": lambda: Item.objects.create(tender=tender, specification="New", quantity=1),
            "edit item": edit_item,
            "price item": lambda: setattr(item, "fob_unit_price", D("5")) or setattr(item, "currency", "USD") or item.save(),
            "set status": lambda: item.set_status(self.pm, "NEEDS_RESOLUTION"),
            "delete item": lambda: Item.objects.get(pk=self.spare.pk).delete(),
            "add attachment": lambda: self.attach(item, AttachmentKind.DATASHEET),
            "delete attachment": lambda: attachment.delete(),
            "add quote": lambda: self.quote(item, quoted_unit_price=D("9")),
            "select quote": lambda: quote.select(self.pm),
            "reject quote": lambda: quote.reject(self.pm),
            "delete quote": lambda: quote.delete(),
            "new suggestion": new_suggestion,
            "accept suggestion": lambda: suggestion.accept(self.pm),
            "reject suggestion": lambda: suggestion.reject(self.pm),
            "link suggestion items": lambda: suggestion.items.add(item),
        }

    def test_allowed_while_drafting_or_costing(self):
        for status in (S.DRAFTING, S.COSTING):
            with self.subTest(status=status):
                tender = self.tender_in(status)
                item = self.priced_item(tender)  # create + price
                item.specification = "Edited"
                item.save()
                self.attach(item, AttachmentKind.BROCHURE)
                self.quote(item, quoted_unit_price=D("10")).select(self.pm)

    def test_r15_r4_refused_in_every_other_state(self):
        _, refused = work_allowed_pairs()
        for status in refused:
            with self.subTest(status=status):
                tender, item, attachment, quote, suggestion = self.seeded()
                # Move the tender (test-only shortcut) to the state under test, then try everything.
                extra = {"exchange_rate_locked_at": timezone.now()} if status in (
                    S.PENDING_AUDIT, S.PENDING_EXECUTIVE_APPROVAL, S.READY_TO_SUBMIT, S.SUBMITTED, S.WON, S.LOST, S.WITHDRAWN
                ) else {}
                force_status(tender, status, **extra)
                for name, attempt in self.attempts(tender, item, attachment, quote, suggestion).items():
                    with self.subTest(status=status, action=name), self.assertRaises(TransitionError) as ctx:
                        attempt_in_own_transaction(attempt)
                    self.assertEqual(ctx.exception.code, "r15")
                self.assertEqual(Item.objects.filter(tender=tender).count(), 2)

    def test_r15_no_item_before_registration(self):
        for status in (S.EXTRACTING, S.PENDING_VERIFICATION, S.PENDING_REGISTRATION_APPROVAL):
            with self.subTest(status=status), self.assertRaises(TransitionError):
                self.make_item(self.new_tender(status))

    def test_r4_no_edit_once_the_rate_lock_is_held(self):
        item = self.complete_item(self.costing())
        item.tender.submit_for_audit(self.pm)
        item.tender.refresh_from_db()
        self.assertIsNotNone(item.tender.exchange_rate_locked_at)
        item.fob_unit_price, item.currency = D("1"), "USD"
        with self.assertRaises(TransitionError):
            item.save()  # no repricing under a frozen rate

    def test_r9a_refused_while_frozen_with_the_freeze_code(self):
        for origin in (S.DRAFTING, S.COSTING):
            with self.subTest(origin=origin):
                tender, item, attachment, quote, suggestion = self.seeded()
                force_status(tender, origin)  # seeded() is DRAFTING; COSTING exercises the other entry
                review = EligibilityReview.objects.raise_for(tender, self.pm, requirement="Turnover.")
                tender.refresh_from_db()
                self.assertTrue(tender.is_frozen)
                for name, attempt in self.attempts(tender, item, attachment, quote, suggestion).items():
                    with self.subTest(origin=origin, action=name), self.assertRaises(TransitionError) as ctx:
                        attempt_in_own_transaction(attempt)
                    self.assertEqual(ctx.exception.code, "r9a", name)
                review.decide(self.md, "PROCEED")  # the MD resolves...
                item.refresh_from_db()
                item.specification = "Edited after the freeze"
                item.save()  # ...and work resumes

    def test_a_frozen_tenders_existing_figures_are_untouched(self):
        tender = self.drafting()
        item = self.priced_item(tender)
        before = item.total_selling_price_tzs
        EligibilityReview.objects.raise_for(tender, self.pm, requirement="Turnover.")
        item.fob_unit_price = D("500")
        with self.assertRaises(TransitionError):
            item.save()
        item.refresh_from_db()
        self.assertEqual(item.total_selling_price_tzs, before)

    def test_items_cannot_be_deleted_around_the_model(self):
        tender = self.drafting()
        item = self.make_item(tender)  # bare: no attachments or quotes to trip PROTECT first
        EligibilityReview.objects.raise_for(tender, self.pm, requirement="Turnover.")
        with self.assertRaises(TransitionError):
            attempt_in_own_transaction(lambda: Item.objects.filter(pk=item.pk).delete())  # same guard
        self.assertTrue(Item.objects.filter(pk=item.pk).exists())


class StartCostingItemCheckTests(ItemFixtures, TestCase):
    """§5: start_costing requires DRAFTING and at least one Item — nothing more."""

    def test_refused_with_no_items(self):
        tender = self.drafting()
        with self.assertRaises(TransitionError) as ctx:
            tender.start_costing(self.pm)
        self.assertEqual(ctx.exception.code, "no_items")
        tender.refresh_from_db()
        self.assertEqual(tender.status, S.DRAFTING)

    def test_allowed_with_one_item(self):
        tender = self.drafting()
        self.make_item(tender)
        tender.start_costing(self.pm)
        self.assertEqual(tender.status, S.COSTING)

    def test_another_tenders_item_does_not_count(self):
        self.make_item(self.drafting())
        tender = self.drafting()
        with self.assertRaises(TransitionError):
            tender.start_costing(self.pm)

    def test_an_unpriced_bare_item_is_enough(self):
        # "Requires only ... at least one Item": no price, supplier or spec split needed.
        tender = self.drafting()
        self.make_item(tender, specification="x", quantity=1)
        tender.start_costing(self.pm)
        self.assertEqual(tender.status, S.COSTING)

    def test_the_other_conditions_still_apply(self):
        for status in (S.EXTRACTING, S.PENDING_VERIFICATION, S.PENDING_REGISTRATION_APPROVAL, S.COSTING):
            with self.subTest(status=status):
                tender = self.new_tender(status)
                with self.assertRaises(TransitionError):
                    tender.start_costing(self.pm)
        tender = self.drafting()
        self.make_item(tender)
        from django.core.exceptions import PermissionDenied

        with self.assertRaises(PermissionDenied):
            tender.start_costing(self.pm2)  # only this tender's PM

    def test_refusal_writes_no_audit_entry_and_changes_nothing(self):
        from core.models import AuditEntry

        tender = self.drafting()
        before = AuditEntry.objects.count()
        with self.assertRaises(TransitionError):
            tender.start_costing(self.pm)
        self.assertEqual(AuditEntry.objects.count(), before)


class R2AgainstRealItemsTests(ItemFixtures, TestCase):
    """The tenders R2 gate reads Item and ItemAttachment through reverse accessors;
    these prove the field names and related_names of this app satisfy it."""

    def test_a_complete_item_sheet_goes_to_audit_and_stamps_the_rate_lock(self):
        tender = self.costing()
        self.complete_item(tender)
        self.complete_item(tender)
        tender.submit_for_audit(self.pm)
        self.assertEqual(tender.status, S.PENDING_AUDIT)
        self.assertIsNotNone(tender.exchange_rate_locked_at)  # R4

    def test_no_items_is_refused(self):
        tender = self.costing()
        with self.assertRaises(TransitionError) as ctx:
            tender.submit_for_audit(self.pm)
        self.assertIn("R2", str(ctx.exception))

    def test_each_missing_required_field_is_refused(self):
        required = ("unit_cost", "currency", "unit_of_measure", "category", "make_brand",
                    "model_part_number", "country_of_origin", "supplier")
        for field in required:
            with self.subTest(missing=field):
                tender = self.costing()
                self.complete_item(tender, omit=[field])
                with self.assertRaises(TransitionError) as ctx:
                    tender.submit_for_audit(self.pm)
                self.assertIn(field, str(ctx.exception))
                tender.refresh_from_db()
                self.assertEqual(tender.status, S.COSTING)
                self.assertIsNone(tender.exchange_rate_locked_at)

    def test_incoterm_can_never_be_missing(self):
        # It defaults to CIF (§8) and cannot be saved blank, so R2 is satisfied by construction.
        self.assertEqual(self.make_item(self.costing()).incoterm, "CIF")
        with self.assertRaises(ValidationError):
            self.make_item(self.costing(), incoterm="")

    def test_each_missing_attachment_kind_is_refused(self):
        for kind in AttachmentKind.values:
            with self.subTest(missing=kind):
                tender = self.costing()
                self.complete_item(tender, attachments=[k for k in AttachmentKind.values if k != kind])
                with self.assertRaises(TransitionError) as ctx:
                    tender.submit_for_audit(self.pm)
                self.assertIn(kind.lower(), str(ctx.exception))

    def test_one_incomplete_item_blocks_the_whole_sheet(self):
        tender = self.costing()
        self.complete_item(tender)
        self.complete_item(tender, omit=["make_brand"])
        with self.assertRaises(TransitionError) as ctx:
            tender.submit_for_audit(self.pm)
        self.assertIn("1 item(s) missing make_brand", str(ctx.exception))

    def test_a_decline_returning_to_costing_lets_the_sheet_be_fixed_and_resubmitted(self):
        tender = self.costing()
        item = self.complete_item(tender)
        tender.submit_for_audit(self.pm)
        force_status(tender, S.PENDING_EXECUTIVE_APPROVAL, exchange_rate_locked_at=tender.exchange_rate_locked_at)
        Approval.objects.sign(
            tender, "SUBMISSION", "DECLINED", self.md,
            typed_name=self.md.get_full_name(), password="x", comments="Re-price lot 1.",
        )
        tender.refresh_from_db()
        self.assertEqual((tender.status, tender.exchange_rate_locked_at), (S.COSTING, None))
        item.refresh_from_db()
        item.unit_cost = D("1100.00")  # R9: back with the PM, who can edit again
        item.save()
        tender.submit_for_audit(self.pm)
        self.assertIsNotNone(tender.exchange_rate_locked_at)  # stamped afresh (R4)
