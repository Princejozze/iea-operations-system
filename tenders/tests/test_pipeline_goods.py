"""
Goods schedule parsing (CLAUDE.md §6): rows, lots, GFS codes, page-break fragments, bundles.
"""

from django.test import SimpleTestCase

from tenders.pipeline.goods import parse_goods_schedule

from .pipeline_helpers import GASCO, SAMPLE, TANAPA, fixture_pages, requires_fixtures

GOODS_HEADER = "|**S/N**|**Description**|**Unit of**<br>**measure**|**Quantity**|\n|---|---|---|---|"
RUNNING_HEADER = "_Goods : SPP-STD for Procurement of General Goods_ "


def lot_block(number, title, gfs):
    return (f"## **LOT NO. {number}** \n\n## **{title}** \n\n## **GFS Code: {gfs}** \n\n")


def parse(pages, start=None, end=None):
    return parse_goods_schedule(pages, start or min(pages), end or max(pages))


@requires_fixtures(GASCO, SAMPLE, TANAPA)
class RealTenderSchedules(SimpleTestCase):
    """Checked against the rendered PDF pages: S/N, unit of measure and quantity are exactly as printed."""

    def schedule(self, name, start, end):
        return parse_goods_schedule(fixture_pages(name), start, end)

    def test_gasco_two_installation_and_supply_rows(self):
        s = self.schedule(GASCO, 106, 106)
        self.assertEqual(len(s.lots), 1)
        lot = s.lots[0]
        self.assertEqual((lot.number, lot.gfs_code, lot.gfs_label),
                         ("TR24/001/2026/2027/G/133", "31122127", "Plant and Machinery"))
        self.assertEqual(lot.title, "Supply of Pressure Reduction and Metering Station (PRS)")
        self.assertEqual(
            [(r.sn, r.description, r.unit_of_measure, r.quantity) for r in lot.rows],
            [("1", "Installation of Pressure Reduction Station (PRS)", "No.", 3),
             ("2", "Supply of Pressure Reduction Station (PRS) Construction", "No.", 3)],
        )
        self.assertEqual(s.notes, [])

    def test_the_geological_survey_tender_is_laboratory_equipment(self):
        s = self.schedule(SAMPLE, 106, 106)
        lot = s.lots[0]
        self.assertEqual((lot.number, lot.gfs_code, lot.gfs_label),
                         ("TR120/2026/2027/G/12", "31122139", "Scientific Equipment"))
        self.assertEqual([(r.sn, r.description, r.unit_of_measure, r.quantity) for r in lot.rows],
                         [("1", "Carbon and sulfur analyzer", "pc", 1)])

    def test_tanapa_is_one_lot_row_that_bundles_two_machines(self):
        s = self.schedule(TANAPA, 44, 46)
        (lot,) = s.lots
        self.assertEqual((lot.gfs_code, lot.gfs_label), ("31122220", "Gardening equipment and machinery"))
        self.assertEqual(lot.title, "Procurement of Golf Course Machines")
        (row,) = lot.rows
        self.assertEqual((row.sn, row.unit_of_measure, row.quantity), ("1", "Lot", 1))
        # One row is NOT one machine: two numbered items live inside the single description.
        self.assertTrue(row.is_bundle)
        self.assertEqual(row.sub_items, 2)
        self.assertIn("Toro Reel master 5010-H Hybrid Fairway", row.description)
        self.assertIn("2. TORO GREENS", row.description)
        self.assertTrue(any("bundle" in w for w in row.warnings))
        self.assertEqual(row.bullets, 25)

    def test_tanapas_row_spills_onto_the_next_page_and_is_merged_whole(self):
        s = self.schedule(TANAPA, 44, 46)
        (row,) = s.rows
        self.assertEqual(row.pages, [45, 46])
        # the text that exists only in the p.46 fragment is in the one description
        self.assertIn("MOWER FLEX 2120", row.description)
        self.assertTrue(row.description.rstrip().endswith("04044-316001472"))
        self.assertEqual(s.notes, ["p.46: headerless 1-column fragment merged into the previous row"])

    def test_the_description_is_one_clean_line(self):
        for name, rng in ((TANAPA, (44, 46)), (GASCO, (106, 106))):
            for row in self.schedule(name, *rng).rows:
                with self.subTest(name=name[:6], sn=row.sn):
                    self.assertNotIn("<br>", row.description)
                    self.assertNotIn("**", row.description)
                    self.assertNotIn("\n", row.description)
                    self.assertEqual(row.description, row.description.strip())

    def test_gfs_is_found_even_when_the_section_was_not_located(self):
        # the whole-document fallback: scan every page
        for name, code in ((GASCO, "31122127"), (SAMPLE, "31122139"), (TANAPA, "31122220")):
            with self.subTest(name=name[:6]):
                s = parse(fixture_pages(name))
                self.assertEqual({l.gfs_code for l in s.lots}, {code})

    def test_a_whole_document_scan_does_not_duplicate_a_schedule_the_invitation_summarises(self):
        # TANAPA's Invitation (p.4) repeats the schedule; the real one is on pp.45-46.
        s = parse(fixture_pages(TANAPA))
        self.assertEqual(len(s.lots), 1)
        (row,) = s.rows  # one row, not the Invitation's copy AND the schedule's
        self.assertIn(row.pages, ([4, 5], [45, 46]))  # the copies are near-identical; either may survive
        self.assertIn("MOWER FLEX 2120", row.description)  # and it is the complete one
        self.assertTrue(any("fuller" in n or "shorter" in n for n in s.notes))

    def test_the_delivery_schedule_table_is_not_mistaken_for_goods(self):
        # GASCO p.107's table has S/N, Description, Quantity, Delivery Site, Delivery Period.
        s = parse(fixture_pages(GASCO), 106, 108)
        self.assertEqual(len(s.rows), 2)


