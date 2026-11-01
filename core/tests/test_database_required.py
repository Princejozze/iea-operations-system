"""
CLAUDE.md section 1: PostgreSQL is required. DATABASE_URL has no default; if it
is unset, startup fails loudly with an instruction to set it. SQLite is never a
fallback.

Settings are evaluated once at import, so each case runs a fresh interpreter.
The child ignores .env (read_env is stubbed) so that only the environment it is
given decides the outcome, whatever this machine's .env contains.
"""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

BASE_DIR = Path(settings.BASE_DIR)

CHILD = textwrap.dedent(
    """
    import os
    import environ
    environ.Env.read_env = classmethod(lambda cls, *a, **k: None)
    os.environ["SECRET_KEY"] = "not-a-real-key"
    os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings"
    import config.settings as s
    print("ENGINE=" + s.DATABASES["default"]["ENGINE"])
    """
)


def start_settings(database_url):
    """Import settings in a fresh interpreter. None = DATABASE_URL absent."""
    env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
    if database_url is not None:
        env["DATABASE_URL"] = database_url
    return subprocess.run(
        [sys.executable, "-c", CHILD], cwd=BASE_DIR, env=env, capture_output=True, text=True, timeout=60
    )


class DatabaseUrlIsRequiredTests(SimpleTestCase):
    def assertRefused(self, result, *fragments):
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("ImproperlyConfigured", result.stderr)
        for fragment in fragments:
            self.assertIn(fragment, result.stderr)

    def test_unset_stops_startup_and_says_how_to_fix_it(self):
        result = start_settings(None)
        self.assertRefused(result, "DATABASE_URL is not set", "postgres://", ".env")

    def test_empty_is_treated_as_unset(self):
        self.assertRefused(start_settings(""), "DATABASE_URL is not set")
        self.assertRefused(start_settings("   "), "DATABASE_URL is not set")

    def test_sqlite_is_refused_not_used_as_a_fallback(self):
        before = (BASE_DIR / "db.sqlite3").exists()
        result = start_settings("sqlite:///db.sqlite3")
        self.assertRefused(result, "does not point at PostgreSQL", "no SQLite fallback")
        self.assertEqual((BASE_DIR / "db.sqlite3").exists(), before)

    def test_a_postgres_url_starts_normally(self):
        for url in ("postgres://u:p@localhost:5432/d", "postgresql://u:p@localhost:5432/d"):
            with self.subTest(url=url):
                result = start_settings(url)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("ENGINE=django.db.backends.postgresql", result.stdout)

    def test_this_run_is_on_postgresql(self):
        # The suite itself must be running on PostgreSQL, never silently elsewhere.
        from django.db import connection

        self.assertEqual(connection.vendor, "postgresql")
