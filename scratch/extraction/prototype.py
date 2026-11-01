"""
Prototype of the CLAUDE.md section 6 document pipeline. Standalone: no Django, no
database, no network, no Gemini. Run from the project root:

    .venv/Scripts/python.exe scratch/extraction/prototype.py

Per PDF in tenders/tests/fixtures/pdfs/:
  1. PDF -> Markdown with pymupdf4llm, page markers kept (<!-- page N -->)
  2. parse the table of contents into names + page ranges
  3. locate the four sections section 6 names (+ TENDERING FORMS for required_forms)
  4. parse the goods schedule table into S/N | Description | Unit of measure | Quantity
  5. find the GFS code
  6. report sizes: estimated tokens per section vs the whole document

Token counts are ESTIMATES (characters / 4): Gemini's tokenizer cannot be called
from here (no API calls). Treat them as order-of-magnitude.
"""

import json
import math
import pathlib
import re
import sys

import pymupdf
import pymupdf4llm

ROOT = pathlib.Path(__file__).resolve().parents[2]
PDFS = ROOT / "tenders" / "tests" / "fixtures" / "pdfs"
OUT = ROOT / "scratch" / "extraction" / "out"

# --------------------------------------------------------------------------- #
# 1. PDF -> Markdown with page markers
# --------------------------------------------------------------------------- #


def load_pages(pdf_path):
    """[(page_number, markdown)] — cached as JSON next to the output, keyed on the PDF's mtime."""
    cache = OUT / f"{pdf_path.stem}.pages.json"
    if cache.exists() and cache.stat().st_mtime > pdf_path.stat().st_mtime:
        return [tuple(p) for p in json.loads(cache.read_text(encoding="utf-8"))]
    doc = pymupdf.open(pdf_path)
    chunks = pymupdf4llm.to_markdown(doc, page_chunks=True, show_progress=False)
    pages = [(c["metadata"]["page_number"], c["text"]) for c in chunks]
    cache.write_text(json.dumps(pages), encoding="utf-8")
    return pages


def with_markers(pages):
    return "".join(f"\n<!-- page {n} -->\n{t}" for n, t in pages)


def est_tokens(text):
    return math.ceil(len(text) / 4)


# --------------------------------------------------------------------------- #
# 2. table of contents
# --------------------------------------------------------------------------- #

TOC_ROW = re.compile(r"^\|\s*(.+?)\s*\|\s*(\d{1,3})\s*\|\s*$")
CHROME = re.compile(r"^(_Goods\s*:.*_|\d{1,3}|<!--.*-->|\|\s*-+\s*\|\s*-+\s*\|)$")


def clean(s):
    return " ".join(re.sub(r"\*+", "", s.replace("<br>", " ")).split())


def norm(s):
    """Lower-case, drop 'SECTION IV:' / 'PART 2:' / '3.' prefixes and punctuation."""
    s = clean(s).replace("’", "'").lower()
    s = re.sub(r"^(section|part)\s+[ivxlc\d]+\s*[:.\-]\s*", "", s)
    s = re.sub(r"^\d+\.\s*", "", s)
    s = re.sub(r"[^a-z0-9' ]+", " ", s)
    return " ".join(s.split())


def is_toc_page(text):
    lines = [l.strip() for l in text.splitlines() if l.strip() and not CHROME.match(l.strip())]
    rows = [l for l in lines if TOC_ROW.match(l)]
    return len(rows) >= 3 and len(rows) >= 0.6 * len(lines)


def parse_toc(pages):
    """Return (entries, toc_page_numbers). Entry: name, page, level ('top' if ALL CAPS)."""
    start = next(
        (n for n, t in pages[:10] if re.search(r"table of contents", t, re.I)), None
    )
    if start is None:
        return [], []
    toc_pages, entries = [], []
    for n, text in pages:
        if n < start:
            continue
        if n != start and not is_toc_page(text):
            break
        toc_pages.append(n)
        for line in text.splitlines():
            m = TOC_ROW.match(line.strip())
            if m and not set(m.group(1)) <= set("-: "):
                name = clean(m.group(1))
                letters = re.sub(r"[^A-Za-z]", "", name)
                entries.append(
                    {"name": name, "page": int(m.group(2)), "level": "top" if letters.isupper() else "sub"}
                )
    last_page = pages[-1][0]
    for i, e in enumerate(entries):
        later = [x["page"] for x in entries[i + 1:] if x["page"] > e["page"]]
        e["end"] = (later[0] - 1) if later else last_page
        tops = [x["page"] for x in entries[i + 1:] if x["level"] == "top" and x["page"] > e["page"]]
        e["end_incl_children"] = (tops[0] - 1) if tops else last_page
    return entries, toc_pages


