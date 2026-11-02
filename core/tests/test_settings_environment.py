"""
Production-only settings (docs/DEPLOY.md §2): on when ENVIRONMENT=production, off in
development, and refused when misconfigured. Plus the logging that makes a production 500
leave a record, without secrets in it.

Settings are evaluated once, at import, so each case imports config.settings afresh in a
subprocess with a controlled environment. Every variable that matters is set explicitly
(an empty string counts as set), so neither the developer's .env nor the shell can change
the outcome: environ.Env.read_env never overwrites a variable that is already present.
"""

import json
import logging
import os
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path

from django.conf import settings
from django.test import RequestFactory, SimpleTestCase

from config.log import REDACTED, RedactingFormatter, redact, request_context

BASE_DIR = Path(settings.BASE_DIR)
STRONG_KEY = secrets.token_urlsafe(50)

DUMP = r"""
import json, logging, sys
import config.settings as s
handlers = s.LOGGING["handlers"]
print(json.dumps({
    "ENVIRONMENT": s.ENVIRONMENT,
    "PRODUCTION": s.PRODUCTION,
    "DEBUG": s.DEBUG,
    "SECURE_PROXY_SSL_HEADER": getattr(s, "SECURE_PROXY_SSL_HEADER", None),
    "CSRF_TRUSTED_ORIGINS": s.CSRF_TRUSTED_ORIGINS,
    "SESSION_COOKIE_SECURE": getattr(s, "SESSION_COOKIE_SECURE", None),
    "CSRF_COOKIE_SECURE": getattr(s, "CSRF_COOKIE_SECURE", None),
    "SECURE_SSL_REDIRECT": getattr(s, "SECURE_SSL_REDIRECT", None),
    "SECURE_HSTS_SECONDS": getattr(s, "SECURE_HSTS_SECONDS", None),
    "SECURE_HSTS_INCLUDE_SUBDOMAINS": getattr(s, "SECURE_HSTS_INCLUDE_SUBDOMAINS", None),
    "handlers": sorted(handlers),
    "log_file": handlers.get("file", {}).get("filename"),
}))
"""


class SubprocessSettings:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log_file = str(Path(self.tmp.name) / "app.log")

    def tearDown(self):
        self.tmp.cleanup()

    def environment(self, **overrides):
        """A complete, explicit environment. Defaults are development."""
        env = dict(os.environ)
        env.update({
            "SECRET_KEY": STRONG_KEY,
            "DEBUG": "True",
            "ENVIRONMENT": "development",
            "ALLOWED_HOSTS": "localhost,127.0.0.1",
            "CSRF_TRUSTED_ORIGINS": "",
            "SECURE_HSTS_SECONDS": "",
            "LOG_FILE": "",
            "LOG_LEVEL": "INFO",
            "USE_S3": "False",
            "MEDIA_ROOT": self.tmp.name,
            "DATABASE_URL": settings_database_url(),
        })
        env.update(overrides)
        if env["SECURE_HSTS_SECONDS"] == "":
            del env["SECURE_HSTS_SECONDS"]  # unset, to see the default (an empty int fails to parse)
        return env

    def production(self, **overrides):
        return self.environment(**{
            "ENVIRONMENT": "production", "DEBUG": "False",
            "ALLOWED_HOSTS": "ops.imperialeastafrica.tz",
            "CSRF_TRUSTED_ORIGINS": "https://ops.imperialeastafrica.tz",
            "LOG_FILE": self.log_file,
            **overrides,
        })

    def run_python(self, code, env):
        return subprocess.run([sys.executable, "-c", code], cwd=BASE_DIR, env=env,
                              capture_output=True, text=True, timeout=120)

    def load(self, env):
        result = self.run_python(DUMP, env)
        if result.returncode:
            raise AssertionError(f"settings failed to load:\n{result.stderr[-2000:]}")
        return json.loads(result.stdout.strip().splitlines()[-1])

    def refused(self, env):
        result = self.run_python("import config.settings", env)
        self.assertNotEqual(result.returncode, 0, "settings loaded but should have been refused")
        self.assertIn("ImproperlyConfigured", result.stderr)
        return result.stderr


def settings_database_url():
    """The current DATABASE_URL, rebuilt from settings (the subprocess only parses it)."""
    db = settings.DATABASES["default"]
    name = db["NAME"].removeprefix("test_")
    return f"postgres://{db['USER']}:{db['PASSWORD']}@{db['HOST'] or 'localhost'}:{db['PORT'] or 5432}/{name}"


