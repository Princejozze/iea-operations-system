"""
Locating the sections section 6 names (CLAUDE.md §6).

The rule, learned from three real tenders: **locate by TOC page range, then confirm
by content signature — not by heading name.** Every TOC entry is a candidate range.
A candidate is accepted only if its pages pass the section's content signature
(e.g. "a table headed S/N | Description | Unit of measure | Quantity" for the goods
schedule). The alias list is a HINT: it ranks candidates, it never decides. A list
tuned on three documents will miss on a fourth, so an unfamiliar heading over a page
range with the right content is still located; a familiar heading over the wrong
content is not.

A required section that is not confidently located is handed to the extractor as the
WHOLE DOCUMENT with the TOC as a hint (``plan_extraction`` / ``ready_sections``). That
is a first-class path, never an error: nothing about an unfamiliar heading may fail an upload.
"""

import re
from dataclasses import dataclass, field

from .tables import est_tokens, key, md_tables, norm, render_markdown, shredded_pages


class Kind:
    """What was located (a TOC range). Several can feed one required group."""

    TENDER_DATA_SHEET = "TENDER_DATA_SHEET"
    INVITATION = "INVITATION"
    ELIGIBILITY = "ELIGIBILITY"
    GOODS_SCHEDULE = "GOODS_SCHEDULE"
    SPECIFICATIONS = "SPECIFICATIONS"
    FORMS = "FORMS"

    ALL = (TENDER_DATA_SHEET, INVITATION, ELIGIBILITY, GOODS_SCHEDULE, SPECIFICATIONS, FORMS)


class Group:
    """What the extractor needs (§6 steps 2-6). METADATA is fed by the Tender Data Sheet
    and/or the Invitation."""

    METADATA = "METADATA"
    ELIGIBILITY = "ELIGIBILITY"
    GOODS_SCHEDULE = "GOODS_SCHEDULE"
    SPECIFICATIONS = "SPECIFICATIONS"
    FORMS = "FORMS"

    ALL = (METADATA, ELIGIBILITY, GOODS_SCHEDULE, SPECIFICATIONS, FORMS)


# --------------------------------------------------------------------------- #
# content signatures: (pages, start, end) -> evidence lines; empty list = not confirmed
# --------------------------------------------------------------------------- #

GOODS_HEADER = {"sn", "description", "unitofmeasure", "quantity"}


def is_goods_header(row):
    return GOODS_HEADER <= {key(c) for c in row}


def _range_text(pages, start, end):
    return "\n".join(pages.get(p, "") for p in range(start, end + 1)).replace("<br>", " ")


def sig_goods_schedule(pages, start, end):
    """A table headed S/N | Description | Unit of measure | Quantity."""
    return [
        f"goods table header on p.{p}"
        for p in range(start, end + 1)
        if any(is_goods_header(t[0]) for t in md_tables(pages.get(p, "")) if t)
    ]


def sig_specifications(pages, start, end):
    """A 'Technical Requirements' table, or a heading that says Specifications. TANAPA's
    Specifications section has NO heading at all and is confirmed only by the table."""
    out = []
    for p in range(start, end + 1):
        text = pages.get(p, "")
        if any(t and "technicalrequirements" in {key(c) for c in t[0]} for t in md_tables(text)):
            out.append(f"'Technical Requirements' table on p.{p}")
        for line in text.splitlines():
            if line.strip().startswith(("#", "**")) and norm(line) in ("technical specifications", "specifications"):
                out.append(f"heading '{norm(line)}' on p.{p}")
    return out


def sig_tender_data_sheet(pages, start, end):
    """A table with an 'ITT Clause' column: the Tender Data Sheet's TDS No. | Information | ITT Clause | Data layout."""
    return [
        f"'ITT Clause' table on p.{p}"
        for p in range(start, end + 1)
        if any(t and "ittclause" in {key(c) for c in t[0]} for t in md_tables(pages.get(p, "")))
    ]


_INVITATION = re.compile(r"invitation\s+(for|date)|mini-?competition\s+quotation|calls\s+for\s+a\s+mini", re.I)
_TENDER_NO = re.compile(r"(tender|quotation)\s+no\b", re.I)
_DEADLINE = re.compile(r"(submission\s+deadline|deadline\s+for\s+submission)", re.I)


def sig_invitation(pages, start, end):
    text = _range_text(pages, start, end)
    if _INVITATION.search(text) and _TENDER_NO.search(text):
        return ["invitation wording and a tender/quotation number in range"]
    return []


_SCORE = re.compile(r"comply\s*/\s*not\s+comply", re.I)
_EVAL = re.compile(r"commercial\s+evaluation|technical\s+evaluation|eligibility|experience", re.I)


def sig_eligibility(pages, start, end):
    text = _range_text(pages, start, end)
    if _SCORE.search(text) and _EVAL.search(text):
        return ["'Comply/Not Comply' scoring criteria in range"]
    return []


