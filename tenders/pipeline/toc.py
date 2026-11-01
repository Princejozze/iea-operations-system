"""
Table-of-contents parsing (CLAUDE.md §6). PPRA tenders carry a TOC with page
numbers; in the three real tenders the page numbers were correct every time, even
where the names did not match the body headings. The TOC is therefore the source
of section PAGE RANGES. It says nothing reliable about section hierarchy (it is a
flat two-column table), so the level is inferred: ALL CAPS is a top-level section.
"""

import re
from dataclasses import dataclass

from .tables import clean

TOC_ROW = re.compile(r"^\|\s*(.+?)\s*\|\s*(\d{1,4})\s*\|\s*$")
# Lines that are page furniture rather than content: running header, bare page number, markers.
_CHROME = re.compile(r"^(_[^_].*_|\d{1,4}|<!--.*-->|\|\s*-+\s*\|\s*-+\s*\|)$")
# The TOC is on the first pages; do not go looking for the phrase deep in the document.
_SEARCH_PAGES = 10


@dataclass(frozen=True)
class TocEntry:
    name: str
    page: int
    level: str  # "top" (ALL CAPS) or "sub"
    end: int  # last page before the next entry that starts later
    end_incl_children: int  # last page before the next TOP-level entry

    def as_dict(self):
        return {"name": self.name, "page": self.page, "level": self.level,
                "end": self.end, "end_incl_children": self.end_incl_children}


@dataclass(frozen=True)
class Toc:
    entries: tuple
    pages: tuple  # PDF pages the TOC itself occupies

    def __bool__(self):
        return bool(self.entries)

    def as_dict(self):
        return {"pages": list(self.pages), "entries": [e.as_dict() for e in self.entries]}

    def hint(self):
        """The TOC as plain text, for the whole-document fallback (§6: send the document with the TOC as a hint)."""
        return "\n".join(f"{e.name} .... p.{e.page}" for e in self.entries)


def _is_toc_page(text):
    lines = [l.strip() for l in text.splitlines() if l.strip() and not _CHROME.match(l.strip())]
    rows = [l for l in lines if TOC_ROW.match(l)]
    return len(rows) >= 3 and len(rows) >= 0.6 * len(lines)


def parse_toc(pages):
    """``pages`` is {page_number: markdown}. Returns a ``Toc`` (empty if none is found)."""
    numbers = sorted(pages)
    if not numbers:
        return Toc((), ())
    start = next(
        (n for n in numbers[:_SEARCH_PAGES] if re.search(r"table\s+of\s+contents", pages[n], re.I)), None
    )
    if start is None:
        return Toc((), ())
    toc_pages, raw = [], []
    for n in numbers:
        if n < start:
            continue
        if n != start and not _is_toc_page(pages[n]):
            break
        toc_pages.append(n)
        for line in pages[n].splitlines():
            m = TOC_ROW.match(line.strip())
            if m and not set(m.group(1)) <= set("-: "):
                name = clean(m.group(1))
                letters = re.sub(r"[^A-Za-z]", "", name)
                raw.append((name, int(m.group(2)), "top" if letters.isupper() else "sub"))
    last_page = numbers[-1]
    entries = []
    for i, (name, page, level) in enumerate(raw):
        later = [p for _, p, _ in raw[i + 1:] if p > page]
        later_tops = [p for _, p, lv in raw[i + 1:] if lv == "top" and p > page]
        entries.append(TocEntry(
            name=name, page=page, level=level,
            end=(later[0] - 1) if later else last_page,
            end_incl_children=(later_tops[0] - 1) if later_tops else last_page,
        ))
    # A TOC row with a page beyond the document is not a TOC row (a table of dates, say).
    entries = [e for e in entries if 1 <= e.page <= last_page]
    return Toc(tuple(entries), tuple(toc_pages))
