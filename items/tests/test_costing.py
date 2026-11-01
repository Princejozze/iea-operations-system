"""
The §4 costing chain as a pure function. Expected figures are worked out by hand
below, not by running the code: if the chain changes, these must be re-derived.
"""

import dataclasses
from decimal import Decimal

from django.test import SimpleTestCase

from core.models import CostingDefaults
from items.costing import CostingInputs, compute_costing

from .helpers import SPEC_RATES

D = Decimal


def inputs(**over):
    base = dict(
        fob_unit_price=D("100"), fx_rate=D("2500"), local_unit_cost=D("0"), quantity=10,
        shipping_applies=True, risk_rate=D("0.05"), labour_hours_per_unit=D("2"),
    )
    base.update(over)
    return CostingInputs(**base)


class WorkedExampleTests(SimpleTestCase):
    """100 USD FOB x 2,500 TZS/USD x 10 units, imported, 5% risk, 2 labour hours a unit."""

    def setUp(self):
        self.r = compute_costing(inputs(), SPEC_RATES)

    def test_import_chain(self):
        self.assertEqual(self.r.import_material, D("2500000"))  # 100 x 2500 x 10
        self.assertEqual(self.r.shipping, D("250000"))  # 10%
        self.assertEqual(self.r.cif, D("2750000"))
        self.assertEqual(self.r.duty, D("687500"))  # 25% of CIF
        self.assertEqual(self.r.cnf, D("137500"))  # 5% of CIF

    def test_material_chain(self):
        self.assertEqual(self.r.local_material, D("0"))
        self.assertEqual(self.r.total_material, D("3575000"))  # 2.5m + 250k + 687.5k + 137.5k
        self.assertEqual(self.r.risk_management, D("178750"))  # 5%
        self.assertEqual(self.r.overhead, D("375375"))  # (3,575,000 + 178,750) x 10%
        self.assertEqual(self.r.margin, D("825825"))  # (3,575,000 + 178,750 + 375,375) x 20%
        self.assertEqual(self.r.material_selling, D("4954950"))

    def test_labour_markup_is_additive_130_not_compounding_132(self):
        self.assertEqual(self.r.labour_hours, D("20"))
        self.assertEqual(self.r.labour_cost, D("300000"))  # 20 h x 15,000
        self.assertEqual(self.r.labour_selling, D("390000"))  # x 1.30
        self.assertNotEqual(self.r.labour_selling, D("396000"))  # x 1.10 x 1.20 = 1.32

    def test_totals(self):
        self.assertEqual(self.r.maintenance, D("53449.5"))  # (4,954,950 + 390,000) x 1%
        self.assertEqual(self.r.total_selling, D("5398399.5"))
        self.assertEqual(self.r.unit_rate, 539839)  # floor(539,839.95)

    def test_cost_of_capital_is_computed_but_never_added(self):
        self.assertEqual(self.r.cost_of_capital, D("795437.5"))  # 3,575,000 x 22.25%
        self.assertEqual(
            self.r.total_selling, self.r.material_selling + self.r.labour_selling + self.r.maintenance
        )
        with_it = self.r.total_selling + self.r.cost_of_capital
        self.assertNotEqual(self.r.total_selling, with_it)

    def test_vat_is_not_applied_per_item(self):
        self.assertNotEqual(self.r.total_selling, D("5398399.5") * D("1.18"))
        self.assertFalse(hasattr(self.r, "vat"))


class LocallySourcedTests(SimpleTestCase):
    """Locally sourced: no FOB, shipping does not apply. 3 units at 1,000 TZS, 1 labour hour a unit."""

    def setUp(self):
        self.r = compute_costing(
            inputs(fob_unit_price=D("0"), fx_rate=D("1"), local_unit_cost=D("1000"), quantity=3,
                   shipping_applies=False, risk_rate=D("0"), labour_hours_per_unit=D("1")),
            SPEC_RATES,
        )

    def test_shipping_cif_duty_cnf_are_all_zero(self):
        for name in ("import_material", "shipping", "cif", "duty", "cnf"):
            self.assertEqual(getattr(self.r, name), D("0"), name)

    def test_figures(self):
        self.assertEqual(self.r.total_material, D("3000"))
        self.assertEqual(self.r.cost_of_capital, D("667.5"))
        self.assertEqual(self.r.overhead, D("300"))
        self.assertEqual(self.r.margin, D("660"))  # 3,300 x 20%
        self.assertEqual(self.r.material_selling, D("3960"))
        self.assertEqual(self.r.labour_selling, D("58500"))  # 45,000 x 1.30
        self.assertEqual(self.r.maintenance, D("624.6"))
        self.assertEqual(self.r.total_selling, D("63084.6"))
        self.assertEqual(self.r.unit_rate, 21028)  # floor(21,028.2)

    def test_not_applying_shipping_only_zeroes_the_import_surcharges(self):
        # An imported line on the same prices does carry them; import_material itself is unaffected.
        imported = compute_costing(inputs(shipping_applies=True), SPEC_RATES)
        local_flag = compute_costing(inputs(shipping_applies=False), SPEC_RATES)
        self.assertEqual(imported.import_material, local_flag.import_material)
        self.assertEqual(local_flag.total_material, D("2500000"))
        self.assertGreater(imported.total_material, local_flag.total_material)


