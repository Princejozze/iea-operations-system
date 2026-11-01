"""
TOC parsing and section location (CLAUDE.md §6): locate by TOC page range, CONFIRM BY CONTENT
SIGNATURE — aliases are a hint only — and fall back to the whole document, never fail.
"""

import re

from django.test import SimpleTestCase

from tenders.pipeline import sections as S
from tenders.pipeline.converter import check_text_layer
from tenders.pipeline.tables import est_tokens, md_tables, norm, shredded_pages, table_is_shredded
from tenders.pipeline.toc import parse_toc

from .pipeline_helpers import GASCO, SAMPLE, TANAPA, converted, fixture_pages, requires_fixtures

K, G = S.Kind, S.Group

# What each real tender contains, established from the PDFs themselves (and checked by eye
# against the rendered pages): section -> (first page, last page).
EXPECTED = {
    GASCO: {K.TENDER_DATA_SHEET: (71, 80), K.INVITATION: (7, 9), K.ELIGIBILITY: (81, 88),
            K.GOODS_SCHEDULE: (106, 106), K.SPECIFICATIONS: (108, 110), K.FORMS: (89, 102)},
    SAMPLE: {K.TENDER_DATA_SHEET: (71, 80), K.INVITATION: (7, 9), K.ELIGIBILITY: (81, 88),
             K.GOODS_SCHEDULE: (106, 106), K.SPECIFICATIONS: (108, 110), K.FORMS: (89, 102)},
    TANAPA: {K.INVITATION: (3, 7), K.ELIGIBILITY: (16, 19), K.GOODS_SCHEDULE: (44, 46),
             K.SPECIFICATIONS: (49, 50), K.FORMS: (51, 60)},
}


def wrong_content(pages):
    """GASCO with the goods table's header and the technical-requirements table (and its heading)
    reworded, so the TOC names are intact but the content signatures no longer hold."""
    pages = dict(pages)
    pages[106] = pages[106].replace("**Unit of**<br>**measure**", "**Packaging**")
    pages[108] = pages[108].replace("**Technical Requirements**", "**Parameter**").replace(
        "3. Technical Specifications", "3. Equipment Details")
    return pages


def plan_for(name, pages=None):
    pages = pages if pages is not None else fixture_pages(name)
    doc = converted(name)
    toc = parse_toc(pages)
    return S.plan_extraction(pages, doc.page_raw_text(), toc, check_text_layer(doc)), pages, toc


@requires_fixtures(GASCO, SAMPLE, TANAPA)
class TocParsingTests(SimpleTestCase):
    def test_entry_counts_and_pages_of_each_toc(self):
        for name, count, toc_pages in ((GASCO, 40, (2, 3)), (SAMPLE, 40, (2, 3)), (TANAPA, 19, (2,))):
            with self.subTest(name=name[:6]):
                toc = parse_toc(fixture_pages(name))
                self.assertEqual((len(toc.entries), toc.pages), (count, toc_pages))

    def test_a_toc_that_runs_over_two_pages_is_read_whole(self):
        names = [e.name for e in parse_toc(fixture_pages(GASCO)).entries]
        self.assertEqual(names[0], "LIST OF ABBREVIATION")  # first TOC page
        self.assertEqual(names[-1], "Advance Payment Guarantee")  # second TOC page

    def test_levels_come_from_capitalisation(self):
        entries = {e.name: e for e in parse_toc(fixture_pages(GASCO)).entries}
        self.assertEqual(entries["TENDER DATA SHEET"].level, "top")
        self.assertEqual(entries["QUALIFICATION AND EVALUATION CRITERIA"].level, "top")
        self.assertEqual(entries["Technical Specifications"].level, "sub")

    def test_ranges_run_to_the_next_entry(self):
        entries = {e.name: e for e in parse_toc(fixture_pages(GASCO)).entries}
        spec = entries["Technical Specifications"]
        self.assertEqual((spec.page, spec.end), (108, 110))  # Drawings starts on 111
        tds = entries["TENDER DATA SHEET"]
        self.assertEqual((tds.page, tds.end, tds.end_incl_children), (71, 73, 80))  # children to QEC at 81

    def test_the_hint_lists_names_and_pages(self):
        hint = parse_toc(fixture_pages(TANAPA)).hint()
        self.assertIn("SCHEDULE OF REQUIREMENTS .... p.44", hint)
        self.assertEqual(len(hint.splitlines()), 19)


