"""
The DocumentConverter interface, pymupdf4llm behind it, page markers, and the
empty-text-layer check (CLAUDE.md §6: "A scanned tender produces nothing").
"""

from django.test import SimpleTestCase

from tenders.pipeline.converter import (
    BLANK_PAGE_CHARS,
    ConversionError,
    ConvertedDocument,
    ConvertedPage,
    DocumentConverter,
    PyMuPDF4LLMConverter,
    check_text_layer,
)
from tenders.pipeline.tables import render_markdown, split_pages

from .pipeline_helpers import (
    TANAPA, blank_pdf, converted, encrypted_pdf, requires_fixtures, scanned_pdf, text_pdf,
)


def doc(*char_counts):
    """A ConvertedDocument whose pages have the given number of text characters."""
    return ConvertedDocument(
        "test", tuple(ConvertedPage(i + 1, "x", "a" * n) for i, n in enumerate(char_counts))
    )


class InterfaceTests(SimpleTestCase):
    def test_the_interface_cannot_be_used_without_a_convert_method(self):
        with self.assertRaises(TypeError):
            DocumentConverter()

        class Lazy(DocumentConverter):
            pass

        with self.assertRaises(TypeError):
            Lazy()

    def test_pymupdf4llm_is_one_implementation_of_it(self):
        self.assertIsInstance(PyMuPDF4LLMConverter(), DocumentConverter)

    def test_the_pinned_version_is_the_one_in_use(self):
        # §4: pin pymupdf4llm==1.28.2 with pymupdf==1.28.2; 2.x is an empty shim.
        self.assertEqual(PyMuPDF4LLMConverter().name, "pymupdf4llm 1.28.2")


class PageMarkerTests(SimpleTestCase):
    def test_markdown_carries_a_marker_before_every_page(self):
        md = render_markdown([(1, "first"), (2, "second"), (10, "tenth")])
        self.assertEqual(md.count("<!-- page "), 3)
        self.assertIn("<!-- page 10 -->\ntenth", md)

    def test_split_pages_is_the_exact_inverse(self):
        pages = [(1, "# Title\n\ntext"), (2, "|a|b|\n|---|---|\n|1|2|\n"), (3, ""), (4, "last page\n")]
        self.assertEqual(split_pages(render_markdown(pages)), dict(pages))

    def test_a_marker_inside_a_page_does_not_break_the_split(self):
        pages = [(1, "text with an inline <!-- page 9 --> comment"), (2, "next")]
        self.assertEqual(split_pages(render_markdown(pages)), dict(pages))


class TextLayerTests(SimpleTestCase):
    def test_a_normal_tender_passes(self):
        report = check_text_layer(doc(*[1000] * 50, 5, 5))  # title and blank pages are normal
        self.assertTrue(report.ok)
        self.assertEqual(report.blank_pages, (51, 52))

    def test_no_text_at_all_is_a_scan(self):
        report = check_text_layer(doc(0, 0, 0, 0))
        self.assertFalse(report.ok)
        self.assertIn("scanned", report.reason)

    def test_near_empty_is_a_scan(self):
        # Page numbers and a stamp survive on a scan: a few characters a page is still no text layer.
        self.assertFalse(check_text_layer(doc(*[6] * 80)).ok)
        self.assertFalse(check_text_layer(doc(*[40] * 40)).ok)  # under the average threshold

    def test_a_mostly_blank_document_is_a_scan(self):
        self.assertFalse(check_text_layer(doc(*([5] * 19), 20000)).ok)  # 19 of 20 blank, one dense page

    def test_just_enough_text_passes(self):
        self.assertTrue(check_text_layer(doc(*[100] * 10)).ok)

    def test_a_pdf_with_no_pages_is_refused(self):
        report = check_text_layer(doc())
        self.assertFalse(report.ok)
        self.assertIn("no pages", report.reason)

    def test_the_blank_page_threshold_counts_only_alphanumerics(self):
        page = ConvertedPage(1, "", " \n.,-- ##" * 50)
        self.assertLess(page.text_chars, BLANK_PAGE_CHARS)


class RealConverterOnSyntheticPdfs(SimpleTestCase):
    """Small PDFs built in the test: fast, and they need no fixture."""

    def test_text_pages_convert_with_a_marker_each_and_a_text_layer(self):
        document = PyMuPDF4LLMConverter().convert(
            text_pdf([f"Tender page {n} about laboratory centrifuges and their warranty terms" * 3 for n in (1, 2, 3)])
        )
        self.assertEqual([p.number for p in document.pages], [1, 2, 3])
        self.assertEqual(document.converter, "pymupdf4llm 1.28.2")
        self.assertEqual(document.markdown.count("<!-- page "), 3)
        self.assertIn("laboratory centrifuges", document.pages[1].markdown)
        self.assertIn("laboratory centrifuges", document.pages[1].raw_text)
        self.assertTrue(check_text_layer(document).ok)

    def test_a_scanned_pdf_has_no_text_layer(self):
        document = PyMuPDF4LLMConverter().convert(scanned_pdf(3))
        self.assertEqual(len(document.pages), 3)
        report = check_text_layer(document)
        self.assertFalse(report.ok, report.as_dict())
        self.assertIn("scanned", report.reason)

    def test_blank_pages_have_no_text_layer(self):
        self.assertFalse(check_text_layer(PyMuPDF4LLMConverter().convert(blank_pdf(4))).ok)

    def test_bytes_that_are_not_a_pdf_raise_conversion_error(self):
        for data in (b"", b"not a pdf at all", b"%PDF-1.4 truncated nonsense"):
            with self.subTest(data=data[:12]), self.assertRaises(ConversionError):
                PyMuPDF4LLMConverter().convert(data)

    def test_a_password_protected_pdf_raises_conversion_error(self):
        with self.assertRaises(ConversionError) as ctx:
            PyMuPDF4LLMConverter().convert(encrypted_pdf())
        self.assertIn("password", str(ctx.exception))


@requires_fixtures(TANAPA)
class RealConverterOnATender(SimpleTestCase):
    def test_a_real_tender_converts_page_for_page_with_markers_and_a_good_text_layer(self):
        document = converted(TANAPA)
        self.assertEqual(len(document.pages), 73)
        self.assertEqual([p.number for p in document.pages], list(range(1, 74)))
        md = document.markdown
        self.assertIn("<!-- page 1 -->", md)
        self.assertIn("<!-- page 73 -->", md)
        report = check_text_layer(document)
        self.assertTrue(report.ok)
        self.assertGreater(report.chars_per_page, 500)

    def test_stored_markdown_splits_back_into_the_same_pages(self):
        document = converted(TANAPA)
        self.assertEqual(split_pages(document.markdown), document.page_markdown())
