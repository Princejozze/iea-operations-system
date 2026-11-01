"""
Independent checks on prototype.py. Does the Markdown lose text the PDF actually has?
Compares, page by page, the alphanumeric characters in PyMuPDF's raw text extraction
against those in the pymupdf4llm Markdown for the same page. A ratio well below 1.0
means text went missing (images, outlines, a table the converter dropped).

    .venv/Scripts/python.exe scratch/extraction/verify.py
"""

import json
import pathlib
import re
import sys

import pymupdf

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import prototype as P  # noqa: E402


def alnum(s):
    return len(re.sub(r"[^A-Za-z0-9]", "", s))


def md_alnum(s):
    s = re.sub(r"<!--.*?-->|<br>", " ", s)
    return alnum(s)


def main():
    for pdf in sorted(P.PDFS.glob("*.pdf")):
        res = json.loads((P.OUT / f"{pdf.stem}.result.json").read_text(encoding="utf-8"))
        by_no = dict(P.load_pages(pdf))
        doc = pymupdf.open(pdf)
        print(f"\n{pdf.name}")
        worst = []
        for page in doc:
            raw = alnum(page.get_text())
            md = md_alnum(by_no.get(page.number + 1, ""))
            n_img = len(page.get_images())
            worst.append((round(md / raw, 2) if raw else None, page.number + 1, raw, md, n_img))
        for label, s in res["sections"].items():
            lo, hi = s["pages"]
            sel = [w for w in worst if lo <= w[1] <= hi]
            raw_t, md_t = sum(w[2] for w in sel), sum(w[3] for w in sel)
            imgs = sum(w[4] for w in sel)
            lossy = [(w[1], w[0]) for w in sel if w[0] is not None and w[0] < 0.85]
            print(f"  {label:32} pp.{lo}-{hi}: raw {raw_t:,} vs md {md_t:,} alnum chars "
                  f"(md/raw {md_t / raw_t:.2f}), images {imgs}, pages <0.85: {lossy}")
        total_raw, total_md = sum(w[2] for w in worst), sum(w[3] for w in worst)
        print(f"  WHOLE DOC: raw {total_raw:,} vs md {total_md:,} (md/raw {total_md / total_raw:.2f}); "
              f"pages with no text layer: {[w[1] for w in worst if w[2] < 20]}")


if __name__ == "__main__":
    main()
