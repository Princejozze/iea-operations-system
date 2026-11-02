"""
The pipeline, end to end (CLAUDE.md §5-§6).

    prepare_extraction(tender)   cron-driven: convert the PDF, store the Markdown, detect a missing
                                 text layer, locate the sections, parse the goods schedule.
                                 Leaves a healthy tender in EXTRACTING, ready for the model.
    ready_sections(tender)       the extractor's inputs, built from the stored Markdown and the plan.
    extract_fields(tender, ex)   ask the model, guard its answer (R12), store it on TenderExtraction
                                 with a confidence and source page per field, and move the tender to
                                 PENDING_VERIFICATION. Never writes to Tender.
    process_tender(...)          prepare + extract: what the cron command runs per tender.
    fail_stuck(...)              the safety net: a tender still EXTRACTING after a deadline is finished
                                 with empty fields, so nothing is ever trapped (§5 step 6).
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

from core.models import Client

from ..models import (
    FINISHED_OUTCOMES,
    WORK_ALLOWED_STATUSES,
    ExtractionOutcome,
    Tender,
    TenderDocumentKind,
    TenderExtraction,
    TenderStatus,
    TransitionError,
)
from . import fields as extraction_fields
from . import sections
from .converter import ConversionError, PyMuPDF4LLMConverter, check_text_layer
from .extractor import ExtractionConfigError, build_request, forms_from_plan
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
MSG_EXTRACT_FAILED = "Automatic extraction failed. Enter the tender details by hand."
MSG_NOTHING_READ = "Nothing could be read from the document automatically. Enter the tender details by hand."
MSG_PARTIAL = "Some details could not be read from the document. Check every field and complete the empty ones."
MSG_STUCK = "Automatic extraction did not finish in time. Enter the tender details by hand."


def _tender_pdf(tender):
    return tender.documents.filter(kind=TenderDocumentKind.TENDER_PDF).order_by("-created_at", "-id").first()


def _claim(tender, stale_after=None):
    """Take the tender for processing. Returns (extraction, claimed). Two cron runs overlapping
    cannot both claim it: the tender row is locked and TenderExtraction is one row per tender."""
    if stale_after is None:
        stale_after = STALE_AFTER  # looked up per call, so tests and ops can patch it
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


def prepare_extraction(tender, *, converter=None, stale_after=None):
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
    """The extraction, provided the document was converted and sectioned (its plan has groups).
    That stays true after the model has been asked, so this works for PREPARED and every later outcome."""
    extraction = TenderExtraction.objects.filter(tender=tender).first()
    if extraction is None or "groups" not in (extraction.plan or {}):
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



# --------------------------------------------------------------------------- #
# asking the model (§6 "Extraction")
# --------------------------------------------------------------------------- #


def _claim_reading(tender, stale_after):
    """Take the tender for the model call. Returns (extraction, claimed). The call costs money, so
    two overlapping cron runs must never both make it: the tender row is locked while the claim
    is written, and only a PREPARED (or stale READING) extraction can be claimed."""
    if stale_after is None:
        stale_after = STALE_AFTER  # looked up per call, so tests and ops can patch it
    with transaction.atomic(), audit_actor(None):
        locked = Tender.objects.select_for_update().get(pk=tender.pk)
        if locked.status != TenderStatus.EXTRACTING:
            raise TransitionError(f"extract_fields needs a tender in EXTRACTING, not {locked.status}.", code="r19")
        extraction = TenderExtraction.objects.filter(tender=locked).select_for_update().first()
        if extraction is None:
            raise TransitionError("This tender has not been prepared for extraction.", code="not_prepared")
        if extraction.outcome == ExtractionOutcome.PREPARED:
            extraction.outcome = ExtractionOutcome.READING
            extraction.save()
            return extraction, True
        stale = (
            extraction.outcome == ExtractionOutcome.READING
            and extraction.updated_at < timezone.now() - stale_after
        )
        if stale:
            extraction.save()  # refreshes updated_at: this worker now owns the call
            return extraction, True
        return extraction, False  # finished, still converting, or being read by another worker


def _release(extraction):
    """Hand a claim back (the extractor is not configured): the next run, after the fix, picks it up."""
    with transaction.atomic(), audit_actor(None):
        current = TenderExtraction.objects.select_for_update().get(pk=extraction.pk)
        if current.outcome == ExtractionOutcome.READING:
            current.outcome = ExtractionOutcome.PREPARED
            current.save()


def _client_match(name):
    """A SUGGESTION only: the existing Client whose name or short name equals the extracted one."""
    if not name:
        return None
    found = list(Client.objects.filter(name__iexact=name)[:2]) or list(Client.objects.filter(short_name__iexact=name)[:2])
    return {"id": found[0].pk, "name": found[0].name} if len(found) == 1 else None


def _record(tender, extraction, *, outcome, message="", fields=None, raw_output="", usage=None, model_name=""):
    """Write the result and move the tender on. Only if the claim is still ours: a tender that was
    abandoned or finished meanwhile is not overwritten under the PM's feet."""
    with transaction.atomic(), audit_actor(None):
        current = TenderExtraction.objects.select_for_update().get(pk=extraction.pk)
        if current.outcome != ExtractionOutcome.READING:
            return current
        current.outcome, current.message = outcome, message
        current.fields, current.raw_output = fields or {}, raw_output
        current.usage, current.model_name = usage or {}, model_name
        current.extracted_at = timezone.now()
        current.save()
        locked = Tender.objects.select_for_update().get(pk=tender.pk)
        if locked.status == TenderStatus.EXTRACTING:  # not withdrawn meanwhile
            locked.finish_extraction(None)  # EXTRACTING -> PENDING_VERIFICATION, whatever the outcome (§5 step 6)
    tender.refresh_from_db()
    return current


