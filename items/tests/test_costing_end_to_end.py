"""
The section 4 costing chain, end to end through a real Item (database, exchange
rate, rate snapshot), against figures worked out BY HAND from the template
formulas (IMPERIAL EAST AFRICA LIMITED MASTER TEMPLATE 2026).

Every expected number below is a literal derived on paper and re-verified with
exact rational arithmetic in a script that imports nothing from this project. None
of them was read off this code's output: if the chain changes, they must be
re-derived, not re-pasted.

THE ITEM
    quantity 4, FOB 100.00 USD at 2,500 TZS/USD, local material 10,000 TZS a unit,
    imported (shipping applies), risk 5%, labour 2.5 hours a unit.
    Company rates: shipping 10%, duty 25%, C&F 5%, labour 15,000/hour, overhead 10%,
    margin 20%, maintenance 1%, cost of capital 22.25%.

HAND DERIVATION (A: imported)
    import_material  = 100 x 2,500 x 4                         = 1,000,000
    shipping         = 10% of import_material (FOB, not CIF)   =   100,000
    cif              = 1,000,000 + 100,000                     = 1,100,000
    duty             = 25% of CIF                              =   275,000
    cnf              = 5% of CIF                               =    55,000
    local_material   = 10,000 x 4                              =    40,000
    total_material   = 40,000 + 1,000,000 + 100,000
                       + 275,000 + 55,000                      = 1,470,000
    cost_of_capital  = 22.25% of 1,470,000   (stored, NOT added) =  327,075
    risk_management  = 5% of 1,470,000                         =    73,500
    overhead         = 10% of (1,470,000 + 73,500 = 1,543,500) =   154,350
    margin           = 20% of (1,543,500 + 154,350 = 1,697,850)=   339,570
    material_selling = 1,697,850 + 339,570                     = 2,037,420
    labour_hours     = 2.5 x 4                                 =        10
    labour_cost      = 10 x 15,000                             =   150,000
    labour_selling   = 150,000 x 1.30   (additive, NOT 1.32)   =   195,000
    maintenance      = 1% of (2,037,420 + 195,000 = 2,232,420) =  22,324.20
    total_selling    = 2,037,420 + 195,000 + 22,324.20         = 2,254,744.20
    unit_rate        = floor(2,254,744.20 / 4 = 563,686.05)    =   563,686

Each classic mistake lands on a different number: compounding labour (1.32) would
make total_selling 2,257,774.20; adding cost of capital would make it 2,581,819.20;
margin on (material + risk) only would make margin 308,700 instead of 339,570.
"""

import ast
from decimal import Decimal
from pathlib import Path

from django.test import SimpleTestCase, TestCase

import items.costing
from core.models import CostingDefaults
from items.models import Item
from tenders.models import Currency

from .helpers import ItemFixtures

D = Decimal

# --- A: imported ---------------------------------------------------------------------
EXPECTED_A = {
    "import_material": D("1000000"),
    "shipping": D("100000"),
    "cif": D("1100000"),
    "duty": D("275000"),
    "cnf": D("55000"),
    "local_material": D("40000"),
    "total_material": D("1470000"),
    "cost_of_capital": D("327075"),
    "risk_management": D("73500"),
    "overhead": D("154350"),
    "margin": D("339570"),
    "material_selling": D("2037420"),
    "labour_hours": D("10"),
    "labour_cost": D("150000"),
    "labour_selling": D("195000"),
    "maintenance": D("22324.20"),
    "total_selling_price_tzs": D("2254744.20"),
    "unit_selling_price_tzs": 563686,
}

# --- B: the same item, locally sourced (shipping_applies False) -------------------------
#   shipping = cif = duty = cnf = 0, so
#   total_material   = 40,000 + 1,000,000                       = 1,040,000
#   cost_of_capital  = 22.25% of 1,040,000                      =   231,400
#   risk_management  = 5% of 1,040,000                          =    52,000
#   overhead         = 10% of (1,040,000 + 52,000 = 1,092,000)  =   109,200
#   margin           = 20% of (1,092,000 + 109,200 = 1,201,200) =   240,240
#   material_selling = 1,201,200 + 240,240                      = 1,441,440
#   labour (unchanged)                                          =   195,000
#   maintenance      = 1% of (1,441,440 + 195,000 = 1,636,440)  =    16,364.40
#   total_selling    = 1,441,440 + 195,000 + 16,364.40          = 1,652,804.40
#   unit_rate        = floor(1,652,804.40 / 4 = 413,201.10)     =   413,201
EXPECTED_B = {
    "import_material": D("1000000"),
    "shipping": D("0"),
    "cif": D("0"),
    "duty": D("0"),
    "cnf": D("0"),
    "local_material": D("40000"),
    "total_material": D("1040000"),
    "cost_of_capital": D("231400"),
    "risk_management": D("52000"),
    "overhead": D("109200"),
    "margin": D("240240"),
    "material_selling": D("1441440"),
    "labour_hours": D("10"),
    "labour_cost": D("150000"),
    "labour_selling": D("195000"),
    "maintenance": D("16364.40"),
    "total_selling_price_tzs": D("1652804.40"),
    "unit_selling_price_tzs": 413201,
}

