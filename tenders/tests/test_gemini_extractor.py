"""
GeminiExtractor against a STUB client: no network, no API key, no cost. What matters here is
that the model and key come from the environment (never from source), that the request is
shaped as intended, and that every kind of bad answer becomes an ExtractionError.
"""

import json
import re
from pathlib import Path
from types import SimpleNamespace

from django.conf import settings
from django.test import SimpleTestCase

from tenders.pipeline import fields as F
from tenders.pipeline.extractor import (
    DocumentExtractor,
    ExtractionConfigError,
    ExtractionError,
    ExtractionRequest,
    ExtractionUnavailable,
    GeminiExtractor,
    RequestPart,
)

SECRET = "AIza-this-is-a-fake-key-1234567890"
MODEL = "test-model-from-env"


def request(names=("title", "submission_deadline")):
    return ExtractionRequest(
        parts=(RequestPart("TENDER DATA SHEET", ((7, 8),), "\n<!-- page 7 -->\nDeadline 13/11/2026\n"),),
        field_names=tuple(names),
    )


class StubModels:
    def __init__(self, response=None, exc=None):
        self.response, self.exc, self.calls = response, exc, []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc:
            raise self.exc
        return self.response


def stub_client(response=None, exc=None):
    return SimpleNamespace(models=StubModels(response, exc))


def response(text, usage=None):
    return SimpleNamespace(text=text, usage_metadata=usage, prompt_feedback=None)


USAGE = SimpleNamespace(prompt_token_count=4100, candidates_token_count=310, thoughts_token_count=40,
                        cached_content_token_count=0, total_token_count=4450)


class Configuration(SimpleTestCase):
    def test_the_model_and_key_come_from_the_environment(self):
        ex = GeminiExtractor.from_environment({"GEMINI_API_KEY": SECRET, "GEMINI_MODEL": MODEL})
        self.assertEqual((ex.model, ex.api_key, ex.name), (MODEL, SECRET, f"gemini:{MODEL}"))

    def test_a_missing_model_is_a_clear_configuration_error(self):
        for env in ({"GEMINI_API_KEY": SECRET}, {"GEMINI_API_KEY": SECRET, "GEMINI_MODEL": "  "}):
            with self.subTest(env=sorted(env)), self.assertRaises(ExtractionConfigError) as ctx:
                GeminiExtractor.from_environment(env)
            self.assertIn("GEMINI_MODEL", str(ctx.exception))

    def test_a_missing_key_is_a_clear_configuration_error(self):
        with self.assertRaises(ExtractionConfigError) as ctx:
            GeminiExtractor.from_environment({"GEMINI_MODEL": MODEL})
        self.assertIn("GEMINI_API_KEY", str(ctx.exception))

    def test_nothing_in_the_environment_is_a_configuration_error_not_a_crash(self):
        with self.assertRaises(ExtractionConfigError):
            GeminiExtractor.from_environment({})

    def test_a_configuration_error_is_an_extraction_error(self):
        self.assertTrue(issubclass(ExtractionConfigError, ExtractionError))

    def test_the_real_client_can_be_built_offline_with_retries_and_a_timeout(self):
        # Building the SDK client makes no request. This proves the options are accepted by the SDK.
        client = GeminiExtractor(api_key=SECRET, model=MODEL, timeout_seconds=90)._get_client()
        self.assertEqual(type(client).__name__, "Client")

    def test_no_model_identifier_is_hardcoded_anywhere_in_the_source(self):
        # §4: model names rot. The identifier comes from GEMINI_MODEL, never from source.
        offenders = []
        for path in (Path(settings.BASE_DIR) / "tenders").rglob("*.py"):
            if "tests" in path.parts or "migrations" in path.parts:
                continue
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if re.search(r"gemini-[0-9a-z]", line, re.I):
                    offenders.append(f"{path.name}:{n}: {line.strip()}")
        self.assertEqual(offenders, [])


