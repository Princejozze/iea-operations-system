"""
Goods schedule -> rows (CLAUDE.md §6): ``S/N | Description | Unit of measure | Quantity``,
plus the lot and ``GFS Code`` each table sits under.

Facts from the three real tenders that shape this:

* A row that overflows a page comes out of the converter as a HEADERLESS fragment
  (one column, or four) at the top of the next page. It is merged into the row above,
  and only when it really opens the next page directly after a goods table — a table
  elsewhere is never swallowed.
* ``Unit of measure: Lot, Quantity: 1`` is a BUNDLE priced as one line. One row is not
  one physical item; the number of "1. ... 2. ..." sub-items inside the description is
  reported, never split off.
* The same schedule can appear twice in a whole-document scan (the Invitation
  summarises it). Rows with the same lot and S/N are merged, keeping the fuller text.
* Item descriptions are bullet blobs of ``attribute: value`` pairs. Turning those into
  structured attributes (for R9 variance flagging) is later work; the text is kept whole.
"""

import re
from dataclasses import dataclass, field

from .sections import is_goods_header
from .tables import clean, iter_blocks

_LOT = re.compile(r"\bLOT\s*NO\.?\s*:?\s*([A-Z0-9][A-Z0-9/.\-]*[A-Z0-9])(.*)$", re.I)
_GFS = re.compile(r"GFS\s*Code\s*:?\s*(\d{6,10})\s*[-–—]\s*(.+)$", re.I)
_CHROME = re.compile(r"^(_[^_].*_|\d{1,4}|<!--.*-->)$")
_WHOLE_NUMBER = re.compile(r"^\d{1,3}(,\d{3})+$|^\d+$|^\d+\.0+$")


@dataclass
class GoodsRow:
    sn: str
    description: str
    unit_of_measure: str
    quantity_raw: str
    quantity: object  # int, or None when the cell is not a whole number
    pages: list
    lot_number: str = ""
    sub_items: int = 0  # "1. X 2. Y" markers inside one description
    bullets: int = 0
    warnings: list = field(default_factory=list)

    @property
    def is_bundle(self):
        return self.unit_of_measure.strip().lower() == "lot"

    def as_dict(self):
        return {"sn": self.sn, "description": self.description, "unit_of_measure": self.unit_of_measure,
                "quantity": self.quantity, "quantity_raw": self.quantity_raw, "pages": self.pages,
                "lot_number": self.lot_number, "sub_items": self.sub_items, "bullets": self.bullets,
                "warnings": self.warnings}


@dataclass
class GoodsLot:
    number: str = ""
    title: str = ""
    gfs_code: str = ""
    gfs_label: str = ""
    rows: list = field(default_factory=list)

    def as_dict(self):
        return {"number": self.number, "title": self.title, "gfs_code": self.gfs_code,
                "gfs_label": self.gfs_label, "rows": [r.as_dict() for r in self.rows]}


@dataclass
class GoodsSchedule:
    lots: list
    notes: list

    @property
    def rows(self):
        return [r for lot in self.lots for r in lot.rows]

    def as_dict(self):
        return {"lots": [l.as_dict() for l in self.lots], "notes": self.notes}


def _numbered_sub_items(text):
    """Count consecutive '1. X', '2. Y' markers in one description (a Lot row can bundle several items)."""
    found, expect = 0, 1
    for m in re.finditer(r"(?:^|\s)(\d{1,2})\.\s+(?=[A-Za-z])", text):
        if int(m.group(1)) == expect:
            found += 1
            expect += 1
    return found if found >= 2 else 0


def _parse_quantity(raw):
    q = raw.strip()
    if _WHOLE_NUMBER.match(q):
        return int(float(q.replace(",", "")))
    return None


def _finish(row):
    row.description = " ".join(row.description.split())
    row.quantity = _parse_quantity(row.quantity_raw)
    if row.quantity is None:
        row.warnings.append(f"quantity {row.quantity_raw!r} is not a whole number")
    elif row.quantity < 1:
        row.warnings.append("quantity is zero")
        row.quantity = None
    row.sub_items = _numbered_sub_items(row.description)
    row.bullets = row.description.count("•")
    if row.is_bundle:
        row.warnings.append("unit of measure is Lot: a bundle priced as one line, not one item")
    if not row.description:
        row.warnings.append("empty description")
    row.pages = sorted(set(row.pages))
    return row