class TocEdgeCases(SimpleTestCase):
    def test_no_table_of_contents_gives_an_empty_toc(self):
        toc = parse_toc({1: "A tender with no contents page.", 2: "More text."})
        self.assertFalse(toc)
        self.assertEqual((toc.entries, toc.pages), ((), ()))

    def test_an_empty_document_gives_an_empty_toc(self):
        self.assertFalse(parse_toc({}))

    def test_rows_pointing_past_the_end_of_the_document_are_dropped(self):
        pages = {1: "# Table of contents\n\n|ALPHA|2|\n|---|---|\n|BETA|3|\n|GAMMA|900|\n|DELTA|3|\n", 2: "x", 3: "y"}
        self.assertEqual([e.name for e in parse_toc(pages).entries], ["ALPHA", "BETA", "DELTA"])

    def test_the_phrase_deep_in_the_document_is_not_a_toc(self):
        pages = {n: "text" for n in range(1, 30)}
        pages[25] = "Table of contents\n\n|A|1|\n|B|2|\n|C|3|\n"
        self.assertFalse(parse_toc(pages))


@requires_fixtures(GASCO, SAMPLE, TANAPA)
class LocatingTheSectionsTests(SimpleTestCase):
    def test_every_section_is_located_in_all_three_tenders(self):
        for name, expected in EXPECTED.items():
            plan, _, _ = plan_for(name)
            with self.subTest(name=name[:6]):
                found = {k: tuple(v["pages"]) for k, v in plan["sections"].items()}
                self.assertEqual(found, expected)
                self.assertEqual(plan["whole_document_fallback"], [])

    def test_each_located_section_carries_the_evidence_that_confirmed_it(self):
        for name in EXPECTED:
            plan, _, _ = plan_for(name)
            for kind, section in plan["sections"].items():
                with self.subTest(name=name[:6], kind=kind):
                    self.assertTrue(section["evidence"])
                    self.assertIn(section["confidence"], ("high", "medium"))

    def test_tanapas_specifications_have_no_heading_and_are_confirmed_by_the_table(self):
        plan, pages, _ = plan_for(TANAPA)
        spec = plan["sections"][K.SPECIFICATIONS]
        self.assertEqual(spec["pages"], [49, 50])
        self.assertTrue(any("Technical Requirements" in e for e in spec["evidence"]))
        headings = [l for p in (49, 50) for l in pages[p].splitlines()
                    if l.strip().startswith(("#", "**")) and norm(l) in ("specifications", "technical specifications")]
        self.assertEqual(headings, [])  # no heading anywhere in the range: content alone confirmed it

    def test_toc_names_that_differ_from_the_body_still_locate(self):
        # §6: the TOC says "Schedule of Goods...", the body says "List of Goods...".
        plan, pages, _ = plan_for(GASCO)
        self.assertEqual(plan["sections"][K.GOODS_SCHEDULE]["toc_name"], "Schedule of Goods and Related Services")
        self.assertIn("List of Goods and Related Services", pages[106])

    def test_a_mini_competition_has_no_tender_data_sheet_and_that_is_not_a_failure(self):
        plan, _, _ = plan_for(TANAPA)
        self.assertNotIn(K.TENDER_DATA_SHEET, plan["sections"])
        self.assertEqual(plan["groups"][G.METADATA], [K.INVITATION])  # its Invitation states the deadline
        self.assertEqual(plan["whole_document_fallback"], [])

    def test_a_general_goods_tender_takes_its_metadata_from_the_tds_and_the_invitation(self):
        plan, _, _ = plan_for(GASCO)
        self.assertEqual(plan["groups"][G.METADATA], [K.TENDER_DATA_SHEET, K.INVITATION])

    def test_required_forms_are_named_by_the_toc(self):
        names = plan_for(TANAPA)[0]["forms_named_in_toc"]
        self.assertEqual(names, ["Manufacturer's Authorization Form", "Special Power of Attorney",
                                 "Declaration against Fraud and Corruption", "Tender Securing Declaration"])
        general = plan_for(GASCO)[0]["forms_named_in_toc"]
        self.assertIn("Declaration Against Fraud and Corruption", general)
        self.assertIn("Tender Security or Tender Securing Declaration", general)


