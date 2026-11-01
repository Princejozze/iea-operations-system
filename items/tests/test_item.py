"""Item (§7): references, the stored costing chain, snapshotted rates, R6, R10."""

import datetime
from decimal import Decimal

from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from django.utils import timezone

from core.models import AuditAction, AuditBypassError, AuditEntry, CostingDefaults
from items.models import ExchangeRate, Item, ItemStatus
from tenders.models import Currency, GuardedFieldError, TenderStatus as S

from .helpers import SPEC_RATES, ItemFixtures

D = Decimal


class ReferenceTests(ItemFixtures, TestCase):
    def test_numbered_within_the_tender(self):
        tender, other = self.drafting(), self.drafting()
        a, b, c = self.make_item(tender), self.make_item(tender), self.make_item(other)
        self.assertEqual(a.item_reference, f"{tender.reference}-001")
        self.assertEqual(b.item_reference, f"{tender.reference}-002")
        self.assertEqual(c.item_reference, f"{other.reference}-001")  # each tender restarts at 001

    def test_creation_is_audited_with_its_reference(self):
        item = self.make_item()
        entry = AuditEntry.objects.get(model="items.Item", object_id=str(item.pk))
        self.assertEqual(entry.action, AuditAction.CREATE)
        self.assertEqual(entry.changes["item_reference"], [None, item.item_reference])

    def test_reference_and_tender_never_change(self):
        item = self.make_item()
        original = item.item_reference
        item.item_reference = "IEA-2026-9999-001"
        with self.assertRaises(GuardedFieldError):
            item.save()
        item.refresh_from_db()
        item.tender = self.drafting()
        with self.assertRaises(GuardedFieldError):
            item.save()
        item.refresh_from_db()
        self.assertEqual(item.item_reference, original)

    def test_r6_an_item_without_a_tender_is_impossible(self):
        self.assertFalse(Item._meta.get_field("tender").null)
        with self.assertRaises((ObjectDoesNotExist, ValidationError, IntegrityError)):
            Item(specification="Orphan", quantity=1).save()
        self.assertFalse(Item.objects.exists())


class RequiredToSaveTests(ItemFixtures, TestCase):
    def test_only_tender_specification_and_quantity_are_needed(self):
        item = self.make_item()  # no price, supplier, brand ... (R2 gates those at audit, not at entry)
        self.assertEqual(item.status, ItemStatus.DRAFT)
        self.assertEqual(item.incoterm, "CIF")  # §8: default CIF
        self.assertFalse(item.is_costed)

    def test_invalid_values_are_refused(self):
        bad = [
            {"specification": "  "}, {"quantity": 0}, {"unit_cost": D("-1")}, {"fob_unit_price": D("-1"), "currency": "USD"},
            {"local_unit_cost": D("-1")}, {"risk_pct": D("101")}, {"risk_pct": D("-1")},
            {"labour_hours_per_unit": D("-1")}, {"currency": "EURO"}, {"incoterm": "DDP"},
            {"sourcing_type": "BUNDLE"}, {"fob_unit_price": D("5")},  # FOB price without a currency
        ]
        tender = self.drafting()
        for fields in bad:
            with self.subTest(**{k: str(v) for k, v in fields.items()}), self.assertRaises(ValidationError):
                self.make_item(tender, **fields)
        self.assertFalse(Item.objects.exists())