# --------------------------------------------------------------------------- #
# 3. locate the sections section 6 names
# --------------------------------------------------------------------------- #

# Known headings per section, in preference order (section 6 table). Matched on the
# TOC entry name, normalised. Variants seen: mini-competition and general goods.
KNOWN = {
    "metadata (Tender Data Sheet)": ["tender data sheet"],
    "metadata (Invitation)": ["invitation", "invitation for tender", "invitation for quotations"],  # TOC says "SECTION I: INVITATION", body says "... FOR QUOTATIONS"
    "qualification_and_evaluation": ["qualification and evaluation criteria"],
    "goods_schedule": ["list of goods and related services", "schedule of goods and related services"],
    "goods_schedule (parent fallback)": ["schedule of requirements"],
    "specifications": ["technical specifications", "specifications"],
    "tendering_forms": ["tendering forms"],
}


def find_entry(entries, wanted, level=None):
    for w in wanted:
        for e in entries:
            if norm(e["name"]) == w and (level is None or e["level"] == level):
                return e
    return None


def locate(entries):
    found = {}
    found["metadata (Tender Data Sheet)"] = find_entry(entries, KNOWN["metadata (Tender Data Sheet)"], "top")
    found["metadata (Invitation)"] = find_entry(entries, KNOWN["metadata (Invitation)"], "top")
    found["qualification_and_evaluation"] = find_entry(entries, KNOWN["qualification_and_evaluation"], "top")
    sched = find_entry(entries, KNOWN["goods_schedule"])
    via = "TOC entry"
    if sched is None:
        parent = find_entry(entries, KNOWN["goods_schedule (parent fallback)"], "top")
        if parent is not None:
            # the parent's own range only: stop where the next TOC entry starts
            sched, via = parent, "parent section (no 'List of Goods' entry in the TOC)"
    found["goods_schedule"] = sched
    found["specifications"] = find_entry(entries, KNOWN["specifications"])
    found["tendering_forms"] = find_entry(entries, KNOWN["tendering_forms"], "top")
    found["_goods_via"] = via
    return found


def section_range(entry, key):
    """Pages a section covers. Top-level sections that own subsections (TDS, QEC, forms)
    take their children too; subsections stop where the next TOC entry starts."""
    if entry is None:
        return None
    if entry["level"] == "top" and key not in ("goods_schedule",):
        return (entry["page"], entry["end_incl_children"])
    return (entry["page"], entry["end"])


def verify_heading(pages_by_no, entry, aliases=()):
    """Does the entry's heading appear on the page the TOC says? The body often words a heading
    differently from the TOC, so every alias counts. Returns (page_found, note)."""
    targets = {norm(entry["name"]), *aliases}
    for offset in (0, 1, -1, 2, -2, 3):
        p = entry["page"] + offset
        text = pages_by_no.get(p, "")
        for line in text.splitlines():
            if not line.strip().startswith(("#", "**")):
                continue
            n = norm(line)
            if n and any(n == tg or (tg in n and len(n) <= len(tg) + 12) for tg in targets):  # near-equality: a long line merely CONTAINING the words is not a heading
                return p, "heading on stated page" if offset == 0 else f"heading {offset:+d} page(s) from TOC"
    return None, "no matching heading text on or near that page (TOC alone placed it)"


def toc_reliability(pages_by_no, entries):
    """How far can the TOC's page numbers be trusted? For every entry: is its own heading on
    the stated page, nearby, or nowhere (renamed/absent in the body)?"""
    out = {"on_page": 0, "nearby": 0, "not_found": [], "total": len(entries)}
    for e in entries:
        page, note = verify_heading(pages_by_no, e)
        if page == e["page"]:
            out["on_page"] += 1
        elif page is not None:
            out["nearby"] += 1
        else:
            out["not_found"].append(f'{e["name"]} (p.{e["page"]})')
    return out


# --------------------------------------------------------------------------- #
# 4. goods schedule table -> rows
# --------------------------------------------------------------------------- #