def parse_goods_schedule(pages, start, end):
    """Parse every goods table on pages ``start..end`` ({page: markdown}) into lots of rows."""
    lots, notes = [], []
    context = {"number": "", "title": "", "gfs_code": "", "gfs_label": ""}
    awaiting_title = False
    current_lot = None
    prev_ended_in_goods = False
    for p in range(start, end + 1):
        text = pages.get(p)
        if text is None:
            prev_ended_in_goods = False
            continue
        blocks = [
            (kind, payload) for kind, payload in iter_blocks(text)
            if kind == "table" or (payload.strip() and not _CHROME.match(payload.strip()))
        ]
        ended_in_goods = False
        for i, (kind, payload) in enumerate(blocks):
            if kind == "line":
                line = clean(payload)
                m = _LOT.search(line)
                g = _GFS.search(line)
                if m:
                    context = {"number": m.group(1).rstrip("."), "title": clean(m.group(2)), "gfs_code": "", "gfs_label": ""}
                    awaiting_title = not context["title"]
                elif g:
                    context["gfs_code"], context["gfs_label"] = g.group(1), g.group(2).strip()
                    awaiting_title = False
                elif awaiting_title and payload.lstrip().startswith("#") and not re.match(r"tender\s+no", line, re.I):
                    context["title"], awaiting_title = line.lstrip("# ").strip(), False
                ended_in_goods = False
                continue
            rows = payload
            if rows and is_goods_header(rows[0]):
                data = rows[1:]
                current_lot = _lot_for(lots, context)
            elif i == 0 and prev_ended_in_goods and current_lot is not None and rows and len(rows[0]) in (1, 4):
                data = rows  # its "header" row is really data (a row spilling over the page break)
                notes.append(f"p.{p}: headerless {len(rows[0])}-column fragment merged into the previous row")
            else:
                ended_in_goods = False
                continue
            _add_rows(current_lot, data, p)
            ended_in_goods = True
        prev_ended_in_goods = ended_in_goods
    for lot in lots:
        for row in lot.rows:
            _finish(row)
    _merge_duplicates(lots, notes)
    return GoodsSchedule([l for l in lots if l.rows], notes)


def _lot_for(lots, context):
    for lot in lots:
        if (lot.number, lot.gfs_code, lot.title) == (context["number"], context["gfs_code"], context["title"]):
            return lot
    lot = GoodsLot(context["number"], context["title"], context["gfs_code"], context["gfs_label"])
    lots.append(lot)
    return lot


def _add_rows(lot, data, page):
    for r in data:
        if len(r) == 1:  # a single-column spill: more description text for the row above
            if lot.rows:
                lot.rows[-1].description += f" {r[0]}"
                lot.rows[-1].pages.append(page)
            continue
        cells = (r + [""] * 4)[:4]
        sn, desc, uom, qty = cells
        if len(r) > 4 and any(r[4:]) and lot.rows:
            lot.rows[-1].warnings.append("extra columns ignored")
        if not sn and lot.rows:  # a wrapped row: blank S/N, the text belongs to the row above
            prev = lot.rows[-1]
            prev.description += f" {desc}"
            prev.unit_of_measure = prev.unit_of_measure or uom
            prev.quantity_raw = prev.quantity_raw or qty
            prev.pages.append(page)
            continue
        lot.rows.append(GoodsRow(sn=sn, description=desc, unit_of_measure=uom, quantity_raw=qty,
                                 quantity=None, pages=[page], lot_number=lot.number))


def _merge_duplicates(lots, notes):
    """The same schedule seen twice (the Invitation summarises it, a whole-document scan reads
    both): S/N is unique within a lot, so keep the fuller copy of each repeated S/N. Lots with the
    same number are first folded together."""
    by_number = {}
    for lot in list(lots):
        if lot.number and lot.number in by_number:
            target = by_number[lot.number]
            target.rows.extend(lot.rows)
            target.gfs_code = target.gfs_code or lot.gfs_code
            target.gfs_label = target.gfs_label or lot.gfs_label
            target.title = target.title or lot.title
            lots.remove(lot)
        elif lot.number:
            by_number[lot.number] = lot
    for lot in lots:
        kept = []
        for row in lot.rows:
            same = next((r for r in kept if r.sn and r.sn == row.sn), None)
            if same is None:
                kept.append(row)
            elif len(row.description) > len(same.description):
                kept[kept.index(same)] = row
                notes.append(f"lot {lot.number} S/N {row.sn}: kept the fuller of two copies")
            else:
                notes.append(f"lot {lot.number} S/N {row.sn}: ignored a shorter second copy")
        lot.rows = kept
