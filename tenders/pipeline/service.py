"""
The pipeline, end to end, up to the point where a section is ready to send (CLAUDE.md §5-§6).

    prepare_extraction(tender)   cron-driven: convert the PDF, store the Markdown, detect a missing
                                 text layer, locate the sections, parse the goods schedule.
                                 Makes NO model call and leaves a healthy tender in EXTRACTING.
    ready_sections(tender)       the extractor's inputs, built from the stored Markdown and the plan.
    create_draft_items(tender)   goods schedule rows -> draft Item rows, once the MD has registered
                                 the tender (R15).

Three rules from §5/§6 this module is responsible for:

* Nothing here may trap a tender (R12). A scan, a corrupt file, a missing PDF or an unexpected
  failure all end the same way: outcome MANUAL_ENTRY, a clear message for the PM, and the tender
  moves on to PENDING_VERIFICATION with empty fields to type.
* Conversion takes ~0.4 s per page, so this is called from a cron-invoked command, never from a request.
* ``tenders`` may not import ``items`` (§1): draft items are created through the ``tender.items``
  reverse accessor, which needs no import.
"""

import logging
from dataclasses import dataclass, field
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from core.audit import audit_actor

from ..models import (
    WORK_ALLOWED_STATUSES,
    ExtractionOutcome,
    Tender,
    TenderDocumentKind,
    TenderExtraction,
    TenderStatus,
    TransitionError,
)
from . import sections
from .converter import ConversionError, PyMuPDF4LLMConverter, check_text_layer
from .goods import parse_goods_schedule
from .tables import split_pages
from .toc import parse_toc

logger = logging.getLogger(__name__)

# A PROCESSING claim older than this is assumed to belong to a crashed worker and is taken over.
STALE_AFTER = timedelta(minutes=15)

MSG_NO_PDF = "No tender PDF is attached to this tender. Enter the tender details by hand."
MSG_UNREADABLE = "The PDF could not be read ({reason}). Enter the tender details by hand."
MSG_SCANNED = (
    "This PDF has no readable text — it looks like a scanned document. "
    "Nothing could be read from it automatically, so enter the tender details by hand. ({reason})"
)
MSG_FAILED = "The tender could not be read automatically. Enter the tender details by hand."


def _tender_pdf(tender):
    return tender.documents.filter(kind=TenderDocumentKind.TENDER_PDF).order_by("-created_at", "-id").first()


def _claim(tender, stale_after):
    """Take the tender for processing. Returns (extraction, claimed). Two cron runs overlapping
    cannot both claim it: the tender row is locked and TenderExtraction is one row per tender."""
    with transaction.atomic(), audit_actor(None):
        locked = Tender.objects.select_for_update().get(pk=tender.pk)
        if locked.status != TenderStatus.EXTRACTING:
            raise TransitionError(
                f"prepare_extraction needs a tender in EXTRACTING, not {locked.status}.", code="r19"
            )
        extraction = TenderExtraction.objects.filter(tender=locked).first()
        if extraction is None:
            return TenderExtraction.objects.create(tender=locked), True
        stale = (
            extraction.outcome == ExtractionOutcome.PROCESSING
            and extraction.updated_at < timezone.now() - stale_after
        )
        if stale:
            extraction.save()  # refreshes updated_at: this worker now owns it
            return extraction, True
        return extraction, False


def _finish(tender, extraction, *, outcome, message="", converter="", plan=None, markdown=None, pdf=None):
    """Record the result and, for manual entry, move the tender on (§5 step 6)."""
    with transaction.atomic(), audit_actor(None):
        if markdown is not None and pdf is not None:
            pdf.markdown = markdown
            pdf.save()
        extraction.outcome, extraction.message, extraction.converter = outcome, message, converter
        extraction.plan = plan or {}
        extraction.save()
        if outcome == ExtractionOutcome.MANUAL_ENTRY:
            current = Tender.objects.select_for_update().get(pk=tender.pk)
            if current.status == TenderStatus.EXTRACTING:  # not withdrawn meanwhile
                current.finish_extraction(None)
    tender.refresh_from_db()
    return extraction


