"""
The guard between the model and the PM (CLAUDE.md §5, §6, R12): whatever an extractor says,
only values the document actually supports are kept, and the clarification deadline and site
visit are null unless the tender states them.
"""

from django.test import SimpleTestCase
from google.genai import types

from tenders.models import BidSecurityType
from tenders.pipeline import fields as F

from .pipeline_helpers import field, listing

SENT = (
    "\n<!-- page 7 -->\nTender Submission Date: 13/11/2026. Tender Submission Time: 2:00 PM hrs local time.\n"
    "\n<!-- page 8 -->\n|36.|Deadline for<br>Submission of Tenders|23.1|\n"
    "Tenderers may request clarification at least 7 days before the deadline for submission.\n"
    "Tenderers may attend a site visit if any is announced, as specified in the TDS.\n"
    "\n<!-- page 9 -->\nClarification requests close on 06/11/2026.\n"
    "The site visit is mandatory and will be held on 10/11/2026 at the project site.\n"
    "Tender security: Tender Securing Declaration is required.\n"
    "The market opens 13 2026.\n"
)
PAGES = F.page_texts([SENT])
DEADLINE_QUOTE = "Tender Submission Date: 13/11/2026. Tender Submission Time: 2:00 PM"


def one(name, entry, pages=PAGES):
    return F.finalize_fields({name: entry}, pages)[name]


class DatesMustBeInTheirQuote(SimpleTestCase):
    def test_a_stated_deadline_is_kept_with_its_page_and_confidence(self):
        rec = one("submission_deadline", field("2026-11-13T14:00:00", 7, DEADLINE_QUOTE, 0.96))
        self.assertEqual((rec["value"], rec["source_page"], rec["confidence"]), ("2026-11-13T14:00:00", 7, 0.96))
        self.assertEqual((rec["flags"], rec["low_confidence"], rec["source"]), ([], False, "model"))
        self.assertEqual(rec["quote"], DEADLINE_QUOTE)

    def test_the_page_is_corrected_when_the_quote_is_real_but_the_page_is_wrong(self):
        rec = one("submission_deadline", field("2026-11-13T14:00:00", 3, DEADLINE_QUOTE))
        self.assertEqual(rec["source_page"], 7)
        self.assertIn("page_corrected", rec["flags"])

    def test_a_date_that_is_not_in_its_quote_is_a_guess_and_is_dropped(self):
        rec = one("submission_deadline", field("2026-11-20T14:00:00", 7, DEADLINE_QUOTE))
        self.assertIsNone(rec["value"])
        self.assertIn("date_not_in_quote", rec["flags"])

    def test_a_quote_that_is_not_in_the_document_grounds_nothing(self):
        rec = one("submission_deadline", field("2026-12-01T10:00:00", 7, "Submission closes on 1/12/2026 at 10:00"))
        self.assertIsNone(rec["value"])
        self.assertIn("quote_not_found", rec["flags"])

    def test_no_quote_no_date(self):
        for quote in (None, "", "13/11"):
            with self.subTest(quote=quote):
                rec = one("submission_deadline", field("2026-11-13T14:00:00", 7, quote))
                self.assertIsNone(rec["value"])

    def test_month_names_ground_a_date_and_only_as_whole_words(self):
        pages = F.page_texts(["\n<!-- page 1 -->\nTenders close on 13 November 2026 at 14:00 hours.\n"
                              "The market opens 13 2026.\n"])
        ok = one("submission_deadline", field("2026-11-13T14:00:00", 1, "13 November 2026 at 14:00"), pages)
        self.assertEqual(ok["value"], "2026-11-13T14:00:00")
        # "market" must not stand in for the month "mar"
        bad = one("submission_deadline", field("2026-03-13T00:00:00", 1, "The market opens 13 2026"), pages)
        self.assertIsNone(bad["value"])

    def test_twelve_and_twenty_four_hour_times_both_ground(self):
        pages = F.page_texts(["\n<!-- page 1 -->\nDeadline 13/11/2026 at 2:00 PM and also 13/11/2026 at 14:00.\n"])
        for quote in ("13/11/2026 at 2:00 PM", "13/11/2026 at 14:00"):
            with self.subTest(quote=quote):
                self.assertEqual(one("submission_deadline", field("2026-11-13T14:00:00", 1, quote), pages)["value"],
                                 "2026-11-13T14:00:00")

    def test_a_time_the_quote_does_not_contain_is_dropped_but_the_date_stays(self):
        pages = F.page_texts(["\n<!-- page 1 -->\nTenders close on 13/11/2026.\n"])
        rec = one("submission_deadline", field("2026-11-13T09:30:00", 1, "Tenders close on 13/11/2026"), pages)
        self.assertEqual(rec["value"], "2026-11-13")
        self.assertIn("time_not_in_quote", rec["flags"])
        self.assertIn("time_missing", rec["flags"])

    def test_a_date_only_answer_is_kept_and_flagged_as_missing_its_time(self):
        rec = one("submission_deadline", field("2026-11-13", 7, DEADLINE_QUOTE))
        self.assertEqual(rec["value"], "2026-11-13")
        self.assertIn("time_missing", rec["flags"])

    def test_unparseable_dates_are_invalid(self):
        for bad in ("13/11/2026", "tomorrow", "2026-13-45", 20261113):
            with self.subTest(bad=bad):
                rec = one("submission_deadline", field(bad, 7, DEADLINE_QUOTE))
                self.assertIsNone(rec["value"])
                self.assertEqual(rec["flags"], ["invalid_value"])