# Titles of the standard forms, matched against HEADINGS only. The ITT prose mentions tender
# security, power of attorney and so on in passing; only the forms themselves carry these headings.
_FORM_TITLES = (
    r"form of tender", r"quotation submission form", r"power of attorney", r"securing declaration",
    r"declaration against fraud", r"authori[sz]ation form", r"performance (securing|security)",
    r"price adjustment data", r"schedule of cost indexation",
)


def sig_forms(pages, start, end):
    found = set()
    for p in range(start, end + 1):
        for line in pages.get(p, "").splitlines():
            if line.strip().startswith(("#", "**")):
                text = norm(line)
                found.update(pat for pat in _FORM_TITLES if re.search(pat, text))
    return [f"{len(found)} standard form titles as headings in range"] if len(found) >= 2 else []


# A section's content starts within a page or two of its TOC page (the TOC page is often a
# divider carrying only "SECTION III: TENDER DATA SHEET"). Confirming content only in this
# window — clipped to the entry's own range — keeps a wrong TOC page number from "confirming" a
# huge range that merely contains the content somewhere, and keeps a neighbour's window from
# reaching into the next section.
SIGNATURE_WINDOW = 4  # pages: the TOC page and the three after it


@dataclass(frozen=True)
class SectionSpec:
    kind: str
    aliases: tuple  # normalised TOC names that HINT at this section (never decisive)
    signature: object
    include_children: bool  # container sections (TDS, QEC, forms) own their subsections' pages
    window: object = SIGNATURE_WINDOW  # pages from the TOC page to examine; None = the whole range

    @property
    def is_container(self):
        return self.include_children


SPECS = {
    Kind.TENDER_DATA_SHEET: SectionSpec(Kind.TENDER_DATA_SHEET, ("tender data sheet",), sig_tender_data_sheet, True),
    Kind.INVITATION: SectionSpec(
        Kind.INVITATION, ("invitation", "invitation for tender", "invitation for tenders", "invitation for quotations"),
        sig_invitation, False),
    Kind.ELIGIBILITY: SectionSpec(
        Kind.ELIGIBILITY, ("qualification and evaluation criteria",), sig_eligibility, True),
    Kind.GOODS_SCHEDULE: SectionSpec(
        Kind.GOODS_SCHEDULE,
        ("list of goods and related services", "schedule of goods and related services", "schedule of requirements"),
        sig_goods_schedule, False),
    Kind.SPECIFICATIONS: SectionSpec(
        Kind.SPECIFICATIONS, ("technical specifications", "specifications"), sig_specifications, False),
    # The forms are spread over many pages (a divider, then one form after another): examine them all.
    Kind.FORMS: SectionSpec(Kind.FORMS, ("tendering forms",), sig_forms, True, window=None),
}


# --------------------------------------------------------------------------- #
# locating
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class LocatedSection:
    kind: str
    start: int
    end: int
    toc_name: str
    toc_page: int
    alias_matched: bool
    evidence: tuple

    @property
    def confidence(self):
        """HIGH: the TOC name is a known alias AND the content confirms it. MEDIUM: the content
        confirms it but the name was unfamiliar (the case a fixed alias list cannot handle)."""
        return "high" if self.alias_matched else "medium"

    def as_dict(self):
        return {"pages": [self.start, self.end], "toc_name": self.toc_name, "toc_page": self.toc_page,
                "alias_matched": self.alias_matched, "confidence": self.confidence, "evidence": list(self.evidence)}


def locate_section(pages, toc, spec):
    """Best confirmed candidate for ``spec``, or (None, reason).

    Every TOC entry is a candidate. Only those whose first pages pass the content signature
    survive. Among survivors an alias match wins, then the outermost entry (containers) or the
    narrowest (leaves)."""
    survivors, rejected_aliases = [], []
    for entry in toc.entries:
        start = entry.page
        end = entry.end_incl_children if spec.include_children else entry.end
        last = end if spec.window is None else min(end, start + spec.window - 1)
        evidence = spec.signature(pages, start, last)
        alias = norm(entry.name) in spec.aliases
        if evidence:
            # Preference: an alias match first (a hint), then the outermost entry for a container
            # section (TDS, QEC, forms: their subsections would also pass) or the narrowest for a
            # leaf section (the specific entry, not its parent).
            rank = (not alias, entry.page if spec.is_container else end - start, end - start)
            survivors.append((rank, entry, evidence, alias))
        elif alias:
            rejected_aliases.append(f"'{entry.name}' (p.{entry.page}) matched by name but its pages do not look right")
    if not survivors:
        reason = "; ".join(rejected_aliases) or (
            "no table-of-contents entry covers pages with the expected content" if toc else "no table of contents found"
        )
        return None, reason
    _, entry, evidence, alias = min(survivors, key=lambda s: s[0])
    start = entry.page
    end = entry.end_incl_children if spec.include_children else entry.end
    return LocatedSection(spec.kind, start, end, entry.name, entry.page, alias, tuple(evidence)), ""


def locate_all(pages, toc):
    """({kind: LocatedSection}, {kind: reason it was not located})."""
    located, missing = {}, {}
    for kind, spec in SPECS.items():
        section, reason = locate_section(pages, toc, spec)
        if section:
            located[kind] = section
        else:
            missing[kind] = reason
    return located, missing