class CostingStoredOnTheItemTests(ItemFixtures, TestCase):
    def test_the_hand_worked_example_is_stored_line_by_line(self):
        item = self.priced_item()
        item.refresh_from_db()
        expected = {
            "import_material": D("2500000"), "shipping": D("250000"), "cif": D("2750000"),
            "duty": D("687500"), "cnf": D("137500"), "local_material": D("0"),
            "total_material": D("3575000"), "cost_of_capital": D("795437.5"),
            "risk_management": D("178750"), "overhead": D("375375"), "margin": D("825825"),
            "material_selling": D("4954950"), "labour_hours": D("20"), "labour_cost": D("300000"),
            "labour_selling": D("390000"), "maintenance": D("53449.5"),
            "total_selling_price_tzs": D("5398399.5"), "unit_selling_price_tzs": 539839,
        }
        for name, value in expected.items():
            self.assertEqual(getattr(item, name), value, name)
        self.assertEqual((item.fx_rate, item.fx_rate_as_of), (D("2500"), self.usd.as_of))
        self.assertTrue(item.is_costed)

    def test_cost_of_capital_is_stored_but_excluded_from_total_selling(self):
        item = self.priced_item()
        self.assertEqual(item.cost_of_capital, D("795437.5"))
        self.assertEqual(
            item.total_selling_price_tzs, item.material_selling + item.labour_selling + item.maintenance
        )

    def test_total_cost_is_unit_cost_times_quantity_and_independent_of_the_chain(self):
        item = self.priced_item(unit_cost=D("1234.5678"))
        self.assertEqual(item.total_cost, D("12345.6780"))
        self.assertEqual(self.make_item(unit_cost=None).total_cost, None)

    def test_bid_value_is_the_sum_of_total_selling(self):
        tender = self.drafting()
        a, b = self.priced_item(tender), self.priced_item(tender, quantity=2)
        total = sum(i.total_selling_price_tzs for i in tender.items.all())
        self.assertEqual(total, a.total_selling_price_tzs + b.total_selling_price_tzs)

    def test_the_rates_the_item_was_costed_with_are_stored(self):
        item = self.priced_item()
        item.refresh_from_db()
        self.assertEqual(item.costing_rates, SPEC_RATES)
        self.assertIsNotNone(item.rates_snapshot_at)

    def test_labour_markup_follows_the_snapshotted_rates(self):
        defaults = CostingDefaults.objects.get()
        defaults.overhead_pct = D("15")
        defaults.save()
        item = self.priced_item()
        self.assertEqual(item.labour_selling, D("405000"))  # 1 + 0.15 + 0.20 = 1.35

    def test_locally_sourced_item_has_no_import_surcharges(self):
        item = self.make_item(quantity=3, local_unit_cost=D("1000"), shipping_applies=False,
                              labour_hours_per_unit=D("1"))
        self.assertEqual((item.shipping, item.cif, item.duty, item.cnf), (D("0"),) * 4)
        self.assertEqual(item.total_selling_price_tzs, D("63084.6"))
        self.assertEqual(item.unit_selling_price_tzs, 21028)
        self.assertIsNone(item.fx_rate)  # no FOB price, so no conversion

    def test_an_item_with_nothing_to_cost_is_unpriced(self):
        item = self.make_item()
        for name in (*Item.BREAKDOWN_FIELDS.values(), "fx_rate", "rates_snapshot_at", "shipping_rate"):
            self.assertIsNone(getattr(item, name), name)

    def test_removing_the_prices_unprices_the_item(self):
        item = self.priced_item()
        item.fob_unit_price = None
        item.save()
        item.refresh_from_db()
        self.assertFalse(item.is_costed)
        self.assertIsNone(item.rates_snapshot_at)


