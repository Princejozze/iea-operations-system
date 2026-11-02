"""
HTML to PDF with PyMuPDF's ``Story`` — already a pinned dependency, so no extra
package or system binary (no wkhtmltopdf, no headless browser).

Story understands a modest subset of HTML/CSS (tables, borders, padding, basic
fonts). Keep document templates to that subset; ``test_pdf`` renders every kind
and parses the result back, so a template that outgrows it fails loudly.
"""

import io

import pymupdf

PAGE = pymupdf.paper_rect("a4")
MARGIN = 48  # points


def html_to_pdf(html):
    """Return ``html`` rendered as PDF bytes, paginated on A4."""
    story = pymupdf.Story(html=html)
    out = io.BytesIO()
    writer = pymupdf.DocumentWriter(out)
    more = True
    while more:
        device = writer.begin_page(PAGE)
        more, _ = story.place(PAGE + (MARGIN, MARGIN, -MARGIN, -MARGIN))
        story.draw(device)
        writer.end_page()
    writer.close()
    return out.getvalue()