# --- C: the same imported item re-costed after the margin default moves 20% -> 25% -------
#   Everything up to total_material and risk/overhead is unchanged (1,470,000 / 73,500 / 154,350).
#   margin           = 25% of 1,697,850                         =   424,462.50
#   material_selling = 1,697,850 + 424,462.50                   = 2,122,312.50
#   labour_selling   = 150,000 x (1 + 10% + 25% = 1.35)         =   202,500
#   maintenance      = 1% of (2,122,312.50 + 202,500)           =    23,248.125
#   total_selling    = 2,122,312.50 + 202,500 + 23,248.125      = 2,348,060.625
#   unit_rate        = floor(2,348,060.625 / 4 = 587,015.15625) =   587,015
EXPECTED_C_CHANGED = {
    "margin": D("424462.50"),
    "material_selling": D("2122312.50"),
    "labour_selling": D("202500"),
    "maintenance": D("23248.125"),
    "total_selling_price_tzs": D("2348060.625"),
    "unit_selling_price_tzs": 587015,
}

# The section 4 rates as the item should have snapshotted them (fractions).
SNAPSHOT_RATES = {
    "shipping_rate": D("0.10"), "duty_rate": D("0.25"), "cnf_rate": D("0.05"),
    "labour_rate_tzs_per_hour": D("15000"), "overhead_rate": D("0.10"), "margin_rate": D("0.20"),
    "maintenance_rate": D("0.01"), "vat_rate": D("0.18"), "cost_of_capital_rate": D("0.2225"),
}


