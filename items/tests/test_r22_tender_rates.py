"""
R22: per-tender costing rates. CostingDefaults is the company baseline; each tender holds
its own copy, which the MD alone tunes (preview without saving, apply to re-cost), and
which locks when the tender leaves COSTING.

Lives in items/tests because re-costing needs real items; items may import tenders (§1).

Expected figures are worked BY HAND. The item is the standard worked example
(test_costing.WorkedExampleTests): 100 USD FOB x 2,500 x 10, imported, 5% risk,
2 labour hours a unit.

  At the baseline (margin 20%):  total_selling 5,398,399.5      unit_rate 539,839

  At margin 15%, everything else unchanged:
    total_material   = 3,575,000      (no margin in it)
    risk 5%          =   178,750  ->  3,753,750
    overhead 10%     =   375,375  ->  4,129,125
    margin 15%       =   619,368.75 -> material_selling 4,748,493.75
    labour           = 300,000 x (1 + 0.10 + 0.15)  = 375,000
    maintenance 1%   = (4,748,493.75 + 375,000) x 1% = 51,234.9375
    total_selling    = 5,174,728.6875                unit_rate floor(517,472.86875) = 517,472
"""

from decimal import Decimal

from django.apps import apps
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase

from core.models import AuditAction, AuditBypassError, AuditEntry, CostingDefaults
from items.models import Item
from tenders.models import (
    RATES_EDITABLE_STATUSES,
    GuardedFieldError,
    TenderCostingRates,
    TenderStatus as S,
    TransitionError,
)

from .helpers import ItemFixtures, force_status

D = Decimal

BASELINE_TOTAL = D("5398399.5")
MARGIN_15_TOTAL = D("5174728.6875")
MARGIN_15_UNIT_RATE = 517472

BASELINE_VALUES = {
    "shipping_pct": D("10"), "duty_pct": D("25"), "cnf_pct": D("5"),
    "labour_rate_tzs_per_hour": D("15000"), "overhead_pct": D("10"), "margin_pct": D("20"),
    "maintenance_pct": D("1"), "cost_of_capital_pct": D("22.25"),
}


def rates_entries(row):
    return AuditEntry.objects.filter(model="tenders.TenderCostingRates", object_id=str(row.pk))


class CreatedWithTheTenderTests(ItemFixtures, TestCase):
    def test_a_tender_created_by_upload_gets_its_rates_copied_from_the_company_defaults(self):
        tender = self.new_tender()
        row = TenderCostingRates.objects.get(tender=tender)
        self.assertEqual(row.values(), BASELINE_VALUES)
        self.assertEqual(row.rates(), CostingDefaults.current())  # same numbers as fractions
        created = rates_entries(row).get()
        self.assertEqual((created.action, created.actor), (AuditAction.CREATE, self.pm))

    def test_editing_the_defaults_later_changes_new_tenders_only(self):
        before = self.new_tender()
        defaults = CostingDefaults.objects.get()
        defaults.margin_pct = D("25")
        defaults.save()
        after = self.new_tender()
        self.assertEqual(before.costing_rates.margin_pct, D("20"))
        self.assertEqual(after.costing_rates.margin_pct, D("25"))

    def test_vat_is_not_a_per_bid_rate_and_still_comes_from_the_defaults(self):
        self.assertNotIn("vat_pct", TenderCostingRates.RATE_FIELDS)
        self.assertEqual(self.new_tender().costing_rates.rates().vat_rate, D("0.18"))


class TheChainUsesTheTendersRatesTests(ItemFixtures, TestCase):
    def test_an_item_is_costed_at_its_tenders_rates_not_the_defaults(self):
        tender = self.costing()
        tender.costing_rates.apply(self.md, margin_pct=D("15"))
        defaults = CostingDefaults.objects.get()
        defaults.margin_pct = D("40")  # the company baseline moves; this bid must not
        defaults.save()
        item = self.priced_item(tender)
        self.assertEqual(item.margin_rate, D("0.15"))
        self.assertEqual(item.total_selling_price_tzs, MARGIN_15_TOTAL)
        self.assertEqual(item.unit_selling_price_tzs, MARGIN_15_UNIT_RATE)


class OnlyTheMdMayEditTests(ItemFixtures, TestCase):
    def test_every_other_role_is_refused_and_nothing_changes(self):
        tender = self.costing()
        item = self.priced_item(tender)
        for user in (self.pm, self.hops, self.auditor, self.finance, self.admin_user):
            with self.subTest(role=user.role), self.assertRaises(PermissionDenied):
                tender.costing_rates.apply(user, margin_pct=D("15"))
        self.assertEqual(TenderCostingRates.objects.get(tender=tender).margin_pct, D("20"))
        self.assertEqual(Item.objects.get(pk=item.pk).total_selling_price_tzs, BASELINE_TOTAL)

    def test_an_inactive_md_is_refused(self):
        tender = self.costing()
        self.md.is_active = False
        with self.assertRaises(PermissionDenied):
            tender.costing_rates.apply(self.md, margin_pct=D("15"))

    def test_no_path_around_apply(self):
        row = self.costing().costing_rates
        row.margin_pct = D("15")
        with self.assertRaises(GuardedFieldError):
            row.save()  # assignment and save, as admin or a careless view would
        with self.assertRaises(AuditBypassError):
            TenderCostingRates.objects.filter(pk=row.pk).update(margin_pct=D("15"))
        with self.assertRaises(ValidationError):
            row.delete()
        with self.assertRaises(ValidationError):
            TenderCostingRates.objects.filter(pk=row.pk).delete()
        self.assertEqual(TenderCostingRates.objects.get(pk=row.pk).margin_pct, D("20"))

    def test_creation_is_also_only_the_models_own(self):
        tender = self.costing()
        with self.assertRaises(GuardedFieldError):
            TenderCostingRates(tender=tender, **BASELINE_VALUES).save()