class RatesDriveTheChainTests(SimpleTestCase):
    def test_labour_markup_is_derived_from_the_rates_not_hardcoded(self):
        self.assertEqual(SPEC_RATES.labour_markup, D("1.30"))
        edited = dataclasses.replace(SPEC_RATES, overhead_rate=D("0.15"), margin_rate=D("0.20"))
        self.assertEqual(edited.labour_markup, D("1.35"))
        r = compute_costing(inputs(), edited)
        self.assertEqual(r.labour_selling, D("405000"))  # 300,000 x 1.35

    def test_every_rate_is_used(self):
        base = compute_costing(inputs(), SPEC_RATES).total_selling
        for name, bumped in (
            ("shipping_rate", D("0.11")), ("duty_rate", D("0.26")), ("cnf_rate", D("0.06")),
            ("labour_rate_tzs_per_hour", D("16000")), ("overhead_rate", D("0.11")),
            ("margin_rate", D("0.21")), ("maintenance_rate", D("0.02")),
        ):
            with self.subTest(rate=name):
                changed = compute_costing(inputs(), dataclasses.replace(SPEC_RATES, **{name: bumped}))
                self.assertNotEqual(changed.total_selling, base)

    def test_cost_of_capital_rate_changes_only_the_stored_cost_of_capital(self):
        other = compute_costing(inputs(), dataclasses.replace(SPEC_RATES, cost_of_capital_rate=D("0.99")))
        base = compute_costing(inputs(), SPEC_RATES)
        self.assertNotEqual(other.cost_of_capital, base.cost_of_capital)
        self.assertEqual(other.total_selling, base.total_selling)

    def test_vat_rate_changes_nothing_per_item(self):
        other = compute_costing(inputs(), dataclasses.replace(SPEC_RATES, vat_rate=D("0.99")))
        self.assertEqual(other.as_dict(), compute_costing(inputs(), SPEC_RATES).as_dict())

    def test_the_model_defaults_equal_the_spec_table(self):
        # The test constants above are the §4 table; CostingDefaults must agree with them.
        self.assertEqual(CostingDefaults().rates(), SPEC_RATES)


class ArithmeticTests(SimpleTestCase):
    def test_unit_rate_on_a_low_fraction(self):
        r = compute_costing(inputs(fob_unit_price=D("0"), local_unit_cost=D("100"), quantity=3,
                                   risk_rate=D("0"), labour_hours_per_unit=D("0")), SPEC_RATES)
        # 300 x 1.1 x 1.2 = 396; x 1.01 = 399.96; / 3 = 133.32 -> 133
        self.assertEqual(r.total_selling, D("399.96"))
        self.assertEqual(r.unit_rate, 133)

    def test_floor_rounds_down_on_a_high_fraction(self):
        r = compute_costing(inputs(fob_unit_price=D("0"), local_unit_cost=D("150"), quantity=3,
                                   risk_rate=D("0"), labour_hours_per_unit=D("0")), SPEC_RATES)
        # 450 x 1.1 x 1.2 = 594; x 1.01 = 599.94; / 3 = 199.98 -> 199, not 200
        self.assertEqual(r.total_selling, D("599.94"))
        self.assertEqual(r.unit_rate, 199)

    def test_stored_figures_have_six_places_and_unit_rate_agrees_with_the_stored_total(self):
        r = compute_costing(inputs(fob_unit_price=D("0.0375"), quantity=7, risk_rate=D("0.033")), SPEC_RATES)
        for name, value in r.as_dict().items():
            if name != "unit_rate":
                self.assertGreaterEqual(value.as_tuple().exponent, -6, name)
        self.assertLessEqual(r.unit_rate * 7, r.total_selling)
        self.assertLess(r.total_selling, (r.unit_rate + 1) * 7)

    def test_a_quantity_below_one_is_refused(self):
        for q in (0, -3):
            with self.subTest(q=q), self.assertRaises(ValueError):
                compute_costing(inputs(quantity=q), SPEC_RATES)

    def test_the_result_does_not_depend_on_the_ambient_decimal_precision(self):
        import decimal

        with decimal.localcontext() as ctx:
            ctx.prec = 5
            low = compute_costing(inputs(), SPEC_RATES)
        self.assertEqual(low, compute_costing(inputs(), SPEC_RATES))