class CostingEndToEndTests(ItemFixtures, TestCase):
    ITEM = dict(
        quantity=4, fob_unit_price=D("100"), currency=Currency.USD, local_unit_cost=D("10000"),
        shipping_applies=True, incoterm="FOB",  # the template assumes FOB: shipping is added
        risk_pct=D("5"), labour_hours_per_unit=D("2.5"),
    )

    def cost_item(self, **over):
        """Create the item in a COSTING tender and read it BACK FROM THE DATABASE."""
        item = self.make_item(self.costing(), **{**self.ITEM, **over})
        return Item.objects.get(pk=item.pk)

    def assertFigures(self, item, expected):
        for field, value in expected.items():
            with self.subTest(figure=field):
                self.assertEqual(getattr(item, field), value)

    # -- the whole chain, to the exact figure -------------------------------------------

    def test_imported_item_matches_the_hand_worked_template(self):
        item = self.cost_item()
        self.assertEqual(item.fx_rate, D("2500"))
        self.assertFigures(item, EXPECTED_A)

    def test_locally_sourced_item_zeroes_exactly_the_four_import_charges(self):
        item = self.cost_item(shipping_applies=False)
        self.assertFigures(item, EXPECTED_B)

    # -- the eight things the audit was asked to check, pinned individually ---------------

    def test_1_shipping_is_a_percentage_of_fob_material_not_of_cif(self):
        item = self.cost_item()
        self.assertEqual(item.shipping, D("100000"))  # 10% of 1,000,000; 10% of CIF would be 110,000
        self.assertNotEqual(item.shipping, D("110000"))

    def test_2_duty_and_cnf_are_percentages_of_cif_which_includes_shipping(self):
        item = self.cost_item()
        self.assertEqual(item.cif, item.import_material + item.shipping)
        self.assertEqual((item.duty, item.cnf), (D("275000"), D("55000")))  # on 1,100,000, not 1,000,000
        self.assertNotEqual(item.duty, D("250000"))

    def test_3_overhead_compounds_on_material_plus_risk_and_margin_on_all_three(self):
        item = self.cost_item()
        self.assertEqual(item.overhead, D("154350"))  # (1,470,000 + 73,500) x 10%
        self.assertEqual(item.margin, D("339570"))  # (1,470,000 + 73,500 + 154,350) x 20%
        self.assertNotEqual(item.margin, D("308700"))  # margin on material + risk only: wrong

    def test_4_labour_markup_is_additive_1_30_not_compounding_1_32(self):
        item = self.cost_item()
        self.assertEqual(item.labour_selling, D("195000"))
        self.assertNotEqual(item.labour_selling, D("198000"))  # 150,000 x 1.10 x 1.20

    def test_5_maintenance_is_one_percent_of_material_plus_labour_selling(self):
        item = self.cost_item()
        self.assertEqual(item.maintenance, D("22324.20"))  # 1% of 2,232,420
        self.assertNotEqual(item.maintenance, D("20374.20"))  # 1% of material_selling alone

    def test_6_cost_of_capital_is_stored_but_excluded_from_total_selling(self):
        item = self.cost_item()
        self.assertEqual(item.cost_of_capital, D("327075"))  # 22.25% of total_material, stored
        self.assertEqual(item.total_selling_price_tzs, D("2254744.20"))
        self.assertNotEqual(item.total_selling_price_tzs, D("2254744.20") + D("327075"))

    # -- 7: the snapshot -------------------------------------------------------------------

    def test_7_item_snapshots_the_company_rates_it_was_costed_with(self):
        item = self.cost_item()
        self.assertIsNotNone(item.rates_snapshot_at)
        for field, value in SNAPSHOT_RATES.items():
            with self.subTest(rate=field):
                self.assertEqual(getattr(item, field), value)

    def test_7_changing_the_defaults_never_reprices_an_existing_item(self):
        item = self.cost_item()
        snapshot_at = item.rates_snapshot_at

        defaults = CostingDefaults.objects.get()
        defaults.margin_pct = D("25")
        defaults.save()
        self.assertEqual(CostingDefaults.current().margin_rate, D("0.25"))  # the default really did move

        # Re-saving the item (any edit at all) must not pick the new default up.
        item.specification = "Benchtop centrifuge, refrigerated, 24 x 1.5 ml"
        item.save()
        item = Item.objects.get(pk=item.pk)
        self.assertEqual(item.margin_rate, D("0.20"))
        self.assertEqual(item.rates_snapshot_at, snapshot_at)
        self.assertFigures(item, EXPECTED_A)  # every figure exactly as before

        # Re-pricing the stored quantity or prices also keeps the old rates.
        item.quantity = 4  # unchanged on purpose: still no reprice
        item.save()
        self.assertFigures(Item.objects.get(pk=item.pk), EXPECTED_A)

    def test_7_a_new_item_gets_the_new_defaults_and_recost_is_the_only_way_to_move_an_old_one(self):
        old = self.cost_item()
        defaults = CostingDefaults.objects.get()
        defaults.margin_pct = D("25")
        defaults.save()

        new = self.cost_item()  # costed after the change
        self.assertFigures(new, EXPECTED_C_CHANGED)
        self.assertEqual(new.margin_rate, D("0.25"))

        self.assertFigures(Item.objects.get(pk=old.pk), EXPECTED_A)  # the old one is untouched

        old.recost_at_current_rates()  # the deliberate action
        self.assertFigures(Item.objects.get(pk=old.pk), EXPECTED_C_CHANGED)
        self.assertEqual(Item.objects.get(pk=old.pk).margin_rate, D("0.25"))


class DecimalThroughoutTests(SimpleTestCase):
    """8: Decimal throughout, never float. Checked on the source, not on the output."""

    def test_costing_module_contains_no_float(self):
        path = Path(items.costing.__file__)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, float):
                offenders.append(f"{path.name}:{node.lineno} float literal {node.value!r}")
            if isinstance(node, ast.Name) and node.id == "float":
                offenders.append(f"{path.name}:{node.lineno} use of float")
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "float":
                offenders.append(f"{path.name}:{node.lineno} float() call")
            if isinstance(node, ast.Div) or (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)):
                pass  # true division of Decimals is fine; only floats are the problem
        self.assertEqual(offenders, [])

    def test_every_stored_figure_is_a_decimal_and_the_rate_is_an_integer(self):
        # The item model's derived fields are DecimalFields (BigIntegerField for the unit rate).
        from django.db import models

        for name in (*Item.BREAKDOWN_FIELDS.values(), *Item.RATE_FIELDS):
            field = Item._meta.get_field(name)
            with self.subTest(field=name):
                if name == "unit_selling_price_tzs":
                    self.assertIsInstance(field, models.BigIntegerField)
                else:
                    self.assertIsInstance(field, models.DecimalField)
                    self.assertNotIsInstance(field, models.FloatField)


