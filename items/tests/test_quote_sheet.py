"""
The xlsx round-trip (§7 "Entry at scale"): download pre-filled, fill prices, upload back.
Every row validated, every failure reported, nothing imported if anything fails.
"""

import datetime
import io

from django.core.exceptions import PermissionDenied
from django.core.files.base import ContentFile
from django.test import TestCase
from openpyxl import load_workbook

from items.models import AttachmentKind, ItemAttachment, Quote, QuoteStatus as QS
from items.services.quote_sheet import (
    COLUMNS, FIRST_DATA_ROW, HEADER_ROW, HEADERS, SHEET_TITLE, SUPPLIER_CELL, TENDER_CELL,
    build_quote_sheet, import_quote_sheet,
)
from tenders.models import TenderStatus as S, TransitionError

from .helpers import D, ItemFixtures

COL = {key: n for n, (key, *_) in enumerate(COLUMNS, start=1)}


def evidence():
    return ContentFile(b"%PDF-1.4 one supplier quotation covering many items", name="eppendorf-q-118.pdf")


class QuoteSheetFixtures(ItemFixtures):
    def setUp(self):
        super().setUp()
        self.tender = self.costing()
        self.items = [self.make_item(self.tender, specification=f"Item {n}", quantity=n) for n in range(1, 4)]

    def workbook(self, tender=None, supplier=None):
        return load_workbook(io.BytesIO(build_quote_sheet(tender or self.tender, supplier)))

    def fill(self, wb, row_offset, **values):
        ws = wb[SHEET_TITLE]
        for key, value in values.items():
            ws.cell(row=FIRST_DATA_ROW + row_offset, column=COL[key], value=value)

    def upload(self, wb, **kwargs):
        out = io.BytesIO()
        wb.save(out)
        out.seek(0)
        kwargs.setdefault("supplier", self.supplier)
        kwargs.setdefault("original_document", evidence())
        kwargs.setdefault("user", self.pm)
        return import_quote_sheet(self.tender, sheet=out, **kwargs)


class DownloadTests(QuoteSheetFixtures, TestCase):
    def test_the_sheet_is_prefilled_with_the_tenders_items(self):
        ws = self.workbook(supplier=self.supplier)[SHEET_TITLE]
        self.assertEqual(ws[TENDER_CELL].value, self.tender.reference)
        self.assertEqual(ws[SUPPLIER_CELL].value, self.supplier.name)
        self.assertEqual(tuple(c.value for c in ws[HEADER_ROW])[: len(HEADERS)], HEADERS)
        rows = [
            (ws.cell(row=r, column=COL["item_reference"]).value, ws.cell(row=r, column=COL["quantity"]).value)
            for r in range(FIRST_DATA_ROW, FIRST_DATA_ROW + 3)
        ]
        self.assertEqual(rows, [(i.item_reference, i.quantity) for i in self.items])

    def test_item_columns_are_locked_and_quote_columns_are_not(self):
        ws = self.workbook()[SHEET_TITLE]
        self.assertTrue(ws.protection.sheet)
        self.assertTrue(ws.cell(row=FIRST_DATA_ROW, column=COL["item_reference"]).protection.locked)
        self.assertFalse(ws.cell(row=FIRST_DATA_ROW, column=COL["quoted_unit_price"]).protection.locked)

    def test_a_101_item_tender_downloads_and_round_trips(self):
        tender = self.costing()
        for n in range(101):
            self.make_item(tender, specification=f"Line {n}", quantity=1)
        self.tender = tender
        wb = self.workbook()
        for offset in range(101):
            self.fill(wb, offset, quoted_unit_price=10 + offset, currency="USD", shipping_applies="Y")
        result = self.upload(wb)
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(len(result.quotes), 101)