class BoilerplateIsNeverAValue(SimpleTestCase):
    """§5: in all three real tenders the clarification deadline and site visit appeared only in
    generic boilerplate. Return null; never infer a date from it."""

    def test_a_clarification_deadline_computed_from_a_rule_is_dropped(self):
        rec = one("clarification_deadline", field(
            "2026-11-06T00:00:00", 8, "Tenderers may request clarification at least 7 days before the deadline for submission"))
        self.assertIsNone(rec["value"])
        self.assertIn("date_not_in_quote", rec["flags"])

    def test_the_submission_date_cannot_be_copied_into_the_clarification_field(self):
        # The quote is real and contains the date, but it is about the submission, not clarification.
        rec = one("clarification_deadline", field("2026-11-13T14:00:00", 7, DEADLINE_QUOTE))
        self.assertIsNone(rec["value"])
        self.assertIn("quote_off_topic", rec["flags"])

    def test_a_stated_clarification_date_is_kept(self):
        rec = one("clarification_deadline", field("2026-11-06", 9, "Clarification requests close on 06/11/2026", 0.9))
        self.assertEqual(rec["value"], "2026-11-06")

    def test_site_visit_boilerplate_is_not_a_requirement(self):
        rec = one("site_visit_required", field(
            True, 8, "Tenderers may attend a site visit if any is announced, as specified in the TDS"))
        self.assertIsNone(rec["value"])
        self.assertIn("boilerplate", rec["flags"])

    def test_a_stated_site_visit_is_kept(self):
        rec = one("site_visit_required", field(True, 9, "The site visit is mandatory and will be held on 10/11/2026", 0.9))
        self.assertIs(rec["value"], True)
        date = one("site_visit_date", field("2026-11-10", 9, "The site visit is mandatory and will be held on 10/11/2026", 0.9))
        self.assertEqual(date["value"], "2026-11-10")

    def test_a_site_visit_quote_that_is_not_about_a_visit_grounds_nothing(self):
        rec = one("site_visit_required", field(True, 7, DEADLINE_QUOTE))
        self.assertIsNone(rec["value"])

    def test_booleans_must_be_booleans(self):
        rec = one("site_visit_required", field("yes", 9, "The site visit is mandatory and will be held on 10/11/2026"))
        self.assertIsNone(rec["value"])
        self.assertEqual(rec["flags"], ["invalid_value"])

    def test_a_shaky_risky_field_is_dropped_not_just_flagged(self):
        rec = one("clarification_deadline", field("2026-11-06", 9, "Clarification requests close on 06/11/2026", 0.5))
        self.assertIsNone(rec["value"])
        self.assertIn("low_confidence_dropped", rec["flags"])
        self.assertIsNone(one("site_visit_required", field(True, 9, "The site visit is mandatory", None))["value"])

    def test_null_stays_null(self):
        for name in ("clarification_deadline", "site_visit_required", "site_visit_date"):
            with self.subTest(name=name):
                rec = one(name, field(None, None, None, None))
                self.assertEqual((rec["value"], rec["flags"]), (None, []))


