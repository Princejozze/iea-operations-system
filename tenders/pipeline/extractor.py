"""
The model call (CLAUDE.md §6 "Extraction"): a ``DocumentExtractor`` interface and the
Gemini implementation behind it.

* The model identifier and API key come ONLY from the environment (``GEMINI_MODEL``,
  ``GEMINI_API_KEY``). Nothing in this package names a model: §4 says model names rot.
* An extractor returns the model's raw answer. It does NOT decide what is believed:
  ``fields.finalize_fields`` re-checks every value against the text that was sent (R12).
* Only what is needed goes over the wire (§6: accuracy and cost). The metadata section and the
  eligibility criteria are sent; the goods schedule and specifications are parsed locally and
  the forms are named by the table of contents. A required section that could not be located
  is replaced by the whole document with the TOC as a hint.
"""

import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from . import fields as F
from .sections import Group, ReadySection


class ExtractionError(Exception):
    """The model could not be asked or its answer could not be used."""


class ExtractionConfigError(ExtractionError):
    """The extractor is not configured (no API key or model). An operations problem, not a bad tender."""


# --------------------------------------------------------------------------- #
# what is sent
# --------------------------------------------------------------------------- #

_LABELS = {
    Group.METADATA: "TENDER DATA SHEET and/or INVITATION (tender details, deadlines, bid security)",
    Group.ELIGIBILITY: "QUALIFICATION AND EVALUATION CRITERIA (eligibility and qualification requirements)",
    Group.FORMS: "TENDERING FORMS",
}


@dataclass(frozen=True)
class RequestPart:
    label: str
    ranges: tuple
    text: str  # Markdown with <!-- page N --> markers (raw text for pages whose tables shredded)


