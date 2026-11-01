"""
The twelve-app layout of CLAUDE.md §1, enforced.

Built for parallel work: two sessions in different app directories must not be
able to break each other silently. These tests fail if anyone
  - imports an app from below their own (the dependency rule),
  - reorders or drops an app in INSTALLED_APPS,
  - forgets to wire an app's urls.py, or
  - gives `dashboards` a model.
They deliberately do NOT assert that the Phase 2-3 apps are empty — that would
break the moment someone builds them.
"""

import ast
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.test import SimpleTestCase
from django.urls import get_resolver

# CLAUDE.md §1 table, in dependency order. An app may import only from apps ABOVE it.
APP_ORDER = [
    "core", "accounts", "documents", "suppliers", "tenders", "items",
    "audits", "approvals", "postaward", "comms", "tasks", "dashboards",
]
URL_PREFIXES = {app: f"{app}/" for app in APP_ORDER if app not in ("core", "accounts")}


class InstalledAppsTests(SimpleTestCase):
    def test_all_twelve_apps_installed_in_dependency_order(self):
        local = [a for a in settings.INSTALLED_APPS if a in APP_ORDER]
        self.assertEqual(local, APP_ORDER)

    def test_no_apps_outside_the_list(self):
        base = Path(settings.BASE_DIR)
        candidates = {
            p.parent.name
            for p in base.glob("*/apps.py")
            if ".venv" not in p.parts
        }
        self.assertEqual(candidates, set(APP_ORDER), "CLAUDE.md §1: do not create apps outside the list.")

    def test_every_app_config_resolves(self):
        for app in APP_ORDER:
            with self.subTest(app=app):
                self.assertEqual(apps.get_app_config(app).name, app)

    def test_dashboards_has_no_models(self):
        self.assertEqual(list(apps.get_app_config("dashboards").get_models()), [])


class UrlWiringTests(SimpleTestCase):
    def test_each_app_is_included_under_its_own_namespace(self):
        namespaces = get_resolver().namespace_dict
        for app in URL_PREFIXES:
            with self.subTest(app=app):
                self.assertIn(app, namespaces)
                self.assertEqual(namespaces[app][0], URL_PREFIXES[app])

    def test_each_app_owns_a_urls_module_exposing_urlpatterns(self):
        for app in URL_PREFIXES:
            with self.subTest(app=app):
                module = __import__(f"{app}.urls", fromlist=["urlpatterns"])
                self.assertEqual(module.app_name, app)
                self.assertIsInstance(module.urlpatterns, list)


def _imported_top_level_modules(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module.split(".")[0]


class DependencyRuleTests(SimpleTestCase):
    """§1: 'An app may only import from apps above it.' Migrations are skipped:
    they reference other apps by name string, and historical-model imports there
    are legitimate."""

    def test_no_app_imports_from_an_app_below_it(self):
        base = Path(settings.BASE_DIR)
        violations = []
        for index, app in enumerate(APP_ORDER):
            below = set(APP_ORDER[index + 1:])
            for path in (base / app).rglob("*.py"):
                if "migrations" in path.parts:
                    continue
                for module in set(_imported_top_level_modules(path)) & below:
                    violations.append(f"{path.relative_to(base)} imports {module} (below {app})")
        self.assertEqual(violations, [], "\n".join(violations))

    def test_the_checker_actually_detects_a_violation(self):
        # Guard against the test above passing because it checks nothing.
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.py"
            bad.write_text("from items.models import Item\nimport django\n", encoding="utf-8")
            found = set(_imported_top_level_modules(bad))
        self.assertIn("items", found)
        self.assertIn("items", set(APP_ORDER[APP_ORDER.index("tenders") + 1:]))