@requires_fixtures(GASCO, TANAPA)
class AliasesAreOnlyAHintTests(SimpleTestCase):
    """A list tuned on three documents will miss on a fourth (§6). The content decides."""

    def rename(self, pages, mapping):
        pages = dict(pages)
        for n in (2, 3):
            for old, new in mapping.items():
                pages[n] = pages[n].replace(f"|{old}|", f"|{new}|")
        return pages

    def test_an_unfamiliar_toc_name_over_the_right_content_is_still_located(self):
        pages = self.rename(fixture_pages(GASCO), {
            "Schedule of Goods and Related Services": "Bill of Quantities",
            "Technical Specifications": "Equipment Parameters",
            "QUALIFICATION AND EVALUATION CRITERIA": "BIDDER SUITABILITY TESTS",
            "TENDER DATA SHEET": "PARTICULARS OF THIS TENDER",
        })
        plan, _, _ = plan_for(GASCO, pages)
        found = plan["sections"]
        self.assertEqual(found[K.GOODS_SCHEDULE]["pages"], [106, 106])
        self.assertEqual(found[K.SPECIFICATIONS]["pages"], [108, 110])
        self.assertEqual(found[K.ELIGIBILITY]["pages"], [81, 88])
        self.assertEqual(found[K.TENDER_DATA_SHEET]["pages"], [71, 80])
        for kind in (K.GOODS_SCHEDULE, K.SPECIFICATIONS, K.ELIGIBILITY, K.TENDER_DATA_SHEET):
            self.assertFalse(found[kind]["alias_matched"])
            self.assertEqual(found[kind]["confidence"], "medium")  # confirmed by content, name unfamiliar
        self.assertEqual(plan["whole_document_fallback"], [])

    def test_a_known_name_over_the_wrong_content_is_not_located(self):
        # The TOC names are exactly the aliases, but the pages no longer hold a goods table or a
        # technical-requirements table: the name alone must not be enough.
        pages = wrong_content(fixture_pages(GASCO))
        plan, _, _ = plan_for(GASCO, pages)
        self.assertNotIn(K.GOODS_SCHEDULE, plan["sections"])
        self.assertNotIn(K.SPECIFICATIONS, plan["sections"])
        self.assertIn("matched by name but its pages do not look right", plan["not_located"][K.GOODS_SCHEDULE])
        self.assertEqual(plan["groups"][G.GOODS_SCHEDULE], "WHOLE_DOCUMENT")
        self.assertEqual(plan["groups"][G.SPECIFICATIONS], "WHOLE_DOCUMENT")
        self.assertEqual(set(plan["whole_document_fallback"]), {G.GOODS_SCHEDULE, G.SPECIFICATIONS})
        # sections whose content is intact are unaffected
        self.assertEqual(plan["sections"][K.ELIGIBILITY]["pages"], [81, 88])

    def test_a_wrong_toc_page_number_cannot_confirm_a_huge_range(self):
        # "Schedule of Goods" claims page 40, so its range would run 40..106 and CONTAIN the goods
        # table. Content is confirmed only near the start of the range, so that is not accepted;
        # whatever is located instead must be tight and must still contain the table.
        pages = dict(fixture_pages(GASCO))
        pages[2] = pages[2].replace("|Schedule of Goods and Related Services|106|", "|Schedule of Goods and Related Services|40|")
        plan, _, _ = plan_for(GASCO, pages)
        goods = plan["sections"].get(K.GOODS_SCHEDULE)
        self.assertIsNotNone(goods)
        start, end = goods["pages"]
        self.assertNotEqual(goods["toc_page"], 40)
        self.assertLessEqual(end - start, 3)
        self.assertTrue(start <= 106 <= end)

    def test_itt_prose_about_forms_is_not_the_forms_section(self):
        # A loose signature once "located" the forms at an ITT subsection (pp.22-70) whose prose
        # mentions tender security, powers of attorney and so on. Only form headings count.
        pages = dict(fixture_pages(GASCO))
        pages[2] = pages[2].replace("|Schedule of Goods and Related Services|106|", "|Schedule of Goods and Related Services|40|")
        plan, _, _ = plan_for(GASCO, pages)
        self.assertEqual(plan["sections"][K.FORMS]["pages"], [89, 102])

    def test_the_forms_are_located_by_their_headings_under_any_name(self):
        pages = self.rename(fixture_pages(GASCO), {"TENDERING FORMS": "PRESCRIBED DOCUMENTS"})
        plan, _, _ = plan_for(GASCO, pages)
        self.assertEqual(plan["sections"][K.FORMS]["pages"], [89, 102])
        self.assertEqual(plan["sections"][K.FORMS]["confidence"], "medium")

    def test_an_alias_match_beats_an_unfamiliar_name_when_both_pass(self):
        pages = fixture_pages(GASCO)
        spec = S.SPECS[K.SPECIFICATIONS]
        toc = parse_toc(pages)
        section, _ = S.locate_section(pages, toc, spec)
        self.assertTrue(section.alias_matched)
        self.assertEqual(section.confidence, "high")

    def test_the_narrowest_confirmed_range_wins_over_its_parent(self):
        # "PROCURING ENTITY'S REQUIREMENTS" (a top-level parent) spans the schedule too, but the
        # specific entry is chosen.
        plan, _, _ = plan_for(GASCO)
        self.assertEqual(plan["sections"][K.GOODS_SCHEDULE]["pages"], [106, 106])


