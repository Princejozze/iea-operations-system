"""RFQ letterhead rendering from a plain context dict (no database needed)."""

import copy
from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, override_settings

from documents.rendering import render
from documents.rfq import SALES_CC, validate_rfq_context


def rfq_context(**overrides):
    context = {
        "supplier_name": "Mettler Toledo",
        "supplier_contact": "A. Keller",
        "tender_reference": "IEA-2026-0042",
        "date": "1 November 2026",
        "reply_by": "10 November 2026",
        "sender_name": "Gift Minja",
        "sender_title": "Project Manager",
        "items": [
            {"reference": "IEA-2026-0042-001", "specification": "Analytical balance, 0.1 mg\nCalibration certificate", "quantity": 2, "unit_of_measure": "pcs"},
            {"reference": "IEA-2026-0042-002", "specification": "Autoclave 100 L", "quantity": "1", "make_brand": "Tuttnauer"},
        ],
    }
    context.update(overrides)
    return context


class RenderRfqTests(SimpleTestCase):
    def test_subject_and_both_bodies_carry_the_essentials(self):
        out = render("RFQ", rfq_context())
        self.assertEqual(out.subject, "Request for Quotation - IEA-2026-0042")
        for body in (out.text, out.html):
            for needle in ("Mettler Toledo", "A. Keller", "IEA-2026-0042-001", "IEA-2026-0042-002",
                           "Analytical balance, 0.1 mg", "Autoclave 100 L", "Tuttnauer",
                           "10 November 2026", "Gift Minja", "Project Manager"):
                with self.subTest(needle=needle):
                    self.assertIn(needle, body)

    def test_letterhead_is_on_both_bodies_and_address_is_not_invented(self):
        out = render("RFQ", rfq_context())
        self.assertIn("Imperial East Africa Limited", out.html)
        self.assertIn("Imperial East Africa Limited", out.text)
        self.assertNotIn("Tel:", out.html)  # no phone configured, none made up

    @override_settings(DOCUMENT_LETTERHEAD={"address": "Dar es Salaam", "phone": "+255 000"})
    def test_letterhead_details_come_from_settings(self):
        out = render("RFQ", rfq_context())
        self.assertIn("Dar es Salaam", out.html)
        self.assertIn("Tel: +255 000", out.text)

    def test_html_is_self_contained_no_cdn_or_static(self):
        html = render("RFQ", rfq_context()).html
        for forbidden in ("http://", "https://", "<script", "<link", "{% static"):
            self.assertNotIn(forbidden, html)

    def test_user_data_is_html_escaped_but_text_body_is_verbatim(self):
        out = render("RFQ", rfq_context(supplier_name='<script>alert(1)</script> & Co'))
        self.assertNotIn("<script>alert(1)</script>", out.html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt; &amp; Co", out.html)
        self.assertIn("<script>alert(1)</script> & Co", out.text)  # plain text: no HTML entities

    def test_multiline_specification_keeps_its_line_break_in_html(self):
        self.assertIn("0.1 mg<br>Calibration certificate", render("RFQ", rfq_context()).html)

    def test_subject_is_always_one_line(self):
        out = render("RFQ", rfq_context(tender_reference="IEA-2026-0042\r\nBcc: evil@example.com"))
        self.assertNotIn("\n", out.subject)
        self.assertNotIn("\r", out.subject)

    def test_optional_parts_are_omitted_cleanly(self):
        ctx = rfq_context()
        for key in ("supplier_contact", "date", "reply_by", "sender_title"):
            del ctx[key]
        out = render("RFQ", ctx)
        self.assertNotIn("Attention:", out.text)
        self.assertNotIn("We need your quotation by", out.text)

    def test_render_does_not_mutate_the_callers_dict(self):
        ctx = rfq_context()
        before = copy.deepcopy(ctx)
        render("RFQ", ctx)
        self.assertEqual(ctx, before)

    def test_unknown_kind_is_refused(self):
        with self.assertRaises(KeyError):
            render("NOPE", rfq_context())


class ContextContractTests(SimpleTestCase):
    def test_missing_required_keys_are_all_reported(self):
        with self.assertRaises(ValidationError) as caught:
            validate_rfq_context({"items": []})
        messages = " ".join(caught.exception.messages)
        for key in ("supplier_name", "tender_reference", "sender_name", "items"):
            self.assertIn(f"'{key}'", messages)

    def test_item_rows_are_checked(self):
        ctx = rfq_context(items=[{"reference": "X-1", "specification": "", "quantity": 1}])
        with self.assertRaisesMessage(ValidationError, "Item 1: missing 'specification'."):
            validate_rfq_context(ctx)

    def test_only_plain_primitives_are_accepted(self):
        for bad in (Decimal("1.5"), date(2026, 11, 1), {"a"}, object()):
            with self.subTest(bad=type(bad).__name__):
                ctx = rfq_context()
                ctx["items"][0]["quantity"] = bad
                with self.assertRaisesMessage(ValidationError, "plain"):
                    validate_rfq_context(ctx)

    def test_not_a_dict_is_refused(self):
        with self.assertRaises(ValidationError):
            validate_rfq_context(["supplier_name"])