def prepare_extraction(tender, *, converter=None, stale_after=STALE_AFTER):
    """
    Convert the tender's PDF and decide how it will be read. Idempotent: a tender that already
    has a result, or is being processed, is returned untouched.

    * Healthy PDF  -> TenderExtraction.outcome PREPARED, ``plan`` filled, tender stays EXTRACTING.
    * No/blank text layer, corrupt, encrypted, missing, or any unexpected failure
                   -> outcome MANUAL_ENTRY with a message, tender -> PENDING_VERIFICATION.
    """
    converter = converter or PyMuPDF4LLMConverter()
    extraction, claimed = _claim(tender, stale_after)
    if not claimed:
        return extraction

    pdf = _tender_pdf(tender)
    if pdf is None:
        return _finish(tender, extraction, outcome=ExtractionOutcome.MANUAL_ENTRY, message=MSG_NO_PDF)
    try:
        with pdf.file.open("rb") as fh:
            data = fh.read()
        document = converter.convert(data)
    except ConversionError as exc:
        return _finish(tender, extraction, outcome=ExtractionOutcome.MANUAL_ENTRY,
                       message=MSG_UNREADABLE.format(reason=exc))
    except Exception:  # never trap a tender (R12)
        logger.exception("Converting the PDF of tender %s failed", tender.reference)
        return _finish(tender, extraction, outcome=ExtractionOutcome.MANUAL_ENTRY, message=MSG_FAILED)

    try:
        report = check_text_layer(document)
        if not report.ok:
            return _finish(tender, extraction, outcome=ExtractionOutcome.MANUAL_ENTRY,
                           message=MSG_SCANNED.format(reason=report.reason), converter=document.converter,
                           plan={"text_layer": report.as_dict()}, markdown=document.markdown, pdf=pdf)
        pages = document.page_markdown()
        toc = parse_toc(pages)
        plan = sections.plan_extraction(pages, document.page_raw_text(), toc, report)
        plan["goods_schedule"] = _parse_goods(pages, plan).as_dict()
    except Exception:
        logger.exception("Planning the extraction of tender %s failed", tender.reference)
        return _finish(tender, extraction, outcome=ExtractionOutcome.MANUAL_ENTRY, message=MSG_FAILED,
                       converter=document.converter, markdown=document.markdown, pdf=pdf)
    return _finish(tender, extraction, outcome=ExtractionOutcome.PREPARED, converter=document.converter,
                   plan=plan, markdown=document.markdown, pdf=pdf)


def _parse_goods(pages, plan):
    """The goods schedule: the located range, or the whole document when it was not located."""
    located = plan["sections"].get(sections.Kind.GOODS_SCHEDULE)
    start, end = located["pages"] if located else (min(pages), max(pages))
    return parse_goods_schedule(pages, start, end)


def _stored_pages(tender):
    pdf = _tender_pdf(tender)
    if pdf is None or not pdf.markdown:
        raise TransitionError("This tender has no converted Markdown yet.", code="no_markdown")
    return split_pages(pdf.markdown)


def _prepared_extraction(tender):
    extraction = TenderExtraction.objects.filter(tender=tender).first()
    if extraction is None or extraction.outcome != ExtractionOutcome.PREPARED:
        raise TransitionError("This tender has not been prepared for extraction.", code="not_prepared")
    return extraction


def ready_sections(tender):
    """The extractor's inputs: one ``ReadySection`` per required group, from the stored Markdown
    and the plan. The last step of this pipeline — it calls no model."""
    return sections.ready_sections(_stored_pages(tender), _prepared_extraction(tender).plan)


@dataclass
class DraftItems:
    created: list = field(default_factory=list)
    skipped: list = field(default_factory=list)  # [(GoodsRow, reason)]: reported, never silently dropped
    lots: list = field(default_factory=list)  # GoodsLot: carries lot number, title and GFS code
    already_had_items: bool = False


def create_draft_items(tender, *, actor=None):
    """
    §5 step 9: parse the goods schedule from the stored Markdown into draft Item rows.

    * Only once the MD has registered the tender, and never while it is frozen (R15, R9a).
    * Never twice: a tender that already has items is left alone (the PM edits them from here).
    * One Item per schedule row. A row whose quantity is not a whole number >= 1 is SKIPPED and
      reported, not guessed (R12) — the PM adds it by hand. A ``Lot`` row stays one item.
    """
    tender.require_unfrozen("Creating draft items")  # R9a
    status = Tender._base_manager.filter(pk=tender.pk).values_list("status", flat=True).first()
    if status not in WORK_ALLOWED_STATUSES:
        raise TransitionError(
            f"Draft items are created only after the MD registers the tender; it is {status} (R15).", code="r15"
        )
    items = tender._related("items")
    if items is None:
        raise TransitionError("The items app is not installed, so there is nowhere to put draft items.", code="no_items_app")
    result = DraftItems()
    if items.exists():
        result.already_had_items = True
        return result
    pages = _stored_pages(tender)
    plan = _prepared_extraction(tender).plan
    schedule = _parse_goods(pages, plan)
    result.lots = schedule.lots
    with transaction.atomic(), audit_actor(actor):
        for lot in schedule.lots:
            for row in lot.rows:
                if row.quantity is None:
                    result.skipped.append((row, "; ".join(row.warnings) or "quantity could not be read"))
                    continue
                if not row.description:
                    result.skipped.append((row, "empty description"))
                    continue
                result.created.append(
                    items.create(
                        specification=row.description,
                        quantity=row.quantity,
                        unit_of_measure=row.unit_of_measure[:50],
                    )
                )
    return result