class PreviewTests(ItemFixtures, TestCase):
    def test_preview_returns_the_hand_worked_total_and_saves_nothing(self):
        tender = self.costing()
        item = self.priced_item(tender)
        entries_before = AuditEntry.objects.count()

        preview = tender.costing_rates.preview(margin_pct=D("15"))

        self.assertEqual(preview.total_selling, MARGIN_15_TOTAL)
        self.assertEqual((preview.priced_items, preview.unpriced_items), (1, 0))
        self.assertEqual(TenderCostingRates.objects.get(tender=tender).margin_pct, D("20"))
        stored = Item.objects.get(pk=item.pk)
        self.assertEqual(stored.total_selling_price_tzs, BASELINE_TOTAL)
        self.assertEqual(stored.margin_rate, D("0.2"))
        self.assertEqual(AuditEntry.objects.count(), entries_before)  # nothing written at all

    def test_preview_at_the_current_rates_is_the_current_bid_total(self):
        tender = self.costing()
        self.priced_item(tender)
        self.priced_item(tender)
        self.assertEqual(tender.costing_rates.preview().total_selling, BASELINE_TOTAL * 2)

    def test_unpriced_items_are_counted_not_totalled(self):
        tender = self.costing()
        self.priced_item(tender)
        self.make_item(tender)  # no price yet
        preview = tender.costing_rates.preview(margin_pct=D("15"))
        self.assertEqual((preview.total_selling, preview.priced_items, preview.unpriced_items),
                         (MARGIN_15_TOTAL, 1, 1))

    def test_preview_is_what_apply_then_produces(self):
        tender = self.costing()
        self.priced_item(tender)
        self.priced_item(tender, quantity=3, incoterm="CIF")
        candidate = {"margin_pct": D("12.5"), "overhead_pct": D("8"), "labour_rate_tzs_per_hour": D("18000")}
        previewed = tender.costing_rates.preview(**candidate).total_selling
        applied = tender.costing_rates.apply(self.md, **candidate)
        self.assertEqual(applied.new_total, previewed)

    def test_a_candidate_is_validated_like_a_real_edit(self):
        row = self.costing().costing_rates
        for bad in ({"margin_pct": D("101")}, {"duty_pct": D("-1")}, {"margin_pct": D("12.345")},
                    {"labour_rate_tzs_per_hour": D("-5")}, {"vat_pct": D("0")}, {"margin_pct": "lots"}):
            with self.subTest(candidate=bad), self.assertRaises(ValidationError):
                row.preview(**bad)


class ApplyTests(ItemFixtures, TestCase):
    def test_apply_recosts_every_item_and_returns_old_and_new(self):
        tender = self.costing()
        items = [self.priced_item(tender), self.priced_item(tender)]
        unpriced = self.make_item(tender)

        result = tender.costing_rates.apply(self.md, margin_pct=D("15"))

        self.assertEqual((result.old_total, result.new_total), (BASELINE_TOTAL * 2, MARGIN_15_TOTAL * 2))
        self.assertEqual((result.old_rates["margin_pct"], result.new_rates["margin_pct"]), (D("20"), D("15")))
        self.assertEqual(result.items_recosted, 3)
        for item in items:
            stored = Item.objects.get(pk=item.pk)
            self.assertEqual(stored.margin_rate, D("0.15"))
            self.assertEqual(stored.total_selling_price_tzs, MARGIN_15_TOTAL)
            self.assertEqual(stored.unit_selling_price_tzs, MARGIN_15_UNIT_RATE)
        self.assertFalse(Item.objects.get(pk=unpriced.pk).is_costed)

    def test_apply_writes_an_audit_entry_with_old_and_new_attributed_to_the_md(self):
        tender = self.costing()
        item = self.priced_item(tender)
        row = tender.costing_rates
        tender.costing_rates.apply(self.md, margin_pct=D("15"), labour_rate_tzs_per_hour=D("16000"))

        entry = rates_entries(row).get(action=AuditAction.UPDATE)
        self.assertEqual(entry.actor, self.md)
        self.assertEqual(entry.changes["margin_pct"], ["20.00", "15.00"])
        self.assertEqual(entry.changes["labour_rate_tzs_per_hour"], ["15000.00", "16000.00"])
        self.assertEqual(set(entry.changes), {"margin_pct", "labour_rate_tzs_per_hour"})
        # The re-costed item records its own old and new figures, also as the MD.
        item_entry = AuditEntry.objects.filter(
            model="items.Item", object_id=str(item.pk), action=AuditAction.UPDATE
        ).latest("id")
        self.assertEqual(item_entry.actor, self.md)
        self.assertIn("total_selling_price_tzs", item_entry.changes)

    def test_applying_the_current_values_changes_and_records_nothing(self):
        tender = self.costing()
        self.priced_item(tender)
        entries_before = AuditEntry.objects.count()
        result = tender.costing_rates.apply(self.md, margin_pct=D("20.00"))
        self.assertEqual((result.items_recosted, result.old_total), (0, result.new_total))
        self.assertEqual(AuditEntry.objects.count(), entries_before)

    def test_an_invalid_value_saves_nothing(self):
        tender = self.costing()
        item = self.priced_item(tender)
        with self.assertRaises(ValidationError):
            tender.costing_rates.apply(self.md, margin_pct=D("15"), duty_pct=D("250"))
        self.assertEqual(TenderCostingRates.objects.get(tender=tender).values(), BASELINE_VALUES)
        self.assertEqual(Item.objects.get(pk=item.pk).total_selling_price_tzs, BASELINE_TOTAL)

    def test_the_md_may_tune_while_drafting_too(self):
        tender = self.drafting()
        self.priced_item(tender)
        self.assertEqual(tender.costing_rates.apply(self.md, margin_pct=D("15")).new_total, MARGIN_15_TOTAL)