def md_tables(text):
    """Yield each markdown table as a list of rows (list of cleaned cell strings)."""
    block = []
    for line in text.splitlines() + [""]:
        if line.strip().startswith("|"):
            block.append(line.strip())
            continue
        if block:
            rows = []
            for l in block:
                cells = [c for c in l.strip("|").split("|")]
                if all(set(c.strip()) <= set("-: ") for c in cells):
                    continue  # separator row
                rows.append([clean(c) for c in cells])
            yield rows
            block = []


def key(s):
    return re.sub(r"[^a-z]", "", s.lower())


def is_goods_header(row):
    k = [key(c) for c in row]
    return "sn" in k and "description" in k and "unitofmeasure" in k and "quantity" in k


def parse_goods(pages_by_no, page_range):
    """Rows: S/N, Description, Unit of measure, Quantity (+ source pages, merge notes)."""
    rows, notes, started = [], [], False
    for p in range(page_range[0], page_range[1] + 1):
        for table in md_tables(pages_by_no.get(p, "")):
            header = table[0]
            if is_goods_header(header):
                started, data = True, table[1:]
            elif started:
                # A table after the goods table that is not a recognisable table of its own
                # is the previous row spilling over a page break.
                if len(header) == 1 or len(header) == 4:
                    data = table  # its "header" row is really data
                    notes.append(f"p.{p}: headerless {len(header)}-column fragment merged into the previous row")
                else:
                    continue
            else:
                continue
            for r in data:
                r = (r + [""] * 4)[:4] if len(r) >= 4 else r
                if len(r) == 1:  # single-column spill: more description text
                    if rows:
                        rows[-1]["description"] = f'{rows[-1]["description"]} {r[0]}'.strip()
                        rows[-1]["pages"].append(p)
                    continue
                sn, desc, uom, qty = r
                if not sn and rows:  # wrapped row: S/N blank, text belongs to the row above
                    rows[-1]["description"] = f'{rows[-1]["description"]} {desc}'.strip()
                    rows[-1]["unit_of_measure"] = rows[-1]["unit_of_measure"] or uom
                    rows[-1]["quantity_raw"] = rows[-1]["quantity_raw"] or qty
                    notes.append(f"p.{p}: row with blank S/N merged into previous row")
                    continue
                rows.append({"sn": sn, "description": desc, "unit_of_measure": uom,
                             "quantity_raw": qty, "pages": [p]})
    for r in rows:
        q = r["quantity_raw"].replace(",", "").strip()
        r["quantity"] = int(q) if re.fullmatch(r"\d+", q) else None
        r["pages"] = sorted(set(r["pages"]))
        r["sub_items"] = numbered_sub_items(r["description"])
        r["bullets"] = r["description"].count("•")
    return rows, notes


def numbered_sub_items(text):
    """Consecutive '1. X', '2. Y' markers inside one description: a Lot row can bundle several items."""
    found, expect = [], 1
    for m in re.finditer(r"(?:^|\s)(\d{1,2})\.\s+(?=[A-Za-z])", text):
        if int(m.group(1)) == expect:
            found.append(m.start())
            expect += 1
    return len(found) if len(found) >= 2 else 0


def detect_variant(pages_by_no, entries):
    head = " ".join(pages_by_no.get(n, "")[:300] for n in (2, 3, 4))
    if re.search(r"mini-?competition", head, re.I):
        return "mini-competition"
    if find_entry(entries, KNOWN["metadata (Tender Data Sheet)"], "top"):
        return "general goods"
    return "unknown"


def table_health(text):
    """Count tables and flag ones whose extraction looks broken: an empty cell in the header row,
    or a column that is empty in every data row (typical when merged/spanning cells got shredded)."""
    total, suspect = 0, []
    for rows in md_tables(text):
        if len(rows[0]) < 2:
            continue  # single-column blocks are text boxes, not tables
        total += 1
        header_empty = sum(1 for c in rows[0] if not c)
        empty_cols = sum(1 for i in range(len(rows[0])) if all(not (r[i] if i < len(r) else "") for r in rows[1:])) if len(rows) > 1 else 0
        if header_empty or empty_cols:
            suspect.append(clean(" ".join(rows[0]))[:60] or "(empty header)")
    return {"tables": total, "suspect": len(suspect), "examples": suspect[:3]}