def extract_fields(tender, extractor, *, stale_after=None):
    """
    Ask the model what the prepared tender says, and store the answer for the PM to verify.

    * The answer goes to ``TenderExtraction.fields`` and NOWHERE ELSE: no value reaches Tender (R12).
    * Every value passes ``finalize_fields`` first: quotes must be in the document, dates must be in
      their quote, boilerplate is never a value (§5: the clarification deadline and site visit are
      usually not stated, and are null).
    * Success, partial success and failure all end the same way: the tender moves to
      PENDING_VERIFICATION. A failure leaves the fields empty; it never traps the tender.
    * Not configured (no API key or model) is different: that is an operations problem, not a bad
      tender, so the claim is released and ``ExtractionConfigError`` raised.
    """
    extraction, claimed = _claim_reading(tender, stale_after)
    if not claimed:
        return extraction
    try:
        request = build_request(ready_sections(tender), extraction.plan)
        raw = extractor.extract(request)
    except ExtractionConfigError:
        _release(extraction)
        raise
    except Exception:  # an API error, bad JSON, anything: never trap a tender (R12)
        logger.exception("Extracting tender %s failed", tender.reference)
        return _record(tender, extraction, outcome=ExtractionOutcome.FAILED, message=MSG_EXTRACT_FAILED,
                       model_name=getattr(extractor, "name", ""))
    try:
        fields = extraction_fields.finalize_fields(
            raw.fields, request.page_texts(), toc_forms=forms_from_plan(extraction.plan))
        verdict = extraction_fields.classify(fields)
        match = _client_match((fields["client_name"] or {}).get("value"))
        if match:
            fields["client_match"] = {"value": match, "confidence": None, "source_page": None, "quote": None,
                                      "flags": [], "low_confidence": False, "source": "derived"}
    except Exception:
        logger.exception("Checking the extraction of tender %s failed", tender.reference)
        return _record(tender, extraction, outcome=ExtractionOutcome.FAILED, message=MSG_EXTRACT_FAILED,
                       raw_output=raw.raw_text, usage=raw.usage, model_name=raw.model or extractor.name)
    outcome = getattr(ExtractionOutcome, verdict)
    message = {"EXTRACTED": "", "PARTIAL": MSG_PARTIAL, "FAILED": MSG_NOTHING_READ}[verdict]
    return _record(tender, extraction, outcome=outcome, message=message, fields=fields,
                   raw_output=raw.raw_text, usage=raw.usage, model_name=raw.model or extractor.name)


def process_tender(tender, *, converter=None, extractor=None, stale_after=None):
    """What the cron command runs per tender: convert and section it, then ask the model.
    Safe to call twice, or from two processes at once: each stage is claimed before it starts."""
    extraction = prepare_extraction(tender, converter=converter, stale_after=stale_after)
    if extraction.outcome == ExtractionOutcome.PREPARED and extractor is not None:
        extraction = extract_fields(tender, extractor, stale_after=stale_after)
    return extraction


# --------------------------------------------------------------------------- #
# the safety net
# --------------------------------------------------------------------------- #

STUCK_AFTER = timedelta(minutes=30)


def abandon_extraction(tender, message=MSG_STUCK):
    """Finish a tender's extraction as FAILED, with empty fields, and move it to PENDING_VERIFICATION.
    A no-op if it is no longer EXTRACTING or its extraction already finished."""
    with transaction.atomic(), audit_actor(None):
        locked = Tender.objects.select_for_update().get(pk=tender.pk)
        if locked.status != TenderStatus.EXTRACTING:
            return None
        extraction = TenderExtraction.objects.select_for_update().filter(tender=locked).first()
        if extraction is None:
            extraction = TenderExtraction(tender=locked)
        elif extraction.outcome in FINISHED_OUTCOMES:
            return extraction
        extraction.outcome, extraction.message = ExtractionOutcome.FAILED, message
        extraction.fields, extraction.extracted_at = {}, timezone.now()
        extraction.save()
        locked.finish_extraction(None)
    tender.refresh_from_db()
    return extraction


def fail_stuck(*, stuck_after=STUCK_AFTER, now=None):
    """§5 step 6: a tender must never be trapped in EXTRACTING. Anything still there ``stuck_after``
    after its upload (a crashed worker, an outage, a missing API key) is finished with empty
    fields so the PM can type it in. Returns the tenders it moved."""
    cutoff = (now or timezone.now()) - stuck_after
    moved = []
    for tender in Tender.objects.filter(status=TenderStatus.EXTRACTING, created_at__lt=cutoff):
        if abandon_extraction(tender) is not None:
            moved.append(tender)
    return moved