class ProductionSettingsTests(SubprocessSettings, SimpleTestCase):
    def test_production_turns_on_the_proxy_header_secure_cookies_redirect_and_hsts(self):
        s = self.load(self.production())
        self.assertEqual((s["ENVIRONMENT"], s["PRODUCTION"], s["DEBUG"]), ("production", True, False))
        self.assertEqual(s["SECURE_PROXY_SSL_HEADER"], ["HTTP_X_FORWARDED_PROTO", "https"])
        self.assertEqual(s["CSRF_TRUSTED_ORIGINS"], ["https://ops.imperialeastafrica.tz"])
        self.assertIs(s["SESSION_COOKIE_SECURE"], True)
        self.assertIs(s["CSRF_COOKIE_SECURE"], True)
        self.assertIs(s["SECURE_SSL_REDIRECT"], True)
        self.assertEqual(s["SECURE_HSTS_SECONDS"], 3600)  # an hour to start (docs/DEPLOY.md §2)
        self.assertIs(s["SECURE_HSTS_INCLUDE_SUBDOMAINS"], False)  # never all of *.imperialeastafrica.tz
        self.assertEqual(s["handlers"], ["file", "stderr"])
        self.assertEqual(s["log_file"], self.log_file)

    def test_hsts_follows_the_environment(self):
        self.assertEqual(self.load(self.production(SECURE_HSTS_SECONDS="31536000"))["SECURE_HSTS_SECONDS"], 31536000)

    def test_development_leaves_every_production_setting_off(self):
        for env in (self.environment(), self.environment(ENVIRONMENT="")):  # explicit, and implied by DEBUG
            s = self.load(env)
            with self.subTest(ENVIRONMENT=env["ENVIRONMENT"]):
                self.assertEqual((s["ENVIRONMENT"], s["PRODUCTION"]), ("development", False))
                for name in ("SECURE_PROXY_SSL_HEADER", "SESSION_COOKIE_SECURE", "CSRF_COOKIE_SECURE",
                             "SECURE_SSL_REDIRECT", "SECURE_HSTS_SECONDS"):
                    self.assertIsNone(s[name], name)
                self.assertEqual(s["handlers"], ["stderr"])  # no file unless LOG_FILE is set

    def test_development_may_log_to_a_file_too(self):
        self.assertEqual(self.load(self.environment(LOG_FILE=self.log_file))["handlers"], ["file", "stderr"])

    def test_misconfiguration_is_refused_not_guessed(self):
        cases = {
            "production with DEBUG": self.production(DEBUG="True"),
            "production without CSRF origins": self.production(CSRF_TRUSTED_ORIGINS=""),
            "production with an http origin": self.production(CSRF_TRUSTED_ORIGINS="http://ops.imperialeastafrica.tz"),
            "production without LOG_FILE": self.production(LOG_FILE=""),
            "production with LOG_FILE in a missing directory": self.production(
                LOG_FILE=str(Path(self.tmp.name) / "missing" / "app.log")),
            "DEBUG off and no ENVIRONMENT": self.environment(DEBUG="False", ENVIRONMENT=""),
            "an unknown ENVIRONMENT": self.environment(ENVIRONMENT="staging"),
        }
        for label, env in cases.items():
            with self.subTest(label):
                self.refused(env)

    def test_check_deploy_passes_at_the_deploy_scripts_fail_level(self):
        """docs/DEPLOY.md §8 runs `check --deploy --fail-level WARNING` and refuses to deploy otherwise."""
        result = subprocess.run(
            [sys.executable, "manage.py", "check", "--deploy", "--fail-level", "WARNING"],
            cwd=BASE_DIR, env=self.production(), capture_output=True, text=True, timeout=180,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_check_deploy_fails_in_development(self):
        result = subprocess.run(
            [sys.executable, "manage.py", "check", "--deploy", "--fail-level", "WARNING"],
            cwd=BASE_DIR, env=self.environment(), capture_output=True, text=True, timeout=180,
        )
        self.assertNotEqual(result.returncode, 0)  # proves the production settings are what passes it


class ProductionLoggingTests(SubprocessSettings, SimpleTestCase):
    """A 500 in production leaves a record: in the file and on stderr, with the request
    and the traceback, and with no secret or password in it."""

    EMIT = r"""
import logging, os, django
os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings"
django.setup()
from django.conf import settings
from django.test import RequestFactory
request = RequestFactory().post(
    "/items/tender/7/save/?token=QUERYTOKEN123",
    {"password": "hunter2-plaintext", "unit_cost": "100"},
    HTTP_X_FORWARDED_FOR="196.41.32.10",
)
try:
    raise RuntimeError("could not connect to " + settings.DATABASES["default"]["PASSWORD"] + " via "
                       + "postgres://iea_app:dbpass-in-url@rds.example:5432/iea; key " + settings.SECRET_KEY)
except RuntimeError:
    # Exactly what django.core.handlers.exception.response_for_exception logs for a 500.
    logging.getLogger("django.request").error(
        "Internal Server Error: %s", request.path, exc_info=True,
        extra={"status_code": 500, "request": request},
    )
logging.getLogger("tenders.pipeline.service").info("extraction finished for IEA-2026-0042")
"""

    def emit(self):
        env = self.production(DATABASE_URL="postgres://iea_app:db-password-XYZ987@localhost:5432/iea")
        result = self.run_python(self.EMIT, env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return Path(self.log_file).read_text(encoding="utf-8"), result.stderr

    def test_a_500_is_logged_to_the_file_and_stderr_with_request_and_traceback(self):
        logged, stderr = self.emit()
        for output in (logged, stderr):
            with self.subTest(output="file" if output is logged else "stderr"):
                self.assertIn("ERROR django.request", output)
                self.assertIn("Internal Server Error: /items/tender/7/save/", output)
                self.assertIn("[POST /items/tender/7/save/ status=500 ip=196.41.32.10]", output)
                self.assertIn("Traceback (most recent call last)", output)
                self.assertIn("RuntimeError: could not connect", output)
                self.assertRegex(output, r"INFO tenders\.pipeline\.service pid=\d+ extraction finished")  # app INFO too

    def test_no_secret_or_password_reaches_the_log(self):
        logged, stderr = self.emit()
        for output in (logged, stderr):
            with self.subTest(output="file" if output is logged else "stderr"):
                for secret in (STRONG_KEY, "db-password-XYZ987", "dbpass-in-url", "hunter2-plaintext", "QUERYTOKEN123"):
                    self.assertNotIn(secret, output)
                self.assertIn(REDACTED, output)


class RedactionUnitTests(SimpleTestCase):
    def test_known_secret_shapes_are_scrubbed(self):
        cases = [
            ("postgres://iea_app:s3cr3t-pw@db:5432/iea", "postgres://iea_app:[redacted]@db:5432/iea"),
            ("password=abc123xyz next", "password=[redacted] next"),
            ("{'password': 'abc123xyz', 'email': 'a@b.tz'}", "{'password': '[redacted]', 'email': 'a@b.tz'}"),
            ('"api_key": "AIzaSyD-example"', '"api_key": "[redacted]"'),
            ("Authorization: Bearer", "Authorization: [redacted]"),
            ("csrfmiddlewaretoken=Zt9", "csrfmiddlewaretoken=[redacted]"),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(redact(raw), expected)

    def test_configured_secret_values_are_scrubbed_anywhere(self):
        self.assertNotIn(settings.SECRET_KEY, redact(f"boom: {settings.SECRET_KEY} in a message"))

    def test_ordinary_text_is_left_alone(self):
        text = "Tender IEA-2026-0042 moved to COSTING by user 7; 12 items priced."
        self.assertEqual(redact(text), text)

    def test_request_context_never_includes_the_query_string_or_body(self):
        request = RequestFactory().post("/login/?next=/secret-place/", {"password": "pw"}, REMOTE_ADDR="10.0.0.5")
        record = logging.LogRecord("django.request", logging.WARNING, __file__, 1, "Forbidden", None, None)
        record.request, record.status_code = request, 403
        self.assertEqual(request_context(record), " [POST /login/ status=403 ip=10.0.0.5]")
        line = RedactingFormatter("%(message)s%(request_context)s").format(record)
        self.assertEqual(line, "Forbidden [POST /login/ status=403 ip=10.0.0.5]")

    def test_a_record_without_a_request_gets_no_context(self):
        record = logging.LogRecord("tenders", logging.INFO, __file__, 1, "hello", None, None)
        self.assertEqual(RedactingFormatter("%(message)s%(request_context)s").format(record), "hello")
