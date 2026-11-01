# scratch/extraction

Throwaway prototype of the CLAUDE.md §6 document pipeline. **Superseded by `tenders/pipeline/`**
(converter, TOC, section location, goods schedule, service) — read that, not this.

Kept because it is how the findings in §6 were established, and it can be re-run against a new
tender PDF to see what the real pipeline will make of it:

- `convert.py`   — PDF -> Markdown with page markers (pymupdf4llm 1.28.2).
- `prototype.py` — TOC, section location by heading name, goods table, GFS code, token estimates.
  Locates by NAME; the real pipeline locates by TOC page range and confirms by content signature.
- `verify.py`    — compares the Markdown against the PDF's own text layer, page by page.

Run from the project root, e.g. `.venv/Scripts/python.exe scratch/extraction/prototype.py`.
Output goes to `scratch/extraction/out/` (not committed: it holds text of real tenders).

No Django, no database, no network, no Gemini.
