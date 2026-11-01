"""
The tender document pipeline (CLAUDE.md §5-§6), up to the point a section is ready to send.

    converter.py   DocumentConverter interface + pymupdf4llm; text-layer check
    toc.py         table-of-contents parsing
    sections.py    locate sections by TOC range, confirm by content signature; the plan
    goods.py       goods schedule -> rows, lots and GFS codes
    service.py     prepare_extraction / ready_sections / create_draft_items
    tables.py      Markdown table helpers, page markers

Import from the submodules (``from tenders.pipeline.service import prepare_extraction``): this
package deliberately imports nothing at load time, so importing the models never pulls in PyMuPDF.
"""
