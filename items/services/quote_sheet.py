"""
The xlsx round-trip for quote entry at scale (§7 "Entry at scale").

Legacy tenders run to 101 items, where row-by-row entry is punishing. So:

1. ``build_quote_sheet(tender)`` — a workbook pre-filled with the tender's items.
   The item columns are locked; the PM fills the quote columns.
2. ``import_quote_sheet(...)`` — upload it back, together with the supplier's
   original quotation. One sheet = one supplier = one original document: a
   supplier usually sends one quotation covering many items, and that one file
   becomes the evidence behind every quote the sheet creates.

The spreadsheet is a transport format only — never a system of record. Every
row is validated, every failure is reported by row number, and **if any row
fails, nothing is imported**: the PM fixes the sheet and uploads it again, so a
re-upload can never half-duplicate a previous one. A row with no unit price and
nothing else filled in means "this supplier did not quote for this item" and is
skipped.
"""

import datetime
import io
import zipfile
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from tenders.models import Currency

from ..models import Incoterm, Item, assert_item_work_allowed, _is_tenders_pm
from .quotes import build_quote, has_document, save_with_document

SHEET_TITLE = "Quotes"
TENDER_CELL = "B2"
SUPPLIER_CELL = "B3"
HEADER_ROW = 6
FIRST_DATA_ROW = HEADER_ROW + 1
MAX_DATA_ROWS = 1000  # ten times the largest legacy tender; refuses a runaway file

# (key, header, width, filled by the PM?)
COLUMNS = (
    ("item_reference", "Item reference", 20, False),
    ("specification", "Specification required", 60, False),
    ("quantity", "Quantity", 10, False),
    ("unit_of_measure", "Unit of measure", 14, False),
    ("quoted_unit_price", "Unit price", 14, True),
    ("currency", "Currency", 10, True),
    ("incoterm", "Incoterm (blank = CIF)", 12, True),
    ("shipping_applies", "Shipping applies (Y/N)", 12, True),
    ("country_of_origin", "Country of origin", 16, True),
    ("make_brand", "Make / brand", 18, True),
    ("model_part_number", "Model / part number", 18, True),
    ("specification_offered", "Specification offered", 50, True),
    ("lead_time_days", "Lead time (days)", 12, True),
    ("valid_until", "Valid until (YYYY-MM-DD)", 16, True),
    ("notes", "Notes", 30, True),
)
HEADERS = tuple(header for _, header, _, _ in COLUMNS)
FILL_KEYS = tuple(key for key, _, _, filled in COLUMNS if filled)


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