class TheRequest(SimpleTestCase):
    def call(self, **kw):
        client = stub_client(response(json.dumps({"title": {"value": "T"}}), USAGE))
        ex = GeminiExtractor(api_key=SECRET, model=MODEL, client=client, **kw)
        ex.extract(request())
        return client.models.calls[0]

    def test_the_configured_model_is_the_one_asked(self):
        self.assertEqual(self.call()["model"], MODEL)

    def test_the_prompt_carries_the_sections_with_their_page_markers(self):
        sent = self.call()["contents"]
        self.assertIn("Extract these fields: title, submission_deadline.", sent)
        self.assertIn("=== PART 1: TENDER DATA SHEET (pages 7-8) ===", sent)
        self.assertIn("<!-- page 7 -->", sent)

    def test_the_config_asks_for_deterministic_json_under_the_rules(self):
        config = self.call()["config"]
        self.assertEqual(config.temperature, 0)
        self.assertEqual(config.response_mime_type, "application/json")
        self.assertEqual(config.system_instruction, F.SYSTEM_INSTRUCTION)
        self.assertEqual(set(config.response_schema["properties"]), {"title", "submission_deadline"})

    def test_the_output_limit_is_configurable(self):
        self.assertEqual(self.call(max_output_tokens=1234)["config"].max_output_tokens, 1234)

    def test_the_api_key_is_never_part_of_the_request_content(self):
        sent = self.call()
        self.assertNotIn(SECRET, json.dumps({k: str(v) for k, v in sent.items()}))


class TheAnswer(SimpleTestCase):
    def extractor(self, **kw):
        return GeminiExtractor(api_key=SECRET, model=MODEL, client=stub_client(**kw))

    def test_a_good_answer_is_returned_with_usage_and_the_raw_text(self):
        text = json.dumps({"title": {"value": "Lab equipment", "confidence": 0.9, "source_page": 7, "quote": "x"}})
        raw = self.extractor(response=response(text, USAGE)).extract(request())
        self.assertEqual(raw.fields["title"]["value"], "Lab equipment")
        self.assertEqual(raw.raw_text, text)
        self.assertEqual(raw.model, MODEL)
        self.assertEqual(raw.usage, {"prompt_tokens": 4100, "output_tokens": 310, "thoughts_tokens": 40,
                                     "cached_tokens": 0, "total_tokens": 4450})

    def test_missing_usage_is_an_empty_dict_not_an_error(self):
        raw = self.extractor(response=response("{}", None)).extract(request())
        self.assertEqual(raw.usage, {})

    def test_every_kind_of_bad_answer_is_an_extraction_error(self):
        bad = {
            "no text": response(None),
            "empty text": response(""),
            "not json": response("here is your answer: {title: T}"),
            "cut off": response('{"title": {"value": "Lab equi'),
            "a json list": response("[1, 2]"),
            "a json string": response('"hello"'),
        }
        for label, resp in bad.items():
            with self.subTest(label), self.assertRaises(ExtractionError):
                self.extractor(response=resp).extract(request())

    def test_a_blocked_prompt_says_so(self):
        resp = SimpleNamespace(text=None, usage_metadata=None, prompt_feedback=SimpleNamespace(block_reason="SAFETY"))
        with self.assertRaises(ExtractionError) as ctx:
            self.extractor(response=resp).extract(request())
        self.assertIn("SAFETY", str(ctx.exception))

    def test_any_api_failure_becomes_an_extraction_error_without_leaking_internals_or_the_key(self):
        class FakeApiError(Exception):
            code = 503

        for exc in (FakeApiError(f"503 UNAVAILABLE key={SECRET}"), TimeoutError(SECRET), ConnectionError("reset")):
            with self.subTest(exc=type(exc).__name__), self.assertRaises(ExtractionError) as ctx:
                self.extractor(exc=exc).extract(request())
            self.assertNotIn(SECRET, str(ctx.exception))
            self.assertIs(ctx.exception.__cause__, exc)  # the cause is kept for the log

    def test_a_busy_or_unreachable_service_is_unavailable_not_a_failure(self):
        def api_error(status):
            return type("FakeApiError", (Exception,), {"code": status})(f"{status} from the API")

        import httpx
        for exc in (api_error(503), api_error(429), api_error(500), TimeoutError(), ConnectionError("reset"),
                    httpx.ReadTimeout("slow"), httpx.ConnectError("refused")):
            with self.subTest(exc=repr(exc)), self.assertRaises(ExtractionUnavailable):
                self.extractor(exc=exc).extract(request())

    def test_a_refused_request_is_a_plain_failure_not_worth_retrying(self):
        for status in (400, 403, 404):
            exc = type("FakeApiError", (Exception,), {"code": status})("no")
            with self.subTest(status=status), self.assertRaises(ExtractionError) as ctx:
                self.extractor(exc=exc).extract(request())
            self.assertNotIsInstance(ctx.exception, ExtractionUnavailable)

    def test_it_is_a_document_extractor(self):
        self.assertIsInstance(self.extractor(response=response("{}")), DocumentExtractor)
