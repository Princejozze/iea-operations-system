"""Numeric variance flagging: flag the difference, never judge it (§7, §11, §14)."""

import json

from django.test import SimpleTestCase

from audits.variance import compare_specifications, describe, extract_quantities


class TheRealExampleTests(SimpleTestCase):
    def test_required_656kg_offered_620kg_is_flagged_with_exact_numbers(self):
        (flag,) = compare_specifications("Platform scale, capacity 656kg", "Capacity: 620 kg")
        self.assertEqual(flag["type"], "DIFFERENCE")
        self.assertEqual(flag["dimension"], "mass")
        self.assertEqual(flag["required"], "656kg")
        self.assertEqual(flag["offered"], "620 kg")
        self.assertEqual(flag["required_value"], "656")
        self.assertEqual(flag["offered_value"], "620")
        self.assertEqual(flag["unit"], "kg")
        self.assertEqual(flag["difference"], "-36")
        self.assertEqual(flag["percent"], "-5.49")
        self.assertEqual(
            describe(flag), "Mass: required 656kg, offered 620 kg (-36 kg, -5.49%)"
        )

    def test_a_higher_offer_is_flagged_too_it_is_a_difference_not_a_shortfall(self):
        (flag,) = compare_specifications("656 kg", "700 kg")
        self.assertEqual(flag["difference"], "44")
        self.assertIn("(+44 kg, 6.71%)", describe(flag))


class NeverJudgesTests(SimpleTestCase):
    def test_a_flag_carries_no_verdict_or_pass_fail_language(self):
        (flag,) = compare_specifications("656 kg", "620 kg")
        forbidden = {"verdict", "compliant", "compliance", "pass", "fail", "acceptable", "shortfall", "ok"}
        self.assertFalse(forbidden & {k.lower() for k in flag})
        text = describe(flag).lower()
        for word in ("fail", "pass", "complies", "acceptable", "shortfall", "reject", "non-compliant"):
            self.assertNotIn(word, text)

    def test_an_empty_result_means_nothing_comparable_differed_not_that_it_complies(self):
        self.assertEqual(compare_specifications("656 kg", "656 kg"), [])
        self.assertEqual(compare_specifications("Stainless steel", "Mild steel"), [])  # wrong, but no numbers

    def test_flags_are_json_safe_so_they_can_be_stored(self):
        flags = compare_specifications("656 kg, 2 l", "620 kg, 3 l")
        self.assertEqual(json.loads(json.dumps(flags)), flags)
        self.assertEqual(len(flags), 2)


class ComparabilityTests(SimpleTestCase):
    def test_equal_values_in_different_units_do_not_differ(self):
        self.assertEqual(compare_specifications("1 kg", "1000 g"), [])
        self.assertEqual(compare_specifications("2 l", "2000 ml"), [])
        self.assertEqual(compare_specifications("0.1 mg", "100 ug"), [])

    def test_different_values_in_different_units_report_in_the_required_unit(self):
        (flag,) = compare_specifications("1 kg", "900 g")
        self.assertEqual((flag["difference"], flag["unit"], flag["offered_value"]), ("-0.1", "kg", "0.9"))

    def test_only_the_same_kind_of_quantity_is_compared(self):
        self.assertEqual(compare_specifications("656 kg", "620 l"), [])  # mass vs volume

    def test_a_quantity_on_one_side_only_raises_no_flag(self):
        self.assertEqual(compare_specifications("Capacity 656 kg", "Heavy duty scale"), [])
        self.assertEqual(compare_specifications("Heavy duty scale", "Capacity 620 kg"), [])

    def test_blank_and_none_are_fine(self):
        self.assertEqual(compare_specifications("", ""), [])
        self.assertEqual(compare_specifications(None, "620 kg"), [])

    def test_case_and_spacing_do_not_matter(self):
        self.assertEqual(len(compare_specifications("656KG", "620 Kg")), 1)
        self.assertEqual(compare_specifications("656 kg", "656KG"), [])

    def test_thousands_separators_and_decimal_commas(self):
        self.assertEqual(compare_specifications("1,200 kg", "1200 kg"), [])
        self.assertEqual(compare_specifications("0,5 mg", "0.5 mg"), [])
        (flag,) = compare_specifications("1,200 kg", "1,100 kg")
        self.assertEqual(flag["difference"], "-100")

    def test_several_quantities_pair_by_kind_and_order(self):
        flags = compare_specifications(
            "Balance 220 g x 0.1 mg, 230 V", "Balance 220 g x 0.01 mg, 110 V"
        )
        self.assertEqual({(f["dimension"], f["offered"]) for f in flags}, {("mass", "0.01 mg"), ("voltage", "110 V")})

    def test_unequal_counts_are_reported_as_unpaired_not_guessed(self):
        (flag,) = compare_specifications("220 g and 0.1 mg", "220 g")
        self.assertEqual(flag["type"], "UNPAIRED")
        self.assertEqual(flag["required"], "220 g; 0.1 mg")
        self.assertNotIn("difference", flag)

    def test_ranges(self):
        self.assertEqual(compare_specifications("10-20 kg", "15 kg"), [])  # inside
        self.assertEqual(compare_specifications("10-20 kg", "10 to 20 kg"), [])  # same range
        (flag,) = compare_specifications("10-20 kg", "25 kg")
        self.assertEqual(flag["type"], "RANGE")
        self.assertNotIn("difference", flag)


class ParsingTests(SimpleTestCase):
    def texts(self, text):
        return [q.text for q in extract_quantities(text)]

    def test_count_multipliers_and_unknown_words_are_not_quantities(self):
        self.assertEqual(self.texts("Benchtop centrifuge, 24 x 1.5 ml"), ["1.5 ml"])
        self.assertEqual(self.texts("2 units, 5 years warranty"), [])

    def test_compound_units_are_not_misread(self):
        self.assertEqual(self.texts("Concentration 5 mg/ml"), [])
        self.assertEqual(self.texts("Flow 10 l/min"), [])

    def test_a_number_glued_to_a_name_is_not_a_quantity(self):
        self.assertEqual(self.texts("Model M3 kg-50"), [])

    def test_degrees_celsius(self):
        self.assertEqual(self.texts("Operating range up to 300 °C"), ["300 °C"])
        (flag,) = compare_specifications("Max 300 °C", "Max 250 °C")
        self.assertEqual(flag["difference"], "-50")

    def test_zero_required_has_no_percent(self):
        (flag,) = compare_specifications("0 kg", "5 kg")
        self.assertIsNone(flag["percent"])
        self.assertNotIn("%", describe(flag))