def build_quote_sheet(tender, supplier=None):
    """The tender's items as an .xlsx (bytes), ready for the PM to fill in."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill, Protection
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.datavalidation import DataValidation

    wb = Workbook()
    ws = wb.active
    ws.title = SHEET_TITLE
    ws["A1"] = "Imperial East Africa — supplier quote sheet"
    ws["A1"].font = Font(bold=True, size=13)
    ws["A2"], ws[TENDER_CELL] = "Tender", tender.reference
    ws["A3"], ws[SUPPLIER_CELL] = "Supplier", supplier.name if supplier else ""
    ws["A4"] = (
        "Fill the white columns for each item this supplier quoted. Leave a row blank if they "
        "did not quote for it. Upload this sheet together with the supplier's original quotation."
    )
    ws[SUPPLIER_CELL].protection = Protection(locked=supplier is not None)

    locked_fill = PatternFill("solid", fgColor="E7E6E6")
    for col, (key, header, width, filled) in enumerate(COLUMNS, start=1):
        cell = ws.cell(row=HEADER_ROW, column=col, value=header)
        cell.font = Font(bold=True)
        cell.alignment = Alignment(wrap_text=True, vertical="top")
        ws.column_dimensions[get_column_letter(col)].width = width

    items = list(Item.objects.filter(tender=tender).order_by("item_reference"))
    for offset, item in enumerate(items):
        row = FIRST_DATA_ROW + offset
        values = {
            "item_reference": item.item_reference, "specification": item.specification,
            "quantity": item.quantity, "unit_of_measure": item.unit_of_measure,
        }
        for col, (key, _, _, filled) in enumerate(COLUMNS, start=1):
            cell = ws.cell(row=row, column=col, value=None if filled else values[key])
            if filled:
                cell.protection = Protection(locked=False)
            else:
                cell.fill = locked_fill
                cell.alignment = Alignment(wrap_text=True, vertical="top")

    if items:
        last = FIRST_DATA_ROW + len(items) - 1

        def column_of(key):
            return get_column_letter(next(i for i, c in enumerate(COLUMNS, start=1) if c[0] == key))

        for key, options in (
            ("currency", Currency.values),
            ("incoterm", Incoterm.values),
            ("shipping_applies", ["Y", "N"]),
        ):
            validation = DataValidation(type="list", formula1='"' + ",".join(options) + '"', allow_blank=True)
            ws.add_data_validation(validation)
            validation.add(f"{column_of(key)}{FIRST_DATA_ROW}:{column_of(key)}{last}")

    ws.freeze_panes = ws.cell(row=FIRST_DATA_ROW, column=2)
    # Locks the item columns so a row cannot be re-keyed; the quote columns stay editable.
    ws.protection.sheet = True
    ws.protection.formatColumns = False
    ws.protection.formatRows = False

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RowError:
    row: int  # the spreadsheet row number the PM sees; 0 for a whole-sheet problem
    item_reference: str
    message: str

    def __str__(self):
        where = f"Row {self.row}" if self.row else "Sheet"
        ref = f" ({self.item_reference})" if self.item_reference else ""
        return f"{where}{ref}: {self.message}"


@dataclass
class SheetImportResult:
    quotes: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    skipped: int = 0

    @property
    def ok(self):
        return not self.errors


class _CellError(ValueError):
    pass


def _text(value):
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return " ".join(str(value).split()) if not isinstance(value, str) else value.strip()


def _price(value):
    if isinstance(value, bool):
        raise _CellError("Unit price must be a number.")
    try:
        price = Decimal(str(value).replace(",", "").strip()) if not isinstance(value, Decimal) else value
    except InvalidOperation:
        raise _CellError(f"Unit price {value!r} is not a number.") from None
    if not price.is_finite():
        raise _CellError("Unit price must be a number.")
    if price < 0:
        raise _CellError("Unit price cannot be negative.")
    if price.as_tuple().exponent < -4:
        raise _CellError("Unit price has more than 4 decimal places.")
    return price


def _choice(value, options, label, default=None):
    text = _text(value).upper()
    if not text and default is not None:
        return default
    if text not in options:
        raise _CellError(f"{label} must be one of {', '.join(options)} (got {_text(value) or 'blank'}).")
    return text


def _yes_no(value):
    text = _text(value).upper()
    if text in ("Y", "YES", "TRUE", "1"):
        return True
    if text in ("N", "NO", "FALSE", "0"):
        return False
    raise _CellError("Shipping applies must be Y (imported) or N (sourced locally).")


def _lead_time(value):
    if value in (None, ""):
        return None
    try:
        number = Decimal(_text(value))
    except InvalidOperation:
        raise _CellError(f"Lead time {_text(value)!r} is not a whole number of days.") from None
    if number != number.to_integral_value() or number < 0:
        raise _CellError("Lead time must be a whole number of days, zero or more.")
    return int(number)


def _date(value):
    if value in (None, ""):
        return None
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    try:
        return datetime.date.fromisoformat(_text(value))
    except ValueError:
        raise _CellError(f"Valid until {_text(value)!r} is not a date (use YYYY-MM-DD).") from None


def _parse_row(cells):
    """Cells (key -> raw value) to build_quote() keyword arguments."""
    problems, figures = [], {}
    parsers = {
        "quoted_unit_price": _price,
        "currency": lambda v: _choice(v, Currency.values, "Currency"),
        "incoterm": lambda v: _choice(v, Incoterm.values, "Incoterm", default=Incoterm.CIF),
        "shipping_applies": _yes_no,
        "lead_time_days": _lead_time,
        "valid_until": _date,
    }
    for key in FILL_KEYS:
        try:
            figures[key] = parsers.get(key, _text)(cells.get(key))
        except _CellError as exc:
            problems.append(str(exc))
    return figures, problems


def _flatten(exc):
    if hasattr(exc, "error_dict"):
        return [f"{name.replace('_', ' ')}: {m}" for name, msgs in exc.message_dict.items() for m in msgs]
    return list(exc.messages)


def _open_sheet(sheet):
    from openpyxl import load_workbook
    from openpyxl.utils.exceptions import InvalidFileException

    try:
        if hasattr(sheet, "seek"):
            sheet.seek(0)
        workbook = load_workbook(sheet, read_only=True, data_only=True)
    except (InvalidFileException, zipfile.BadZipFile, KeyError, OSError, ValueError) as exc:
        raise ValidationError(f"That file is not a readable .xlsx workbook ({exc.__class__.__name__}).") from None
    if SHEET_TITLE not in workbook.sheetnames:
        raise ValidationError(f"The workbook has no '{SHEET_TITLE}' sheet. Download a fresh quote sheet.")
    return workbook[SHEET_TITLE]


def import_quote_sheet(tender, *, supplier, sheet, original_document, user):
    """
    Read a filled quote sheet for ``tender`` and create one RECEIVED quote from
    ``supplier`` per filled row, each backed by ``original_document`` (the
    supplier's quotation as sent). Returns a SheetImportResult: ``ok`` with the
    new quotes, or the row errors with nothing written.

    Raises PermissionDenied if ``user`` is not the tender's PM, and the tender's
    TransitionError if item work is not allowed (R15, R9a). Quotes are not
    selected here — choosing which quote an item is costed on stays the PM's call.
    """
    if not _is_tenders_pm(user, tender):
        raise PermissionDenied("Only this tender's PM imports its quotes.")
    assert_item_work_allowed(tender, "Importing quotes")
    result = SheetImportResult()

    def fail(row, ref, message):
        result.errors.append(RowError(row, ref, message))

    if not has_document(original_document):
        fail(0, "", "Attach the supplier's original quotation: every quote needs its evidence.")
    if supplier is None:
        fail(0, "", "Choose the supplier this sheet is from.")
    try:
        ws = _open_sheet(sheet)
    except ValidationError as exc:
        fail(0, "", exc.messages[0])
        return result
    if result.errors:
        return result

    if _text(ws[TENDER_CELL].value) != tender.reference:
        fail(0, "", f"This sheet is for {_text(ws[TENDER_CELL].value) or 'no tender'}, not {tender.reference}.")
    sheet_supplier = _text(ws[SUPPLIER_CELL].value)
    if sheet_supplier and sheet_supplier.casefold() != supplier.name.casefold():
        fail(0, "", f"This sheet names supplier {sheet_supplier!r}, but you chose {supplier.name!r}.")
    header = tuple(_text(c) for c in next(ws.iter_rows(min_row=HEADER_ROW, max_row=HEADER_ROW, values_only=True), ()))
    if header[: len(HEADERS)] != HEADERS:
        fail(0, "", "The column headings have been changed. Download a fresh quote sheet.")
    if result.errors:
        return result

    items = {item.item_reference: item for item in Item.objects.filter(tender=tender).select_related("tender")}
    keys = [key for key, *_ in COLUMNS]
    pending, seen = [], set()
    for row_number, values in enumerate(ws.iter_rows(min_row=FIRST_DATA_ROW, values_only=True), start=FIRST_DATA_ROW):
        if row_number - FIRST_DATA_ROW >= MAX_DATA_ROWS:
            fail(row_number, "", f"The sheet has more than {MAX_DATA_ROWS} rows.")
            break
        cells = dict(zip(keys, values))
        ref = _text(cells.get("item_reference"))
        filled = any(_text(cells.get(key)) for key in FILL_KEYS)
        if not filled:
            if ref:
                result.skipped += 1
            continue
        if not ref:
            fail(row_number, "", "A filled row has no item reference.")
            continue
        if ref in seen:
            fail(row_number, ref, "This item appears more than once in the sheet.")
            continue
        seen.add(ref)
        item = items.get(ref)
        if item is None:
            fail(row_number, ref, f"No item {ref} on tender {tender.reference}.")
            continue
        if _text(cells.get("quoted_unit_price")) == "":
            fail(row_number, ref, "The row is filled in but has no unit price.")
            continue
        figures, problems = _parse_row(cells)
        if not problems:
            try:
                pending.append((row_number, ref, build_quote(item, supplier=supplier, user=user, **figures)))
            except ValidationError as exc:
                problems = _flatten(exc)
        for problem in problems:
            fail(row_number, ref, problem)

    if result.errors:
        return result
    if not pending:
        fail(0, "", "No row has a unit price, so there is nothing to import.")
        return result

    stored_name, filename = None, getattr(original_document, "name", "") or "quotation"
    current = (0, "")
    try:
        with transaction.atomic():
            for row_number, ref, quote in pending:
                current = (row_number, ref)
                save_with_document(quote, original_document, user=user, stored_name=stored_name,
                                   original_filename=filename)
                stored_name = stored_name or quote.original_document.file.name  # stored once, shared
                result.quotes.append(quote)
    except ValidationError as exc:
        result.quotes = []
        for message in _flatten(exc):
            fail(*current, message)
    return result
