"""
Small text helpers shared by the pipeline: Markdown table blocks, heading
normalisation, and the "did this table shred on conversion?" check.

Everything here is pure string processing — no Django, no I/O.
"""

import re

PAGE_MARKER = "<!-- page {n} -->"
# [ \t]* not \s*: never eat the page's own leading newlines.
_PAGE_MARKER_RE = re.compile(r"^<!-- page (\d+) -->[ \t]*$", re.MULTILINE)


def render_markdown(pages):
    """[(page_number, markdown)] -> one Markdown string with a page marker before each page.

    This is the format stored on ``TenderDocument.markdown`` (§6: page markers retained)
    and ``split_pages`` reverses it exactly.
    """
    return "".join(f"\n{PAGE_MARKER.format(n=n)}\n{text}" for n, text in pages)


def split_pages(markdown):
    """Inverse of ``render_markdown``: {page_number: markdown}."""
    matches = list(_PAGE_MARKER_RE.finditer(markdown))
    pages = {}
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(markdown)
        # render_markdown writes "\n<marker>\n<text>" for each page: strip the newline that follows
        # the marker and, except on the last page, the one the NEXT marker brought with it.
        body = markdown[m.end():end]
        body = body[1:] if body.startswith("\n") else body
        if i + 1 < len(matches) and body.endswith("\n"):
            body = body[:-1]
        pages[int(m.group(1))] = body
    return pages


def clean(s):
    """One line: drop bold markers and <br>, collapse whitespace."""
    return " ".join(re.sub(r"\*+", "", s.replace("<br>", " ")).split())


def norm(s):
    """Lower-case a heading and drop 'SECTION IV:' / 'PART 2:' / '3.' prefixes and punctuation."""
    s = clean(s).replace("’", "'").lower()
    s = re.sub(r"^(section|part)\s+[ivxlc\d]+\s*[:.\-–—]\s*", "", s)
    s = re.sub(r"^\d+\.\s*", "", s)
    s = re.sub(r"[^a-z0-9' ]+", " ", s)
    return " ".join(s.split())


def key(s):
    """Letters only, lower-case: for comparing table header cells."""
    return re.sub(r"[^a-z]", "", s.lower())


def iter_blocks(text):
    """Yield ("line", str) and ("table", rows) in document order. Rows are lists of cleaned cells;
    the Markdown separator row is dropped, so rows[0] is the table's header row."""
    block = []
    for line in text.splitlines() + [""]:
        if line.strip().startswith("|"):
            block.append(line.strip())
            continue
        if block:
            rows = []
            for raw in block:
                # Drop exactly ONE leading and ONE trailing pipe: str.strip("|") would also eat the
                # empty cells of a shredded row ("||||text|"), which is the very signal we look for.
                body = raw[1:] if raw.startswith("|") else raw
                body = body[:-1] if body.endswith("|") else body
                cells = body.split("|")
                if all(set(c.strip()) <= set("-: ") for c in cells):
                    continue
                rows.append([clean(c) for c in cells])
            yield "table", rows
            block = []
        yield "line", line


def md_tables(text):
    return [payload for kind, payload in iter_blocks(text) if kind == "table"]


def table_is_shredded(rows):
    """A table whose columns look incoherent: an empty cell in the header row, or a column that
    is empty in every data row. Seen on real tenders when a row spanning a page break loses its
    cell borders (Tender Data Sheet p.73: seven misaligned columns). Single-column blocks are
    text boxes, not tables, and are never flagged."""
    if not rows or len(rows[0]) < 2:
        return False
    if any(not c for c in rows[0]):
        return True
    data = rows[1:]
    width = len(rows[0])
    return bool(data) and any(all(not (r[i] if i < len(r) else "") for r in data) for i in range(width))


def shredded_pages(pages):
    """Page numbers (from {page: markdown}) holding at least one shredded table."""
    return sorted(n for n, text in pages.items() if any(table_is_shredded(t) for t in md_tables(text)))


def est_tokens(text):
    """ESTIMATE: characters / 4. Gemini's tokenizer is not available offline."""
    return -(-len(text) // 4)