# -- The Incoterm decides whether freight is already in the price -----------------------------
#
# 100 USD x 2,500 TZS/USD x 10 units = 2,500,000 TZS import material. No local material,
# no risk, no labour, so only the import chain and the three markups are in play.
# Derived by hand, not read off the code:
#
#   EXW / FOB / FCA  (freight NOT in the price: add 10% shipping)
#     shipping 250,000 -> cif 2,750,000 -> duty 25% = 687,500, C&F 5% = 137,500
#     total_material   = 2,750,000 + 687,500 + 137,500           = 3,575,000
#     x 1.10 (overhead) = 3,932,500;  x 1.20 (margin) = 4,719,000;  x 1.01 (maintenance)
#     total_selling    = 4,766,190;   unit_rate = 476,619
#
#   CIF / CFR        (freight IS in the price: shipping is zero, cif is the quoted price)
#     shipping 0 -> cif 2,500,000 -> duty 25% = 625,000, C&F 5% = 125,000
#     total_material   = 2,500,000 + 625,000 + 125,000           = 3,250,000
#     x 1.10 = 3,575,000;  x 1.20 = 4,290,000;  x 1.01
#     total_selling    = 4,332,900;   unit_rate = 433,290
#
# Charging 10% shipping on a CIF quote would give total_material 3,575,000: the freight
# counted twice, with duty and C&F compounding on the inflated CIF.

FREIGHT_NOT_IN_PRICE = {  # EXW, FOB, FCA
    "import_material": D("2500000"), "shipping": D("250000"), "cif": D("2750000"),
    "duty": D("687500"), "cnf": D("137500"), "total_material": D("3575000"),
    "total_selling_price_tzs": D("4766190"), "unit_selling_price_tzs": 476619,
}
FREIGHT_IN_PRICE = {  # CIF, CFR
    "import_material": D("2500000"), "shipping": D("0"), "cif": D("2500000"),
    "duty": D("625000"), "cnf": D("125000"), "total_material": D("3250000"),
    "total_selling_price_tzs": D("4332900"), "unit_selling_price_tzs": 433290,
}


class IncotermShippingTests(ItemFixtures, TestCase):
    """shipping_applies = imported vs local. The Incoterm = is freight already in the price."""

    def cost(self, incoterm, **over):
        fields = dict(
            quantity=10, fob_unit_price=D("100"), currency=Currency.USD, incoterm=incoterm,
            shipping_applies=True, risk_pct=D("0"), labour_hours_per_unit=D("0"),
        )
        item = self.make_item(self.costing(), **{**fields, **over})
        return Item.objects.get(pk=item.pk)

    def assertFigures(self, item, expected):
        for field, value in expected.items():
            with self.subTest(figure=field):
                self.assertEqual(getattr(item, field), value)

    def test_exw_adds_shipping(self):
        self.assertFigures(self.cost("EXW"), FREIGHT_NOT_IN_PRICE)

    def test_fob_adds_shipping(self):
        self.assertFigures(self.cost("FOB"), FREIGHT_NOT_IN_PRICE)

    def test_fca_adds_shipping(self):
        self.assertFigures(self.cost("FCA"), FREIGHT_NOT_IN_PRICE)

    def test_cif_adds_no_shipping_and_cif_is_the_quoted_price(self):
        self.assertFigures(self.cost("CIF"), FREIGHT_IN_PRICE)

    def test_cfr_adds_no_shipping_and_cif_is_the_quoted_price(self):
        self.assertFigures(self.cost("CFR"), FREIGHT_IN_PRICE)

    def test_a_cif_quote_is_cheaper_than_the_same_price_on_fob_by_exactly_the_freight_it_counted_twice(self):
        # 250,000 of shipping plus the duty and C&F charged on it (30% of 250,000 = 75,000).
        self.assertEqual(self.cost("FOB").total_material - self.cost("CIF").total_material, D("325000"))

    def test_a_locally_sourced_item_has_no_import_charges_whatever_the_incoterm(self):
        for incoterm in ("EXW", "FOB", "FCA", "CIF", "CFR"):
            with self.subTest(incoterm=incoterm):
                item = self.cost(incoterm, shipping_applies=False)
                self.assertEqual((item.shipping, item.cif, item.duty, item.cnf), (D("0"),) * 4)

    def test_the_default_incoterm_is_cif_so_an_unspecified_quote_adds_no_shipping(self):
        item = self.make_item(
            self.costing(), quantity=10, fob_unit_price=D("100"), currency=Currency.USD,
            risk_pct=D("0"), labour_hours_per_unit=D("0"),
        )
        self.assertFigures(Item.objects.get(pk=item.pk), FREIGHT_IN_PRICE)