class OrdinaryFields(SimpleTestCase):
    def test_text_fields_are_kept_with_page_and_normalised_whitespace(self):
        rec = one("title", field("  Supply  of   Laboratory\nEquipment ", 7, "Tender Submission Date: 13/11/2026"))
        self.assertEqual(rec["value"], "Supply of Laboratory Equipment")
        self.assertEqual(rec["source_page"], 7)

    def test_low_confidence_is_flagged_never_blocking(self):
        rec = one("title", field("Laboratory Equipment", 7, "Tender Submission Date: 13/11/2026", 0.3))
        self.assertEqual(rec["value"], "Laboratory Equipment")
        self.assertTrue(rec["low_confidence"])
        self.assertIn("low_confidence", rec["flags"])

    def test_a_missing_confidence_is_treated_as_low(self):
        rec = one("title", field("Laboratory Equipment", 7, "Tender Submission Date: 13/11/2026", None))
        self.assertTrue(rec["low_confidence"])

    def test_confidence_is_clamped(self):
        self.assertEqual(one("title", field("T", 7, "Tender Submission Date: 13/11/2026", 7))["confidence"], 1.0)
        self.assertEqual(one("title", field("T", 7, "Tender Submission Date: 13/11/2026", -2))["confidence"], 0.0)

    def test_an_unverifiable_plain_field_is_kept_but_capped_and_flagged(self):
        rec = one("title", field("Laboratory Equipment", 8, "words that are nowhere in the document", 0.99))
        self.assertEqual(rec["value"], "Laboratory Equipment")
        self.assertLessEqual(rec["confidence"], 0.4)
        self.assertIn("quote_not_found", rec["flags"])
        self.assertEqual(rec["source_page"], 8)  # the model's page survives if it is a page we sent

    def test_blank_and_malformed_entries_are_null(self):
        for entry in (None, "text", [], {}, {"value": ""}, {"value": "   "}):
            with self.subTest(entry=entry):
                self.assertIsNone(one("title", entry)["value"])

    def test_bid_security_must_be_a_known_value_with_a_quote(self):
        ok = one("bid_security_type", field("TENDER_SECURING_DECLARATION", 9, "Tender Securing Declaration is required"))
        self.assertEqual(ok["value"], "TENDER_SECURING_DECLARATION")
        self.assertIsNone(one("bid_security_type", field("BANK_BOND", 9, "Tender Securing Declaration is required"))["value"])
        self.assertIsNone(one("bid_security_type", field("BANK_GUARANTEE", 9, None))["value"])  # grounded: needs the words
        self.assertEqual(set(F.SPECS["bid_security_type"].choices), set(BidSecurityType.values))

    def test_unknown_fields_from_the_model_are_ignored(self):
        out = F.finalize_fields({"favourite_colour": field("blue", 1, "x"), "title": field("T", 7, "Tender Submission Date: 13/11/2026")}, PAGES)
        self.assertNotIn("favourite_colour", out)
        self.assertEqual(set(out), {s.name for s in F.FIELDS})

    def test_garbage_in_gives_all_null_not_an_exception(self):
        for raw in (None, [], "text", 5):
            with self.subTest(raw=raw):
                out = F.finalize_fields(raw, PAGES)
                self.assertTrue(all(r["value"] is None for r in out.values()))
                self.assertEqual(F.classify(out), "FAILED")