class SyntheticSchedules(SimpleTestCase):
    def page(self, n, body):
        return f"{RUNNING_HEADER}\n\n{body}\n\n{n} \n"

    def test_several_lots_each_keep_their_own_gfs_code(self):
        pages = {
            1: self.page(1, lot_block("TR1/G/1", "Pumps", "31122001 - Pumps")
                         + GOODS_HEADER + "\n|1|Centrifugal pump|No.|4|\n|2|Gasket set|Set|10|\n"),
            2: self.page(2, lot_block("TR1/G/2", "Valves", "31122002 - Valves")
                         + GOODS_HEADER + "\n|1|Ball valve DN50|No.|12|\n"),
        }
        s = parse(pages)
        self.assertEqual([(l.number, l.gfs_code, l.gfs_label, [r.sn for r in l.rows]) for l in s.lots],
                         [("TR1/G/1", "31122001", "Pumps", ["1", "2"]), ("TR1/G/2", "31122002", "Valves", ["1"])])
        self.assertEqual([r.lot_number for r in s.rows], ["TR1/G/1", "TR1/G/1", "TR1/G/2"])

    def test_quantity_formats(self):
        rows = "".join(f"|{i}|Item {i}|No.|{q}|\n" for i, q in
                       enumerate(["1,200", "15", "2.0", "2.5", "", "ten", "0"], start=1))
        s = parse({1: self.page(1, lot_block("L", "T", "31122001 - X") + GOODS_HEADER + "\n" + rows)})
        self.assertEqual([r.quantity for r in s.rows], [1200, 15, 2, None, None, None, None])
        self.assertTrue(all(r.warnings for r in s.rows if r.quantity is None))  # never silently blank

    def test_a_row_wrapped_onto_a_blank_sn_line_joins_the_row_above(self):
        body = lot_block("L", "T", "31122001 - X") + GOODS_HEADER + (
            "\n|1|Supply of Pressure|No.|3|\n||Reduction Station Construction|||\n|2|Spare parts|Set|1|\n")
        s = parse({1: self.page(1, body)})
        self.assertEqual([r.description for r in s.rows], ["Supply of Pressure Reduction Station Construction", "Spare parts"])
        self.assertEqual([r.sn for r in s.rows], ["1", "2"])

    def test_a_four_column_fragment_at_the_top_of_the_next_page_continues_the_table(self):
        p1 = self.page(1, lot_block("L", "T", "31122001 - X") + GOODS_HEADER + "\n|1|Analyzer|No.|1|\n")
        p2 = self.page(2, "|2|Furnace|No.|2|\n|---|---|---|---|\n|3|Balance|No.|1|\n")
        # a markdown table's first row is its "header": here it is really data
        s = parse({1: p1, 2: p2})
        self.assertEqual([(r.sn, r.description) for r in s.rows],
                         [("1", "Analyzer"), ("2", "Furnace"), ("3", "Balance")])

    def test_a_fragment_that_does_not_open_the_next_page_is_not_swallowed(self):
        p1 = self.page(1, lot_block("L", "T", "31122001 - X") + GOODS_HEADER + "\n|1|Analyzer|No.|1|\n")
        p2 = self.page(2, "Some paragraph first.\n\n|Notes|\n|---|\n|unrelated text|\n")
        s = parse({1: p1, 2: p2})
        self.assertEqual(len(s.rows), 1)
        self.assertEqual(s.rows[0].description, "Analyzer")

    def test_a_fragment_after_a_page_that_did_not_end_in_a_goods_table_is_not_swallowed(self):
        p1 = self.page(1, lot_block("L", "T", "31122001 - X") + GOODS_HEADER + "\n|1|Analyzer|No.|1|\n\nClosing remarks.\n")
        p2 = self.page(2, "|stray fragment|\n|---|\n|text|\n")
        self.assertEqual(parse({1: p1, 2: p2}).rows[0].description, "Analyzer")

    def test_a_second_goods_table_on_the_next_page_is_its_own_table(self):
        p1 = self.page(1, lot_block("L1", "T", "31122001 - X") + GOODS_HEADER + "\n|1|Pump|No.|1|\n")
        p2 = self.page(2, lot_block("L2", "T2", "31122002 - Y") + GOODS_HEADER + "\n|1|Valve|No.|2|\n")
        s = parse({1: p1, 2: p2})
        self.assertEqual([(l.number, [r.description for r in l.rows]) for l in s.lots],
                         [("L1", ["Pump"]), ("L2", ["Valve"])])

    def test_an_unrelated_table_with_other_headers_is_ignored(self):
        body = ("|Technical Requirements|Description|\n|---|---|\n|Power|230 V|\n\n"
                "|S/N|Description|Quantity|Delivery Site|\n|---|---|---|---|\n|1|Pump|4|Dodoma|\n")
        self.assertEqual(parse({1: self.page(1, body)}).rows, [])

    def test_nothing_to_parse_gives_an_empty_schedule_not_an_error(self):
        for pages in ({}, {1: ""}, {1: "no tables here"}, {1: "|a|b|\n|---|---|\n|1|2|\n"}):
            with self.subTest(pages=pages):
                s = parse_goods_schedule(pages, 1, 5)
                self.assertEqual((s.lots, s.rows), ([], []))

    def test_a_row_with_no_description_is_flagged(self):
        body = lot_block("L", "T", "31122001 - X") + GOODS_HEADER + "\n|1||No.|1|\n"
        self.assertIn("empty description", parse({1: self.page(1, body)}).rows[0].warnings)

    def test_numbered_sub_items_are_counted_but_never_split(self):
        desc = "1. Pump A rated 5 kW 2. Pump B rated 7 kW 3. Controller"
        body = lot_block("L", "T", "31122001 - X") + GOODS_HEADER + f"\n|1|{desc}|Lot|1|\n"
        (row,) = parse({1: self.page(1, body)}).rows
        self.assertEqual((row.sub_items, len(parse({1: self.page(1, body)}).rows)), (3, 1))
        self.assertEqual(row.description, desc)

    def test_a_single_numbered_marker_is_not_a_list(self):
        body = lot_block("L", "T", "31122001 - X") + GOODS_HEADER + "\n|1|Model 2. Rated 5 kW|No.|1|\n"
        self.assertEqual(parse({1: self.page(1, body)}).rows[0].sub_items, 0)
