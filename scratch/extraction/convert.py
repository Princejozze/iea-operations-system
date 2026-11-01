"""Step 1: PDF -> Markdown with page markers (pymupdf4llm 1.28.2). File processing only."""
import sys, time, pathlib
import pymupdf, pymupdf4llm

PDFS = pathlib.Path("tenders/tests/fixtures/pdfs")
OUT = pathlib.Path("scratch/extraction/out")

def convert(pdf_path):
    doc = pymupdf.open(pdf_path)
    chunks = pymupdf4llm.to_markdown(doc, page_chunks=True, show_progress=False)
    pages = [(c["metadata"]["page_number"], c["text"]) for c in chunks]
    md = "".join(f"\n<!-- page {n} -->\n{t}" for n, t in pages)
    return pages, md

if __name__ == "__main__":
    for pdf in sorted(PDFS.glob("*.pdf")):
        t = time.time()
        pages, md = convert(pdf)
        (OUT / (pdf.stem + ".md")).write_text(md, encoding="utf-8")
        print(f"{pdf.name}: {len(pages)} pages, {len(md):,} chars, {time.time()-t:.0f}s")