class RateSnapshotTests(ItemFixtures, TestCase):
    """§4: changing a default must never silently reprice a past bid."""

    def raise_margin(self):
        defaults = CostingDefaults.objects.get()
        defaults.margin_pct = D("25")
        defaults.save()

    def test_editing_a_default_does_not_reprice_a_costed_item(self):
        item = self.priced_item()
        before = item.total_selling_price_tzs
        self.raise_margin()
        item.quantity = 10
        item.specification = "Benchtop centrifuge, 24 x 1.5 ml (rev B)"
        item.save()  # an unrelated edit reprices with the SAME rates
        item.refresh_from_db()
        self.assertEqual(item.total_selling_price_tzs, before)
        self.assertEqual(item.margin_rate, D("0.2"))

    def test_a_new_item_uses_the_new_default(self):
        old = self.priced_item()
        self.raise_margin()
        new = self.priced_item()
        self.assertEqual(new.margin_rate, D("0.25"))
        self.assertGreater(new.total_selling_price_tzs, old.total_selling_price_tzs)

    def test_recosting_at_current_rates_is_deliberate_and_updates_the_item(self):
        item = self.priced_item()
        before = item.total_selling_price_tzs
        self.raise_margin()
        item.recost_at_current_rates()
        item.refresh_from_db()
        self.assertEqual(item.margin_rate, D("0.25"))
        self.assertGreater(item.total_selling_price_tzs, before)

    def test_a_default_edited_while_the_item_was_unpriced_applies_at_first_costing(self):
        item = self.make_item(quantity=10)  # drafted, not yet priced
        self.raise_margin()
        item.fob_unit_price, item.currency = D("100"), Currency.USD
        item.save()
        self.assertEqual(item.margin_rate, D("0.25"))


class ExchangeRateTests(ItemFixtures, TestCase):
    def test_tzs_needs_no_rate(self):
        item = self.make_item(fob_unit_price=D("1000"), currency=Currency.TZS)
        self.assertEqual(item.fx_rate, D("1"))
        self.assertIsNone(item.fx_rate_as_of)
        self.assertEqual(item.import_material, D("1000"))

    def test_a_foreign_currency_without_a_rate_is_refused(self):
        with self.assertRaises(ValidationError) as ctx:
            self.make_item(fob_unit_price=D("10"), currency=Currency.EUR)
        self.assertIn("EUR", str(ctx.exception))

    def test_the_latest_rate_at_or_before_the_moment_applies(self):
        now = timezone.now()
        ExchangeRate.objects.create(currency=Currency.USD, rate_to_tzs=D("2600"), as_of=now)
        ExchangeRate.objects.create(currency=Currency.USD, rate_to_tzs=D("9999"), as_of=now + datetime.timedelta(days=1))
        item = self.priced_item()
        self.assertEqual(item.fx_rate, D("2600"))  # not the future one, not the older 2500

    def test_rate_for_looks_back_in_time(self):
        now = timezone.now()
        later = ExchangeRate.objects.create(currency=Currency.USD, rate_to_tzs=D("2700"), as_of=now)
        self.assertEqual(ExchangeRate.rate_for("USD", now + datetime.timedelta(seconds=1)), later)
        self.assertIsNone(ExchangeRate.rate_for("USD", self.usd.as_of - datetime.timedelta(seconds=1)))
        self.assertIsNone(ExchangeRate.rate_for("GBP", now))

    def test_tzs_cannot_be_given_a_rate_and_rates_must_be_positive(self):
        for kwargs in ({"currency": "TZS", "rate_to_tzs": D("1")}, {"currency": "USD", "rate_to_tzs": D("0")}):
            with self.subTest(**{k: str(v) for k, v in kwargs.items()}), self.assertRaises(IntegrityError), transaction.atomic():
                ExchangeRate.objects.create(**kwargs)