class UploadTests(QuoteSheetFixtures, TestCase):
    def test_filled_rows_become_received_quotes_backed_by_the_one_document(self):
        wb = self.workbook()
        self.fill(wb, 0, quoted_unit_price=1500.5, currency="usd", incoterm="fob", shipping_applies="Y",
                  country_of_origin="Germany", make_brand="Eppendorf", model_part_number="5424 R",
                  specification_offered="24 x 1.5 ml", lead_time_days=45, valid_until=datetime.date(2026, 12, 31))
        self.fill(wb, 2, quoted_unit_price="2,000", currency="TZS", shipping_applies="n", valid_until="2026-12-15")
        result = self.upload(wb)

        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.skipped, 1)  # the middle item was not quoted
        first, third = sorted(result.quotes, key=lambda q: q.item.item_reference)
        self.assertEqual(
            (first.item, first.quoted_unit_price, first.currency, first.incoterm, first.shipping_applies,
             first.lead_time_days, first.valid_until, first.specification_offered, first.status),
            (self.items[0], D("1500.5"), "USD", "FOB", True, 45, datetime.date(2026, 12, 31), "24 x 1.5 ml", QS.RECEIVED),
        )
        self.assertEqual((third.quoted_unit_price, third.incoterm, third.shipping_applies), (D("2000"), "CIF", False))
        # One stored file, two QUOTATION attachments pointing at it.
        documents = ItemAttachment.objects.filter(kind=AttachmentKind.QUOTATION)
        self.assertEqual(documents.count(), 2)
        self.assertEqual(len({d.file.name for d in documents}), 1)
        self.assertEqual(set(documents.values_list("original_filename", flat=True)), {"eppendorf-q-118.pdf"})

    def test_quotes_are_not_selected_by_the_import(self):
        wb = self.workbook()
        self.fill(wb, 0, quoted_unit_price=10, currency="USD", shipping_applies="Y")
        self.assertTrue(self.upload(wb).ok)
        self.items[0].refresh_from_db()
        self.assertIsNone(self.items[0].selected_quote)

    def test_every_bad_row_is_reported_and_nothing_is_imported(self):
        wb = self.workbook()
        self.fill(wb, 0, quoted_unit_price=10, currency="USD", shipping_applies="Y")  # good
        self.fill(wb, 1, quoted_unit_price=-5, currency="YEN", shipping_applies="maybe", lead_time_days="soon")
        self.fill(wb, 2, currency="USD")  # filled but no price
        result = self.upload(wb)

        self.assertFalse(result.ok)
        self.assertFalse(Quote.objects.exists())
        self.assertFalse(ItemAttachment.objects.exists())
        by_row = {}
        for error in result.errors:
            by_row.setdefault(error.row, []).append(error.message)
        self.assertEqual(set(by_row), {FIRST_DATA_ROW + 1, FIRST_DATA_ROW + 2})
        self.assertEqual(len(by_row[FIRST_DATA_ROW + 1]), 4)  # price, currency, shipping, lead time
        self.assertIn("no unit price", by_row[FIRST_DATA_ROW + 2][0])
        self.assertIn(self.items[1].item_reference, str(result.errors[0]))

    def test_unknown_and_repeated_item_references_are_reported(self):
        wb = self.workbook()
        ws = wb[SHEET_TITLE]
        ws.protection.sheet = False
        self.fill(wb, 0, quoted_unit_price=10, currency="USD", shipping_applies="Y")
        self.fill(wb, 1, item_reference=self.items[0].item_reference, quoted_unit_price=11, currency="USD",
                  shipping_applies="Y")
        self.fill(wb, 2, item_reference="IEA-2026-9999-001", quoted_unit_price=12, currency="USD", shipping_applies="Y")
        messages = [e.message for e in self.upload(wb).errors]
        self.assertTrue(any("more than once" in m for m in messages))
        self.assertTrue(any("No item IEA-2026-9999-001" in m for m in messages))

    def test_a_sheet_for_another_tender_is_refused(self):
        other = self.costing()
        self.make_item(other)
        wb = self.workbook(tender=other)
        self.fill(wb, 0, quoted_unit_price=10, currency="USD", shipping_applies="Y")
        result = self.upload(wb)
        self.assertFalse(result.ok)
        self.assertIn(other.reference, result.errors[0].message)

    def test_a_sheet_named_for_another_supplier_is_refused(self):
        from suppliers.models import Supplier

        wb = self.workbook(supplier=Supplier.objects.create(name="Hettich"))
        self.fill(wb, 0, quoted_unit_price=10, currency="USD", shipping_applies="Y")
        self.assertIn("Hettich", self.upload(wb).errors[0].message)

    def test_changed_headings_are_refused(self):
        wb = self.workbook()
        wb[SHEET_TITLE].cell(row=HEADER_ROW, column=COL["currency"], value="Ccy")
        self.assertIn("headings", self.upload(wb).errors[0].message)

    def test_no_document_no_import(self):
        wb = self.workbook()
        self.fill(wb, 0, quoted_unit_price=10, currency="USD", shipping_applies="Y")
        for document in (None, ContentFile(b"", name="empty.pdf"), ContentFile(b"%PDF", name="")):
            result = self.upload(wb, original_document=document)
            self.assertFalse(result.ok)
            self.assertIn("original quotation", result.errors[0].message)
        self.assertFalse(Quote.objects.exists())

    def test_a_non_xlsx_file_is_reported_not_raised(self):
        result = import_quote_sheet(
            self.tender, supplier=self.supplier, sheet=io.BytesIO(b"not a workbook"),
            original_document=evidence(), user=self.pm,
        )
        self.assertFalse(result.ok)
        self.assertIn("not a readable .xlsx", result.errors[0].message)

    def test_an_empty_sheet_imports_nothing_and_says_so(self):
        result = self.upload(self.workbook())
        self.assertFalse(result.ok)
        self.assertIn("nothing to import", result.errors[0].message)

    def test_only_this_tenders_pm_imports(self):
        wb = self.workbook()
        with self.assertRaises(PermissionDenied):
            self.upload(wb, user=self.pm2)

    def test_not_while_frozen_or_before_registration(self):
        wb = self.workbook()
        for status, extra in ((S.PENDING_ELIGIBILITY_DECISION, {"previous_status": S.COSTING}),
                              (S.PENDING_REGISTRATION_APPROVAL, {"previous_status": ""})):
            from .helpers import force_status

            force_status(self.tender, status, **extra)
            with self.subTest(status=status), self.assertRaises(TransitionError):
                self.upload(wb)