def group_status(pages, located):
    """For each required group: the located kinds that satisfy it, or None if it needs the
    whole-document fallback. METADATA is satisfied by the Tender Data Sheet, or by an
    Invitation that itself states the submission deadline (a mini-competition has no TDS)."""
    status = {g: None for g in Group.ALL}
    if Kind.TENDER_DATA_SHEET in located:
        status[Group.METADATA] = [k for k in (Kind.TENDER_DATA_SHEET, Kind.INVITATION) if k in located]
    elif Kind.INVITATION in located:
        inv = located[Kind.INVITATION]
        if _DEADLINE.search(_range_text(pages, inv.start, inv.end)):
            status[Group.METADATA] = [Kind.INVITATION]
    if Kind.ELIGIBILITY in located:
        status[Group.ELIGIBILITY] = [Kind.ELIGIBILITY]
    if Kind.GOODS_SCHEDULE in located:
        status[Group.GOODS_SCHEDULE] = [Kind.GOODS_SCHEDULE]
    if Kind.SPECIFICATIONS in located:
        status[Group.SPECIFICATIONS] = [Kind.SPECIFICATIONS]
    if Kind.FORMS in located:
        status[Group.FORMS] = [Kind.FORMS]
    return status


def forms_named_in_toc(toc, forms_section):
    """§6 step 4: the TOC lists the required forms by name — the entries inside the forms section."""
    if forms_section is None:
        return []
    return [
        e.name for e in toc.entries
        if e.level == "sub" and forms_section.start < e.page <= forms_section.end
    ]


# --------------------------------------------------------------------------- #
# the plan, and sections ready to send
# --------------------------------------------------------------------------- #

PLAN_VERSION = 1


def plan_extraction(pages, raw_text, toc, text_layer):
    """Everything decided before the model is called, as a JSON-serialisable dict.

    ``pages`` {n: markdown}; ``raw_text`` {n: raw text layer}. Stored on ``TenderExtraction.plan``.
    """
    located, missing = locate_all(pages, toc)
    status = group_status(pages, located)
    fallback = [g for g, kinds in status.items() if kinds is None]

    # Pages whose tables shredded: send the raw text layer for those pages instead (§6).
    in_sections = {p for s in located.values() for p in range(s.start, s.end + 1)}
    wanted = set(pages) if fallback else in_sections
    raw_pages = {str(p): raw_text.get(p, "") for p in shredded_pages(pages) if p in wanted}

    return {
        "version": PLAN_VERSION,
        "page_count": len(pages),
        "text_layer": text_layer.as_dict(),
        "toc": toc.as_dict(),
        "sections": {k: s.as_dict() for k, s in located.items()},
        "not_located": missing,
        "groups": {g: (kinds if kinds is not None else "WHOLE_DOCUMENT") for g, kinds in status.items()},
        "whole_document_fallback": fallback,
        "forms_named_in_toc": forms_named_in_toc(toc, located.get(Kind.FORMS)),
        "raw_text_pages": raw_pages,
    }


@dataclass(frozen=True)
class ReadySection:
    """One piece of text, ready to hand to the extractor. Building it is the last thing this
    pipeline does: it makes no model call."""

    group: str
    method: str  # "toc+content" or "whole_document"
    ranges: tuple  # ((first_page, last_page), ...)
    text: str  # Markdown with page markers (page provenance, §6)
    sources: tuple  # located kinds that fed it; empty for whole_document
    toc_hint: str = ""  # only for whole_document
    raw_text_pages: tuple = field(default=())  # pages sent as raw text because their tables shredded

    @property
    def est_tokens(self):
        return est_tokens(self.text)


RAW_NOTE = "<!-- raw text layer: the table columns on this page did not convert reliably -->"


def _assemble(pages, ranges, raw_pages):
    out, used_raw = [], []
    for start, end in ranges:
        for n in range(start, end + 1):
            if n not in pages:
                continue
            if str(n) in raw_pages:
                out.append((n, f"{RAW_NOTE}\n{raw_pages[str(n)]}"))
                used_raw.append(n)
            else:
                out.append((n, pages[n]))
    return render_markdown(out), tuple(used_raw)


def ready_sections(pages, plan):
    """Build the extractor's inputs from the stored Markdown pages and the plan. One
    ``ReadySection`` per required group: its located range(s) when confident, otherwise the
    whole document with the TOC as a hint."""
    toc_hint = "\n".join(f"{e['name']} .... p.{e['page']}" for e in plan["toc"]["entries"])
    raw_pages = plan["raw_text_pages"]
    last = max(pages) if pages else 0
    out = []
    for group in Group.ALL:
        kinds = plan["groups"][group]
        if kinds == "WHOLE_DOCUMENT":
            text, used = _assemble(pages, [(1, last)], raw_pages)
            out.append(ReadySection(group, "whole_document", ((1, last),), text, (), toc_hint, used))
            continue
        ranges = tuple(tuple(plan["sections"][k]["pages"]) for k in kinds)
        text, used = _assemble(pages, ranges, raw_pages)
        out.append(ReadySection(group, "toc+content", ranges, text, tuple(kinds), "", used))
    return out
