"""
Shared test support for the document pipeline.

The three real tender PDFs live in ``tenders/tests/fixtures/pdfs/`` and are NOT in the
repository (they are large third-party documents; .gitignore excludes them). Tests that
need them skip cleanly when they are absent.

Converting a 173-page tender takes about a minute (§6: ~0.4 s/page), so the real converter
is wrapped in ``CachedConverter``: the first run on a machine converts for real and keeps
the result in the system temp directory; later runs read it back. The cache is keyed on the
SHA-1 of the PDF bytes and the converter's name+version, so a changed PDF or a bumped
pymupdf4llm is never served stale.
"""

import hashlib
import json
import tempfile
from functools import lru_cache
from pathlib import Path
from unittest import skipUnless

import pymupdf
from django.conf import settings

from tenders.pipeline.converter import (
    ConvertedDocument,
    ConvertedPage,
    DocumentConverter,
    PyMuPDF4LLMConverter,
)

FIXTURES = Path(settings.BASE_DIR) / "tenders" / "tests" / "fixtures" / "pdfs"
GASCO = "gasco_general_TR24_001_2026_2027_G_133.pdf"
SAMPLE = "sample_general_goods_173pp.pdf"
TANAPA = "tanapa_minicomp_TR219_2026_2027_G_38.pdf"
CACHE_DIR = Path(tempfile.gettempdir()) / "iea_pipeline_test_cache"


def requires_fixtures(*names):
    missing = [n for n in names if not (FIXTURES / n).exists()]
    return skipUnless(not missing, f"tender PDF fixtures not present: {', '.join(missing)}")


def fixture_bytes(name):
    return (FIXTURES / name).read_bytes()


class CachedConverter(DocumentConverter):
    """The real pymupdf4llm converter with an on-disk cache of its output."""

    def __init__(self):
        self._real = PyMuPDF4LLMConverter()
        self.calls = 0  # real conversions performed (cache misses)

    @property
    def name(self):
        return self._real.name

    def convert(self, pdf_bytes):
        key = hashlib.sha1(pdf_bytes + self.name.encode()).hexdigest()
        path = CACHE_DIR / f"{key}.json"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            pages = tuple(ConvertedPage(n, md, raw) for n, md, raw in data["pages"])
            return ConvertedDocument(data["converter"], pages)
        self.calls += 1
        document = self._real.convert(pdf_bytes)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"converter": document.converter,
                        "pages": [[p.number, p.markdown, p.raw_text] for p in document.pages]}),
            encoding="utf-8",
        )
        return document


_CONVERTER = CachedConverter()


@lru_cache(maxsize=None)
def converted(name):
    """The ConvertedDocument for a fixture PDF (converted once per machine, memoised per process)."""
    return _CONVERTER.convert(fixture_bytes(name))


def fixture_pages(name):
    return converted(name).page_markdown()


class StaticConverter(DocumentConverter):
    """A test double returning pages it was given, and counting how often it was asked."""

    name = "static test converter 1"

    def __init__(self, pages, raw=None):
        self.pages = pages  # {number: markdown}
        self.raw = raw or {}
        self.calls = 0

    def convert(self, pdf_bytes):
        self.calls += 1
        return ConvertedDocument(
            self.name,
            tuple(ConvertedPage(n, md, self.raw.get(n, md)) for n, md in sorted(self.pages.items())),
        )


class FailingConverter(DocumentConverter):
    name = "failing test converter"

    def __init__(self, exc):
        self.exc = exc

    def convert(self, pdf_bytes):
        raise self.exc


# --------------------------------------------------------------------------- #
# synthetic PDFs (small, fast, built with PyMuPDF itself)
# --------------------------------------------------------------------------- #


def text_pdf(pages):
    """A real PDF whose pages carry the given text (a genuine text layer)."""
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        page.insert_text((72, 100), text, fontsize=11)
    data = doc.tobytes()
    doc.close()
    return data


def scanned_pdf(n_pages=3, text="Tender for the supply of laboratory equipment"):
    """A PDF whose pages are pictures of text and have NO text layer — what a scan is."""
    source = pymupdf.open()
    out = pymupdf.open()
    for _ in range(n_pages):
        page = source.new_page()
        page.insert_text((72, 100), text, fontsize=18)
        pix = page.get_pixmap(dpi=72)
        new = out.new_page(width=page.rect.width, height=page.rect.height)
        new.insert_image(new.rect, pixmap=pix)
    data = out.tobytes()
    source.close()
    out.close()
    return data


def blank_pdf(n_pages=3):
    doc = pymupdf.open()
    for _ in range(n_pages):
        doc.new_page()
    data = doc.tobytes()
    doc.close()
    return data


def encrypted_pdf():
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 100), "secret tender", fontsize=11)
    data = doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="user", owner_pw="owner")
    doc.close()
    return data


# --------------------------------------------------------------------------- #
# a small synthetic tender, as converted pages: fast, and every part is controllable
# --------------------------------------------------------------------------- #

MINI_HEADER = "|**S/N**|**Description**|**Unit of**<br>**measure**|**Quantity**|\n|---|---|---|---|\n"


def mini_tender_pages(goods_rows="|1|Laboratory centrifuge|No.|2|\n|2|Rotor, 24 x 1.5 ml|No.|2|\n"):
    """Twelve pages in the shape of a mini-competition tender: TOC, Invitation, criteria,
    schedule of requirements, specifications and forms, each confirmable by content."""
    return {
        1: "cover page " * 30,
        2: "# **Table of contents**\n\n|SECTION I: INVITATION|3|\n|---|---|\n|QUALIFICATION AND EVALUATION CRITERIA|5|\n"
           "|SCHEDULE OF REQUIREMENTS|7|\n|Specifications|9|\n|TENDERING FORMS|10|\n",
        3: "## **SECTION I: INVITATION FOR QUOTATIONS**\n",
        4: "MINI-COMPETITION QUOTATION\n\n## **Quotation No. TR9/2026/2027/G/1**\n\n"
           "6. The mini competition quotation submission deadline will be on 2:00 PM at 23/10/2026.\n",
        5: "## **SECTION III - QUALIFICATION AND EVALUATION CRITERIA**\n",
        6: "- **Commercial Evaluation**\n\n- **Tender Validity Period (SCORE: Comply/Not Comply to specified minimum requirements)**\n\n"
           "- **Specific Experience (SCORE: Comply/Not Comply to specified minimum requirements)**\n",
        7: "# **SECTION V: SCHEDULE OF REQUIREMENTS**\n",
        8: "## **1. List of Goods and Related Services**\n\n## **LOT NO. TR9/2026/2027/G/1**\n\n## **Supply of Laboratory Equipment**\n\n"
           "## **GFS Code: 31122139 - Scientific Equipment**\n\n" + MINI_HEADER + goods_rows,
        9: "|**Technical Requirements**|**Description**|\n|---|---|\n|Power|230 V, 50 Hz|\n|Speed|up to 15,000 rpm|\n",
        10: "## **SECTION VI: TENDERING FORMS**\n",
        11: "## **1. QUOTATION SUBMISSION FORM**\n\nWe agree to supply the goods.\n",
        12: "## **STANDARD POWER OF ATTORNEY**\n\nTO ALL IT MAY CONCERN\n",
    }