class LockTests(ItemFixtures, TestCase):
    """Rates lock when the tender leaves COSTING (with R4); R9 back to COSTING unlocks them."""

    def submitted_for_audit(self):
        tender = self.costing()
        self.complete_item(  # R2-complete AND costed: the worked example, on FOB
            tender, fob_unit_price=D("100"), incoterm="FOB", quantity=10,
            risk_pct=D("5"), labour_hours_per_unit=D("2"),
        )
        tender.submit_for_audit(self.pm)  # the real transition: R2 passes, R4 stamps the rate lock
        self.assertEqual(tender.status, S.PENDING_AUDIT)
        return tender

    def test_leaving_costing_locks_the_rates(self):
        tender = self.submitted_for_audit()
        self.assertTrue(tender.costing_rates.is_locked)
        with self.assertRaises(TransitionError) as caught:
            tender.costing_rates.apply(self.md, margin_pct=D("15"))
        self.assertEqual(caught.exception.code, "r22")
        self.assertEqual(TenderCostingRates.objects.get(tender=tender).margin_pct, D("20"))

    def test_every_status_outside_drafting_and_costing_is_locked(self):
        self.assertEqual(set(RATES_EDITABLE_STATUSES), {S.DRAFTING, S.COSTING})
        for status in S.values:
            tender = self.new_tender()
            extra = {}
            if status in (S.PENDING_AUDIT, S.PENDING_EXECUTIVE_APPROVAL, S.READY_TO_SUBMIT, S.SUBMITTED):
                extra["exchange_rate_locked_at"] = tender.created_at
            if status == S.PENDING_ELIGIBILITY_DECISION:
                extra["previous_status"] = S.COSTING
            force_status(tender, status, **extra)
            with self.subTest(status=status):
                editable = status in (S.DRAFTING, S.COSTING)
                self.assertEqual(tender.costing_rates.is_locked, not editable)
                if editable:
                    tender.costing_rates.apply(self.md, margin_pct=D("15"))
                else:
                    with self.assertRaises(TransitionError):
                        tender.costing_rates.apply(self.md, margin_pct=D("15"))

    def test_the_model_refuses_a_locked_save_even_inside_its_own_write(self):
        tender = self.submitted_for_audit()
        row = tender.costing_rates
        row.margin_pct = D("15")
        with self.assertRaises(TransitionError), row._system_write():
            row.save()
        with self.assertRaises(TransitionError):
            row.clean()

    def test_preview_still_works_when_locked(self):
        tender = self.submitted_for_audit()
        self.assertEqual(tender.costing_rates.preview(margin_pct=D("15")).total_selling, MARGIN_15_TOTAL)
        self.assertEqual(TenderCostingRates.objects.get(tender=tender).margin_pct, D("20"))

    def test_a_negative_qc_verdict_returns_to_costing_and_unlocks_them(self):
        tender = self.submitted_for_audit()
        QCAudit = apps.get_model("audits", "QCAudit")
        QCAudit.objects.start(tender, self.auditor)
        tender.record_audit_result(self.auditor, compliant=False)  # R9
        tender.refresh_from_db()
        self.assertEqual(tender.status, S.COSTING)
        self.assertIsNone(tender.exchange_rate_locked_at)  # R4 lock cleared alongside
        self.assertFalse(tender.costing_rates.is_locked)
        result = tender.costing_rates.apply(self.md, margin_pct=D("15"))
        self.assertEqual(result.new_rates["margin_pct"], D("15"))
        tender.submit_for_audit(self.pm)  # and leaving COSTING again locks them again
        self.assertTrue(tender.costing_rates.is_locked)