@requires_fixtures(GASCO, SAMPLE, TANAPA)
class WholeDocumentFallbackTests(SimpleTestCase):
    def test_no_toc_at_all_sends_every_group_the_whole_document(self):
        pages = {n: re.sub(r"(?i)table of contents", "Contents page", t) for n, t in fixture_pages(TANAPA).items()}
        plan, _, toc = plan_for(TANAPA, pages)
        self.assertFalse(toc)
        self.assertEqual(plan["sections"], {})
        self.assertEqual(set(plan["whole_document_fallback"]), set(G.ALL))
        ready = S.ready_sections(pages, plan)
        self.assertEqual({r.group for r in ready}, set(G.ALL))
        for r in ready:
            self.assertEqual((r.method, r.ranges, r.sources), ("whole_document", ((1, 73),), ()))
            self.assertEqual(r.text.count("<!-- page "), 73)

    def test_the_fallback_carries_the_toc_as_a_hint(self):
        pages = wrong_content(fixture_pages(GASCO))
        plan, _, _ = plan_for(GASCO, pages)
        ready = {r.group: r for r in S.ready_sections(pages, plan)}
        fallback = ready[G.GOODS_SCHEDULE]
        self.assertEqual(fallback.method, "whole_document")
        self.assertEqual(fallback.ranges, ((1, 173),))
        self.assertIn("TENDER DATA SHEET .... p.71", fallback.toc_hint)
        self.assertIn("Schedule of Goods and Related Services .... p.106", fallback.toc_hint)
        # while the sections that were located keep their narrow ranges and need no hint
        self.assertEqual(ready[G.ELIGIBILITY].method, "toc+content")
        self.assertEqual(ready[G.ELIGIBILITY].toc_hint, "")

    def test_a_tender_with_unfamiliar_everything_never_raises(self):
        for pages in ({}, {1: "x"}, {1: "|a|b|\n|---|---|\n|1|2|\n", 2: ""}):
            with self.subTest(pages=len(pages)):
                doc = converted(TANAPA)
                plan = S.plan_extraction(pages, {}, parse_toc(pages), check_text_layer(doc))
                S.ready_sections(pages, plan)


@requires_fixtures(GASCO, SAMPLE, TANAPA)
class SectionsReadyToSendTests(SimpleTestCase):
    def ready(self, name):
        plan, pages, _ = plan_for(name)
        return {r.group: r for r in S.ready_sections(pages, plan)}, plan, pages

    def test_one_ready_section_per_required_group_with_page_markers(self):
        for name in EXPECTED:
            ready, _, _ = self.ready(name)
            with self.subTest(name=name[:6]):
                self.assertEqual(set(ready), set(G.ALL))
                for r in ready.values():
                    self.assertEqual(r.method, "toc+content")
                    self.assertTrue(r.text.startswith("\n<!-- page "))  # provenance travels with the text

    def test_the_goods_section_is_just_its_page(self):
        ready, _, _ = self.ready(GASCO)
        goods = ready[G.GOODS_SCHEDULE]
        self.assertEqual(goods.ranges, ((106, 106),))
        self.assertIn("GFS Code: 31122127 - Plant and Machinery", goods.text)
        self.assertEqual(goods.text.count("<!-- page "), 1)

    def test_the_metadata_section_joins_the_tds_and_the_invitation(self):
        ready, _, _ = self.ready(GASCO)
        meta = ready[G.METADATA]
        self.assertEqual(meta.ranges, ((71, 80), (7, 9)))
        self.assertIn("Deadline for Submission of Tenders", meta.text.replace("<br>", " "))
        self.assertIn("<!-- page 77 -->", meta.text)

    def test_sections_are_a_fraction_of_the_document(self):
        for name in EXPECTED:
            ready, _, pages = self.ready(name)
            whole = est_tokens("".join(pages.values()))
            sent = sum(r.est_tokens for r in ready.values())
            with self.subTest(name=name[:6]):
                self.assertLess(sent, whole * 0.5)

    def test_est_tokens_is_an_estimate_not_a_count(self):
        self.assertEqual(est_tokens("a" * 400), 100)
        self.assertEqual(est_tokens("a" * 401), 101)


