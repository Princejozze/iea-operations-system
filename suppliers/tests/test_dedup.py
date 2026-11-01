"""
Legacy supplier de-duplication (§7: 426 name strings for roughly 62 companies).

The real exports are not in the repo, so the volume test below is SYNTHETIC: 62
invented company names, each spelled several ways, totalling 426 strings. It
proves the helper's behaviour at that shape, not its accuracy on the real data;
run it against the real names and review the output before trusting the count.
"""

import itertools

from django.test import SimpleTestCase, TestCase

from suppliers.dedup import find_existing, group_exact, normalise_name, suggest_fuzzy
from suppliers.models import Supplier


class NormaliseNameTests(SimpleTestCase):
    def test_spelling_variants_share_one_key(self):
        variants = [
            "Mettler Toledo", "METTLER TOLEDO", "mettler-toledo", "Mettler  Toledo Ltd.",
            "Mettler Toledo Limited", "The Mettler Toledo Co., Ltd", "Méttler Toledo",
        ]
        self.assertEqual({normalise_name(v) for v in variants}, {"mettler toledo"})

    def test_ampersand_and_and_match(self):
        self.assertEqual(normalise_name("Smith & Sons Ltd"), normalise_name("Smith and Sons Limited"))

    def test_legal_words_removed_only_as_whole_tokens(self):
        self.assertEqual(normalise_name("Unlimited Labs"), "unlimited labs")
        self.assertEqual(normalise_name("Cosmo Instruments"), "cosmo instruments")

    def test_different_companies_stay_different(self):
        self.assertNotEqual(normalise_name("Agilent Technologies"), normalise_name("Waters Technologies"))

    def test_blank_and_junk_give_empty_key(self):
        for junk in ("", "   ", None, "---", "..."):
            with self.subTest(junk=junk):
                self.assertEqual(normalise_name(junk), "")

    def test_name_made_only_of_legal_words_still_has_a_key(self):
        self.assertEqual(normalise_name("Co Ltd"), "co ltd")


class GroupExactTests(SimpleTestCase):
    def test_groups_variants_and_counts_rows(self):
        groups, unusable = group_exact(
            ["Shimadzu", "SHIMADZU LTD", "Shimadzu", "Waters", "waters corp.", "Waters"]
        )
        self.assertEqual(unusable, [])
        self.assertEqual([g.canonical for g in groups], ["Shimadzu", "Waters"])
        shimadzu, waters = groups
        self.assertEqual(shimadzu.occurrences, 3)
        self.assertEqual(shimadzu.variants, ["Shimadzu", "SHIMADZU LTD"])
        self.assertEqual(waters.occurrences, 3)

    def test_canonical_prefers_most_common_then_mixed_case(self):
        groups, _ = group_exact(["ACME LABS", "Acme Labs", "ACME LABS", "acme labs"])
        self.assertEqual(groups[0].canonical, "ACME LABS")  # most common wins
        groups, _ = group_exact(["ACME LABS", "Acme Labs"])
        self.assertEqual(groups[0].canonical, "Acme Labs")  # tie: not shouting

    def test_result_is_independent_of_input_order(self):
        names = ["B Corp Labs", "b corp labs ltd", "Alpha Test", "ALPHA TEST", "alpha-test Ltd"]
        results = {
            tuple((g.canonical, tuple(g.variants), g.occurrences) for g in group_exact(list(p))[0])
            for p in itertools.permutations(names)
        }
        self.assertEqual(len(results), 1)

    def test_unusable_names_are_returned_not_dropped_or_merged(self):
        groups, unusable = group_exact(["Real Co Labs", "", "   ", "---", None])
        self.assertEqual(len(groups), 1)
        self.assertEqual(unusable, ["", "   ", "---", None])

    def test_empty_input(self):
        self.assertEqual(group_exact([]), ([], []))


class SuggestFuzzyTests(SimpleTestCase):
    def groups(self, *names):
        return group_exact(list(names))[0]

    def test_typo_is_suggested_not_merged(self):
        groups = self.groups("Shimadzu Scientific", "Shimazdu Scientific", "Waters")
        self.assertEqual(len(groups), 3)  # exact grouping did not merge the typo
        suggestions = suggest_fuzzy(groups)
        self.assertEqual(len(suggestions), 1)
        pair = {suggestions[0].left.canonical, suggestions[0].right.canonical}
        self.assertEqual(pair, {"Shimadzu Scientific", "Shimazdu Scientific"})
        self.assertGreaterEqual(suggestions[0].score, 0.9)

    def test_unrelated_names_are_not_suggested(self):
        self.assertEqual(suggest_fuzzy(self.groups("Agilent Technologies", "Mettler Toledo", "Sartorius")), [])

    def test_short_keys_are_never_compared(self):
        self.assertEqual(suggest_fuzzy(self.groups("Eppi", "Eppo")), [])

    def test_threshold_is_respected(self):
        groups = self.groups("Shimadzu Scientific", "Shimazdu Scientific")
        self.assertEqual(suggest_fuzzy(groups, threshold=0.999), [])


class FindExistingTests(TestCase):
    def test_matches_a_stored_supplier_by_normalised_name(self):
        stored = Supplier.objects.create(name="Mettler Toledo")
        Supplier.objects.create(name="Waters")
        self.assertEqual(find_existing("METTLER-TOLEDO Ltd.", Supplier.objects.all()), stored)

    def test_no_match_and_blank_return_none(self):
        Supplier.objects.create(name="Waters")
        self.assertIsNone(find_existing("Agilent", Supplier.objects.all()))
        self.assertIsNone(find_existing("   ", Supplier.objects.all()))


class LegacyVolumeShapeTests(SimpleTestCase):
    """Synthetic: 62 invented companies spelled 426 ways collapse back to 62."""


    def build(self):
        companies = [f"{a} {b}" for a in ("Alder", "Borealis", "Cedar", "Delta", "Ember", "Fjord",
                                           "Garnet", "Harbor", "Indigo", "Juniper", "Kestrel")
                     for b in ("Scientific", "Instruments", "Analytics", "Medical", "Labs", "Systems")][:62]
        self.assertEqual(len(companies), 62)
        names = []
        for company in companies:  # 6 spellings each = 372 strings
            names += [
                company, company.upper(), company.lower(), company.replace(" ", "-"),
                f"{company}  ",  # trailing/double spacing
                company + " Ltd.",
            ]
        # 54 more, one extra variant for the first 54 companies = 426 exactly.
        names += [f"{c} Limited" for c in companies[:54]]
        return companies, names

    def test_426_strings_collapse_to_62_groups(self):
        companies, names = self.build()
        self.assertEqual(len(names), 426)
        self.assertGreater(len(set(names)), 62)  # genuinely messy input
        groups, unusable = group_exact(names)
        self.assertEqual(unusable, [])
        self.assertEqual(len(groups), 62)
        self.assertEqual(sum(g.occurrences for g in groups), 426)
        # Distinct invented names are not near-duplicates of each other.
        self.assertEqual(suggest_fuzzy(groups, threshold=0.95), [])