def spec_table_check(text):
    """Section 6 says the specs are a two-column 'Technical Requirements | Description' table.
    Do the two columns pair up row by row, or is one just the other re-wrapped?"""
    from difflib import SequenceMatcher

    out = []
    for rows in md_tables(text):
        if len(rows[0]) == 2 and key(rows[0][0]) == "technicalrequirements":
            left = " ".join(r[0] for r in rows[1:] if r)
            right = " ".join(r[1] for r in rows[1:] if len(r) > 1)
            a, b = re.sub(r"[^a-z0-9]", "", left.lower()), re.sub(r"[^a-z0-9]", "", right.lower())
            out.append({"rows": len(rows) - 1, "left_chars": len(left), "right_chars": len(right),
                        "right_is_short_value": len(right) <= 6,
                        "similarity": round(SequenceMatcher(None, a[:1500], b[:1500]).ratio(), 2)})
    return out


# --------------------------------------------------------------------------- #
# 5. GFS code, lot
# --------------------------------------------------------------------------- #

GFS = re.compile(r"GFS\s*Code\s*:?\s*(\d{6,10})\s*[-–—]\s*([^\n*|<]+)", re.I)
LOT = re.compile(r"LOT\s*NO\.?\s*:?\s*([A-Z0-9][A-Z0-9/.\-]+)", re.I)


def find_gfs(pages_by_no, goods_range):
    hits = []
    for p, text in pages_by_no.items():
        for m in GFS.finditer(text):
            hits.append({"code": m.group(1), "label": clean(m.group(2)), "page": p})
    in_goods = [h for h in hits if goods_range and goods_range[0] <= h["page"] <= goods_range[1]]
    lots = sorted({m.group(1).rstrip(".") for t in pages_by_no.values() for m in LOT.finditer(t)})
    return in_goods, hits, lots


# --------------------------------------------------------------------------- #
# probes: does each section's text carry what section 5 wants to extract?
# --------------------------------------------------------------------------- #

PROBES = {
    "tender number": r"tender\s+(?:no|number)|quotation\s+no",
    "submission deadline": r"deadline\s+for\s+submission|closing\s+date|submission\s+deadline|not\s+later\s+than",
    "clarification": r"clarification",
    "tender security": r"tender\s+secur(?:ity|ing)|bid\s+secur",
    "site visit": r"site\s+visit|pre-?tender\s+meeting",
    "manufacturer's authorization": r"manufacturer.s\s+authori[sz]ation",
}


def probe(pages_by_no, page_range):
    out = {}
    for name, pat in PROBES.items():
        pgs = [p for p in range(page_range[0], page_range[1] + 1)
               if re.search(pat, pages_by_no.get(p, "").replace("<br>", " "), re.I)]  # the converter splits phrases with <br>
        out[name] = pgs
    return out


# --------------------------------------------------------------------------- #
# driver
# --------------------------------------------------------------------------- #


def pages_text(pages_by_no, rng):
    return "".join(pages_by_no.get(p, "") + "\n" for p in range(rng[0], rng[1] + 1))