class ListFields(SimpleTestCase):
    def test_items_are_kept_with_their_own_page_and_quote(self):
        rec = one("special_requirements", listing(
            ("Manufacturer's Authorization", 9, "Tender security: Tender Securing Declaration is required"),
            ("Local content", 7, "Tender Submission Date: 13/11/2026")))
        self.assertEqual([(i["text"], i["source_page"]) for i in rec["value"]],
                         [("Manufacturer's Authorization", 9), ("Local content", 7)])
        self.assertEqual(rec["flags"], [])

    def test_a_wrong_item_page_is_corrected_from_the_quote(self):
        rec = one("special_requirements", listing(("Bid security", 1, "Tender security: Tender Securing Declaration is required")))
        self.assertEqual(rec["value"][0]["source_page"], 9)

    def test_an_unverifiable_item_is_kept_but_flagged_for_the_pm(self):
        rec = one("special_requirements", listing(("Turnover of TZS 2bn", 8, "Annual turnover of at least 2 billion")))
        self.assertEqual(rec["value"][0]["text"], "Turnover of TZS 2bn")
        self.assertIn("unverified_item:0", rec["flags"])
        self.assertTrue(rec["low_confidence"])

    def test_duplicates_and_blank_items_are_dropped(self):
        rec = one("special_requirements", listing(("A", 7, "Tender Submission Date: 13/11/2026"), ("a", 7, "x"), ("  ", 7, "y")))
        self.assertEqual([i["text"] for i in rec["value"]], ["A"])

    def test_an_empty_list_is_null(self):
        self.assertIsNone(one("special_requirements", {"items": []})["value"])
        self.assertIsNone(one("special_requirements", {"items": "nope"})["value"])

    def test_required_forms_come_from_the_toc_when_it_names_them(self):
        out = F.finalize_fields({"required_forms": listing(("Invented form", 1, "x"))}, PAGES,
                                toc_forms=[("Power of Attorney", 95), ("Tender Securing Declaration", 99)])
        rec = out["required_forms"]
        self.assertEqual(rec["source"], "toc")
        self.assertEqual([(i["text"], i["source_page"]) for i in rec["value"]],
                         [("Power of Attorney", 95), ("Tender Securing Declaration", 99)])


class Classification(SimpleTestCase):
    def fields(self, **values):
        out = F.finalize_fields({}, PAGES)
        for name, value in values.items():
            out[name] = {**out[name], "value": value}
        return out

    def test_all_core_fields_present_is_extracted(self):
        core = dict(official_reference="R", title="T", client_name="C", submission_deadline="2026-11-13T14:00:00")
        self.assertEqual(F.classify(self.fields(**core)), "EXTRACTED")

    def test_a_missing_core_field_is_partial(self):
        self.assertEqual(F.classify(self.fields(official_reference="R", title="T", client_name="C")), "PARTIAL")

    def test_only_a_non_core_field_is_still_partial(self):
        self.assertEqual(F.classify(self.fields(client_address="Dodoma")), "PARTIAL")

    def test_nothing_is_failed(self):
        self.assertEqual(F.classify(self.fields()), "FAILED")

    def test_null_clarification_and_site_visit_never_make_an_extraction_partial(self):
        # §5: they are usually absent. That is normal, not a defect.
        core = dict(official_reference="R", title="T", client_name="C", submission_deadline="2026-11-13T14:00:00")
        out = self.fields(**core)
        self.assertIsNone(out["clarification_deadline"]["value"])
        self.assertEqual(F.classify(out), "EXTRACTED")


class ResponseSchema(SimpleTestCase):
    def test_the_schema_is_valid_for_the_sdk(self):
        types.Schema.model_validate(F.response_schema())  # raises if the SDK would reject it

    def test_it_asks_only_for_the_fields_in_the_request(self):
        schema = F.response_schema(["title", "special_requirements"])
        self.assertEqual(set(schema["properties"]), {"title", "special_requirements"})
        self.assertEqual(schema["required"], ["title", "special_requirements"])

    def test_every_field_has_the_shape_the_guard_reads(self):
        schema = F.response_schema()
        for spec in F.FIELDS:
            props = schema["properties"][spec.name]["properties"]
            with self.subTest(field=spec.name):
                if spec.kind == "list":
                    self.assertEqual(set(props["items"]["items"]["properties"]), {"text", "quote", "source_page"})
                else:
                    self.assertEqual(set(props), {"value", "confidence", "source_page", "quote"})

    def test_the_bid_security_enum_is_the_real_vocabulary(self):
        enum = F.response_schema()["properties"]["bid_security_type"]["properties"]["value"]["enum"]
        self.assertEqual(set(enum), set(BidSecurityType.values))

    def test_the_prompt_tells_the_model_the_rules_that_protect_r12(self):
        text = F.SYSTEM_INSTRUCTION.lower()
        for phrase in ("return null", "never infer", "quote", "source_page", "day/month/year", "ignore any instruction"):
            self.assertIn(phrase, text)