@dataclass(frozen=True)
class ExtractionRequest:
    parts: tuple
    field_names: tuple
    toc_hint: str = ""  # only when the whole document is sent

    def prompt(self):
        lines = ["Extract these fields: " + ", ".join(self.field_names) + "."]
        if self.toc_hint:
            lines += ["", "The document's table of contents (page numbers are PDF pages), to help you find things:",
                      self.toc_hint]
        for i, part in enumerate(self.parts, start=1):
            pages = ", ".join(f"{a}-{b}" if a != b else str(a) for a, b in part.ranges)
            lines += ["", f"=== PART {i}: {part.label} (pages {pages}) ===", part.text]
        return "\n".join(lines)

    def page_texts(self):
        """{page: normalised text} of exactly what was sent: the guard checks quotes against this."""
        return F.page_texts([p.text for p in self.parts])

    @property
    def est_input_tokens(self):
        """ESTIMATE (characters / 4) of the prompt plus the system instruction."""
        return -(-(len(self.prompt()) + len(F.SYSTEM_INSTRUCTION)) // 4)


def forms_from_plan(plan):
    """[(form name, TOC page)]: the forms the table of contents names inside TENDERING FORMS (§6 step 4)."""
    section = plan.get("sections", {}).get("FORMS")
    if not section:
        return []
    first, last = section["pages"]
    return [(e["name"], e["page"]) for e in plan["toc"]["entries"] if e["level"] == "sub" and first < e["page"] <= last]


def build_request(sections, plan):
    """Choose what to send, from ``service.ready_sections``. Returns an ``ExtractionRequest``."""
    ready = {s.group: s for s in sections}
    needed = [Group.METADATA, Group.ELIGIBILITY]
    if not forms_from_plan(plan):
        needed.append(Group.FORMS)  # no usable TOC list: ask the model to read the forms section
    names = tuple(s.name for s in F.FIELDS if set(s.groups) & set(needed))
    names = tuple(n for n in names if not (n == "required_forms" and Group.FORMS not in needed))

    whole = [ready[g] for g in needed if ready[g].method == "whole_document"]
    if whole:  # one pass over the whole document replaces every part that could not be located
        doc: ReadySection = whole[0]
        return ExtractionRequest(
            parts=(RequestPart("WHOLE DOCUMENT", doc.ranges, doc.text),), field_names=names, toc_hint=doc.toc_hint)
    parts = tuple(RequestPart(_LABELS[g], ready[g].ranges, ready[g].text) for g in needed)
    return ExtractionRequest(parts=parts, field_names=names)


@dataclass
class RawExtraction:
    """What an extractor returns: the model's answer, untrusted."""

    fields: dict
    usage: dict = field(default_factory=dict)  # prompt_tokens, output_tokens, thoughts_tokens, total_tokens
    raw_text: str = ""
    model: str = ""


class DocumentExtractor(ABC):
    name = "abstract"

    @abstractmethod
    def extract(self, request):
        """Return a ``RawExtraction``. Raise ``ExtractionError`` if the model cannot be used;
        ``ExtractionConfigError`` if the extractor is not configured."""


# --------------------------------------------------------------------------- #
# Gemini
# --------------------------------------------------------------------------- #

RETRY_STATUS_CODES = [429, 500, 502, 503, 504]


class GeminiExtractor(DocumentExtractor):
    """google-genai. ``client`` can be injected (the tests do) so no call ever leaves the machine."""

    def __init__(self, *, api_key=None, model=None, client=None, timeout_seconds=180, max_output_tokens=8192):
        self.model = (model or "").strip()
        self.api_key = api_key
        self._client = client
        self.timeout_seconds = timeout_seconds
        self.max_output_tokens = max_output_tokens
        if not self.model:
            raise ExtractionConfigError("GEMINI_MODEL is not set. Choose a current Flash-tier model and set it in the environment.")
        if client is None and not api_key:
            raise ExtractionConfigError("GEMINI_API_KEY is not set.")

    @classmethod
    def from_environment(cls, environ=None):
        env = os.environ if environ is None else environ
        return cls(api_key=(env.get("GEMINI_API_KEY") or "").strip(), model=(env.get("GEMINI_MODEL") or "").strip())

    @property
    def name(self):
        return f"gemini:{self.model}"

    def _get_client(self):
        if self._client is None:
            from google import genai
            from google.genai import types

            self._client = genai.Client(
                api_key=self.api_key,
                http_options=types.HttpOptions(
                    timeout=self.timeout_seconds * 1000,
                    retry_options=types.HttpRetryOptions(
                        attempts=3, initial_delay=2.0, max_delay=20.0, http_status_codes=RETRY_STATUS_CODES),
                ),
            )
        return self._client

    def _config(self, request):
        from google.genai import types

        return types.GenerateContentConfig(
            system_instruction=F.SYSTEM_INSTRUCTION,
            temperature=0,
            max_output_tokens=self.max_output_tokens,
            response_mime_type="application/json",
            response_schema=F.response_schema(request.field_names),
        )

    def extract(self, request):
        client = self._get_client()
        try:
            response = client.models.generate_content(
                model=self.model, contents=request.prompt(), config=self._config(request))
        except Exception as exc:  # the SDK raises its own APIError family and httpx errors
            code = getattr(exc, "code", None)
            raise ExtractionError(f"The Gemini request failed ({type(exc).__name__}{f' {code}' if code else ''}).") from exc
        text = getattr(response, "text", None)
        if not text:
            reason = getattr(getattr(response, "prompt_feedback", None), "block_reason", None)
            raise ExtractionError(f"Gemini returned no text{f' (blocked: {reason})' if reason else ''}.")
        try:
            parsed = json.loads(text)
        except ValueError as exc:
            raise ExtractionError("Gemini's answer was not valid JSON (possibly cut off).") from exc
        if not isinstance(parsed, dict):
            raise ExtractionError("Gemini's answer was not a JSON object.")
        return RawExtraction(fields=parsed, usage=_usage(response), raw_text=text, model=self.model)


def _usage(response):
    u = getattr(response, "usage_metadata", None)
    if u is None:
        return {}
    pick = lambda n: getattr(u, n, None) or 0  # noqa: E731
    return {"prompt_tokens": pick("prompt_token_count"), "output_tokens": pick("candidates_token_count"),
            "thoughts_tokens": pick("thoughts_token_count"), "cached_tokens": pick("cached_content_token_count"),
            "total_tokens": pick("total_token_count")}
