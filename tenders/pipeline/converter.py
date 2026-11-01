"""
PDF -> Markdown (CLAUDE.md §6). The rest of the pipeline sees only the
``DocumentConverter`` interface, so pymupdf4llm (AGPL via PyMuPDF) can be swapped
for docling (MIT) without touching anything else.

A converter returns one ``ConvertedPage`` per PDF page carrying BOTH its Markdown
and its raw text layer. The raw text serves two purposes the Markdown cannot:
detecting a scanned tender (§6: "a scanned tender produces nothing") and standing
in for a page whose tables shredded on conversion.
"""

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass

from .tables import render_markdown

# A page with fewer alphanumeric characters than this has, in practice, no body text
# (a blank page, a stamp, a page number). Real pages in the test tenders average ~1,000.
BLANK_PAGE_CHARS = 25
# Below this average per page the document is treated as having no usable text layer.
MIN_AVG_CHARS_PER_PAGE = 80
# ...or when this share of its pages is blank.
MAX_BLANK_FRACTION = 0.9


class ConversionError(Exception):
    """The file could not be read as a PDF at all (corrupt, encrypted, not a PDF)."""


@dataclass(frozen=True)
class ConvertedPage:
    number: int  # 1-based, the PDF's own page index
    markdown: str
    raw_text: str  # the PDF text layer as PyMuPDF reads it, no table structure

    @property
    def text_chars(self):
        return len(re.sub(r"[^A-Za-z0-9]", "", self.raw_text))


@dataclass(frozen=True)
class ConvertedDocument:
    converter: str  # e.g. "pymupdf4llm 1.28.2"
    pages: tuple

    @property
    def markdown(self):
        return render_markdown([(p.number, p.markdown) for p in self.pages])

    def page_markdown(self):
        return {p.number: p.markdown for p in self.pages}

    def page_raw_text(self):
        return {p.number: p.raw_text for p in self.pages}


@dataclass(frozen=True)
class TextLayerReport:
    ok: bool
    pages: int
    chars_per_page: float
    blank_pages: tuple
    reason: str = ""

    def as_dict(self):
        return {"ok": self.ok, "pages": self.pages, "chars_per_page": round(self.chars_per_page, 1),
                "blank_pages": list(self.blank_pages), "reason": self.reason}


def check_text_layer(document):
    """Is there enough real text to extract from? (§6: never let a scan silently extract to blanks.)"""
    n = len(document.pages)
    if n == 0:
        return TextLayerReport(False, 0, 0.0, (), "The PDF has no pages.")
    blank = tuple(p.number for p in document.pages if p.text_chars < BLANK_PAGE_CHARS)
    per_page = sum(p.text_chars for p in document.pages) / n
    if per_page < MIN_AVG_CHARS_PER_PAGE or len(blank) / n >= MAX_BLANK_FRACTION:
        return TextLayerReport(
            False, n, per_page, blank,
            f"Only {per_page:.0f} characters of text per page and {len(blank)} of {n} pages are blank: "
            "this looks like a scanned document with no text layer.",
        )
    return TextLayerReport(True, n, per_page, blank)


class DocumentConverter(ABC):
    """Anything that turns PDF bytes into per-page Markdown plus the raw text layer."""

    name = "abstract"

    @abstractmethod
    def convert(self, pdf_bytes):
        """Return a ``ConvertedDocument``. Raise ``ConversionError`` if the bytes are not a readable PDF."""


class PyMuPDF4LLMConverter(DocumentConverter):
    """pymupdf4llm. Pinned in requirements.txt: pymupdf4llm==1.28.2 with pymupdf==1.28.2 (§4) —
    2.x is an empty shim that fails on import. Conversion takes ~0.4 s per page (§6), so call
    this from the cron-driven command, never from a request."""

    @property
    def name(self):
        import pymupdf4llm

        return f"pymupdf4llm {getattr(pymupdf4llm, '__version__', '?')}"

    def convert(self, pdf_bytes):
        import pymupdf
        import pymupdf4llm

        try:
            doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
        except Exception as exc:  # pymupdf raises several types for bad input
            raise ConversionError(f"This file could not be opened as a PDF: {exc}") from exc
        try:
            if doc.needs_pass:
                raise ConversionError("This PDF is password-protected.")
            chunks = pymupdf4llm.to_markdown(doc, page_chunks=True, show_progress=False)
            raw = {page.number + 1: page.get_text() for page in doc}
        except ConversionError:
            raise
        except Exception as exc:
            raise ConversionError(f"This PDF could not be converted: {exc}") from exc
        finally:
            doc.close()
        pages = tuple(
            ConvertedPage(
                number=c["metadata"]["page_number"],
                markdown=c["text"],
                raw_text=raw.get(c["metadata"]["page_number"], ""),
            )
            for c in chunks
        )
        return ConvertedDocument(converter=self.name, pages=pages)