def analyse(pdf_path):
    pages = load_pages(pdf_path)
    by_no = dict(pages)
    full_md = with_markers(pages)
    (OUT / f"{pdf_path.stem}.md").write_text(full_md, encoding="utf-8")
    entries, toc_pages = parse_toc(pages)
    located = locate(entries)
    res = {"file": pdf_path.name, "pages": len(pages), "chars": len(full_md),
           "est_tokens_total": est_tokens(full_md), "words_total": len(full_md.split()), "toc_pages": toc_pages,
           "toc_entries": len(entries), "sections": {}, "missing": []}

    ranges = {}
    for label in ("metadata (Tender Data Sheet)", "metadata (Invitation)", "qualification_and_evaluation",
                  "goods_schedule", "specifications", "tendering_forms"):
        e = located[label]
        if e is None:
            res["missing"].append(label)
            continue
        rng = section_range(e, "goods_schedule" if label == "goods_schedule" else label)
        ranges[label] = rng
        text = pages_text(by_no, rng)
        aliases = set(KNOWN[label])
        if label == "goods_schedule":
            aliases |= set(KNOWN["goods_schedule (parent fallback)"])
        found_page, note = verify_heading(by_no, e, aliases)
        res["sections"][label] = {
            "toc_name": e["name"], "toc_page": e["page"], "pages": list(rng),
            "heading_check": note, "heading_page": found_page,
            "chars": len(text), "est_tokens": est_tokens(text), "words": len(text.split()),
            "table_health": table_health(text),
            "pct_of_doc": round(100 * est_tokens(text) / res["est_tokens_total"], 1),
        }
        if label == "goods_schedule":
            res["sections"][label]["located_via"] = located["_goods_via"]

    # forms named in the TOC under TENDERING FORMS
    res["variant"] = detect_variant(by_no, entries)
    res["toc_reliability"] = toc_reliability(by_no, entries)
    if "specifications" in ranges:
        res["spec_tables"] = spec_table_check(pages_text(by_no, ranges["specifications"]))
    if located["tendering_forms"]:
        i = entries.index(located["tendering_forms"])
        forms = []
        for e in entries[i + 1:]:
            if e["level"] == "top":
                break
            forms.append(f'{e["name"]} (p.{e["page"]})')
        res["forms_named_in_toc"] = forms

    if "goods_schedule" in ranges:
        rows, notes = parse_goods(by_no, ranges["goods_schedule"])
        res["goods_rows"], res["goods_notes"] = rows, notes
    gfs_in, gfs_all, lots = find_gfs(by_no, ranges.get("goods_schedule"))
    res["gfs_in_goods_section"], res["gfs_everywhere"], res["lot_numbers"] = gfs_in, gfs_all, lots

    res["probes"] = {label: probe(by_no, ranges[label])
                     for label in ("metadata (Tender Data Sheet)", "metadata (Invitation)") if label in ranges}

    # what the section-driven strategy would send vs the whole document
    send = set()
    for label in ("metadata (Tender Data Sheet)", "metadata (Invitation)", "qualification_and_evaluation",
                  "goods_schedule", "specifications", "tendering_forms"):
        if label in ranges:
            send.update(range(ranges[label][0], ranges[label][1] + 1))
    sent_text = "".join(by_no.get(p, "") + "\n" for p in sorted(send))
    res["strategy"] = {"pages_sent": len(send), "est_tokens": est_tokens(sent_text),
                       "pct_of_doc": round(100 * est_tokens(sent_text) / res["est_tokens_total"], 1)}
    res["toc"] = entries
    return res


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    results = []
    for pdf in sorted(PDFS.glob("*.pdf")):
        r = analyse(pdf)
        (OUT / f"{pdf.stem}.result.json").write_text(json.dumps(r, indent=1, ensure_ascii=False), encoding="utf-8")
        results.append(r)
        print(f"\n{'=' * 78}\n{r['file']}  ({r['pages']} pp, ~{r['est_tokens_total']:,} tokens)")
        print(f"variant: {r['variant']}; TOC: {r['toc_entries']} entries on pages {r['toc_pages']}; "
              f"heading found on its stated page for {r['toc_reliability']['on_page']}/{r['toc_reliability']['total']}, "
              f"nearby {r['toc_reliability']['nearby']}, not found {len(r['toc_reliability']['not_found'])}")
        for label, s in r["sections"].items():
            print(f"  {label:32} TOC '{s['toc_name']}' p.{s['toc_page']}  range {s['pages'][0]}-{s['pages'][1]}"
                  f"  ~{s['est_tokens']:,} tok ({s['pct_of_doc']}%)  [{s['heading_check']}]")
        for m in r["missing"]:
            print(f"  {m:32} NOT LOCATED")
        for label, s in r["sections"].items():
            h = s["table_health"]
            if h["tables"]:
                print(f"  tables in {label}: {h['tables']}, suspect {h['suspect']} {h['examples']}")
        print("  spec tables:", r.get("spec_tables"))
        print("  goods notes:", r.get("goods_notes"))
        print("  forms named in TOC:", r.get("forms_named_in_toc"))
        for row in r.get("goods_rows", []):
            print(f"  row S/N={row['sn']!r} uom={row['unit_of_measure']!r} qty={row['quantity']} "
                  f"desc[{len(row['description'])} chars, {row['bullets']} bullets, sub_items={row['sub_items']}]: "
                  f"{row['description'][:70]!r}")
        print("  GFS in goods section:", [(g['code'], g['label'], g['page']) for g in r["gfs_in_goods_section"]])
        print("  strategy sends", r["strategy"])
    return results


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