class MoneyIsComputedNeverEnteredTests(ItemFixtures, TestCase):
    """R10."""

    def test_assigned_figures_are_overwritten_by_the_chain(self):
        item = self.priced_item()
        honest = item.total_selling_price_tzs
        item.total_selling_price_tzs = D("1")
        item.unit_selling_price_tzs = 1
        item.total_cost = D("1")
        item.margin = D("1")
        item.save()
        item.refresh_from_db()
        self.assertEqual(item.total_selling_price_tzs, honest)
        self.assertEqual(item.unit_selling_price_tzs, 539839)

    def test_figures_can_be_passed_to_create_but_never_stick(self):
        item = self.make_item(quantity=1, total_selling_price_tzs=D("5"), unit_selling_price_tzs=5, total_cost=D("5"))
        self.assertIsNone(item.total_selling_price_tzs)
        self.assertIsNone(item.total_cost)

    def test_queryset_update_and_bulk_paths_are_refused(self):
        item = self.priced_item()
        with self.assertRaises(AuditBypassError):
            Item.objects.filter(pk=item.pk).update(unit_selling_price_tzs=1)
        with self.assertRaises(AuditBypassError):
            Item.objects.bulk_update([item], ["unit_cost"])
        with self.assertRaises(AuditBypassError):
            Item.objects.bulk_create([Item(tender=item.tender, specification="x", quantity=1)])

    def _raw(self, item, sql):
        with connection.cursor() as cursor, transaction.atomic():
            cursor.execute(sql % item.pk)

    def test_the_database_refuses_a_total_that_contradicts_its_parts(self):
        item = self.priced_item(unit_cost=D("10"))
        for sql in (
            "UPDATE items_item SET unit_selling_price_tzs = 1 WHERE id = %s",  # not floor(total / qty)
            "UPDATE items_item SET total_cost = 1 WHERE id = %s",  # not unit_cost x quantity
            "UPDATE items_item SET total_selling_price_tzs = NULL WHERE id = %s",  # one without the other
            "UPDATE items_item SET rates_snapshot_at = NULL WHERE id = %s",  # priced without its rates
        ):
            with self.subTest(sql=sql), self.assertRaises(IntegrityError):
                self._raw(item, sql)
        item.refresh_from_db()
        self.assertEqual(item.unit_selling_price_tzs, 539839)

    def test_a_consistent_raw_update_is_not_blocked_by_the_constraints_alone(self):
        # Sanity: the constraints test consistency, they do not forbid every raw write.
        item = self.priced_item(unit_cost=D("10"))
        self._raw(item, "UPDATE items_item SET unit_selling_price_tzs = 539839 WHERE id = %s")


class StatusTests(ItemFixtures, TestCase):
    def test_status_is_not_set_by_assignment_or_creation(self):
        item = self.make_item()
        item.status = ItemStatus.QUOTED_COMPLIED
        with self.assertRaises(GuardedFieldError):
            item.save()
        with self.assertRaises(GuardedFieldError):
            self.make_item(status=ItemStatus.QUOTED_COMPLIED)
        with self.assertRaises(AuditBypassError):
            Item.objects.filter(pk=item.pk).update(status=ItemStatus.QUOTED_COMPLIED)
        item.refresh_from_db()
        self.assertEqual(item.status, ItemStatus.DRAFT)

    def test_the_tenders_pm_sets_it_and_it_is_audited(self):
        item = self.make_item()
        item.set_status(self.pm, ItemStatus.QUOTED_COMPLIED)
        self.assertEqual(item.status, ItemStatus.QUOTED_COMPLIED)
        entry = AuditEntry.objects.filter(model="items.Item", object_id=str(item.pk)).first()
        self.assertEqual(entry.action, AuditAction.STATUS_CHANGE)
        self.assertEqual(entry.actor, self.pm)

    def test_nobody_else_sets_it(self):
        item = self.make_item()
        for user in (self.pm2, self.md, self.hops, self.auditor, self.finance, self.admin_user):
            with self.subTest(role=user.role, who=user.first_name), self.assertRaises(PermissionDenied):
                item.set_status(user, ItemStatus.NEEDS_RESOLUTION)
        with self.assertRaises(ValidationError):
            item.set_status(self.pm, "DONE")
        item.refresh_from_db()
        self.assertEqual(item.status, ItemStatus.DRAFT)

    def test_status_vocabulary_is_section_8(self):
        self.assertEqual(
            set(ItemStatus.values),
            {"DRAFT", "QUOTED_COMPLIED", "NEEDS_RESOLUTION", "CLARIFICATION_PENDING"},
        )