@requires_fixtures(GASCO, SAMPLE, TANAPA)
class ShreddedTablesGetRawTextTests(SimpleTestCase):
    """§6: Tender Data Sheet p.73 (in both general-goods tenders) came out as seven misaligned
    columns and carries the Manufacturer's Authorization requirement. Send the raw text."""

    def test_page_73_is_detected_as_shredded_in_both_general_goods_tenders(self):
        for name in (GASCO, SAMPLE):
            with self.subTest(name=name[:6]):
                self.assertIn(73, shredded_pages(fixture_pages(name)))

    def test_the_converter_really_did_lose_the_cell_structure_there(self):
        rows = md_tables(fixture_pages(GASCO)[73])[0]
        self.assertTrue(table_is_shredded(rows))
        self.assertEqual(len(rows[0]), 7)
        self.assertEqual(sum(1 for c in rows[0] if not c), 3)

    def test_the_plan_replaces_that_page_with_its_raw_text_and_keeps_the_requirement(self):
        plan, pages, _ = plan_for(GASCO)
        self.assertIn("73", plan["raw_text_pages"])
        raw = plan["raw_text_pages"]["73"]
        self.assertIn("Manufacturer", raw)
        self.assertIn("Authorization is required", raw)
        meta = {r.group: r for r in S.ready_sections(pages, plan)}[G.METADATA]
        self.assertIn(73, meta.raw_text_pages)
        self.assertIn(S.RAW_NOTE, meta.text)
        self.assertIn("Authorization is required", meta.text)
        # the shredded Markdown table for that page is no longer what is sent
        self.assertNotIn("||||", meta.text.split("<!-- page 73 -->")[1].split("<!-- page 74 -->")[0])

    def test_clean_pages_keep_their_parsed_tables(self):
        plan, pages, _ = plan_for(GASCO)
        meta = {r.group: r for r in S.ready_sections(pages, plan)}[G.METADATA]
        self.assertNotIn(72, meta.raw_text_pages)
        self.assertIn("|**TDS**<br>**No.**|", meta.text)  # page 72's table, as converted

    def test_the_goods_and_specification_pages_are_never_shredded(self):
        for name, ranges in ((GASCO, [(106, 106), (108, 110)]), (SAMPLE, [(106, 106), (108, 110)]), (TANAPA, [(44, 46), (49, 50)])):
            flagged = set(shredded_pages(fixture_pages(name)))
            for lo, hi in ranges:
                with self.subTest(name=name[:6], pages=(lo, hi)):
                    self.assertFalse(flagged & set(range(lo, hi + 1)))

    def test_tanapa_needs_no_raw_text_in_any_section(self):
        self.assertEqual(plan_for(TANAPA)[0]["raw_text_pages"], {})

    def test_the_whole_document_fallback_carries_every_shredded_page_as_raw_text(self):
        pages = {n: re.sub(r"(?i)table of contents", "Contents page", t) for n, t in fixture_pages(GASCO).items()}
        plan, _, _ = plan_for(GASCO, pages)
        self.assertEqual(set(plan["whole_document_fallback"]), set(G.ALL))
        self.assertIn("73", plan["raw_text_pages"])
        self.assertEqual(set(map(int, plan["raw_text_pages"])), set(shredded_pages(pages)))


class ShreddedHeuristicTests(SimpleTestCase):
    def test_single_column_blocks_are_text_boxes_not_tables(self):
        self.assertFalse(table_is_shredded([["some wrapped text"], ["more"]]))

    def test_a_clean_table_is_not_shredded(self):
        self.assertFalse(table_is_shredded([["S/N", "Description"], ["1", "Pump"], ["2", "Valve"]]))

    def test_empty_header_cells_or_an_empty_column_are_shredded(self):
        self.assertTrue(table_is_shredded([["", "", "x", "y"], ["1", "2", "3", "4"]]))
        self.assertTrue(table_is_shredded([["a", "b", "c"], ["1", "", "3"], ["4", "", "6"]]))

    def test_the_leading_empty_cells_of_a_shredded_row_survive_parsing(self):
        rows = md_tables("||||Manufacturer<br>Local|Sole|\n|---|---|---|---|---|\n|11.|Number|3.3|Not Applicab|le|\n")[0]
        self.assertEqual(rows[0][:3], ["", "", ""])
        self.assertEqual(len(rows[0]), 5)
