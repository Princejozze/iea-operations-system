"""CLAUDE.md §1: documents renders from a plain dict and imports no domain model or enum."""

import ast
from pathlib import Path

from django.test import SimpleTestCase

from documents.models import Document

# Everything below documents in §1's order, plus the apps that hold domain enums.
BELOW = {"suppliers", "tenders", "items", "audits", "approvals", "postaward", "comms", "tasks", "dashboards"}


class IndependenceTests(SimpleTestCase):
    def test_no_module_imports_an_app_below_it(self):
        root = Path(__file__).resolve().parent.parent
        checked = 0
        for path in root.rglob("*.py"):
            if "migrations" in path.parts:
                continue
            checked += 1
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    names = [node.module]
                for name in names:
                    with self.subTest(file=path.name, name=name):
                        self.assertNotIn(name.split(".")[0], BELOW)
        self.assertGreater(checked, 5)

    def test_document_has_no_foreign_key_to_a_domain_model(self):
        targets = {
            f.related_model._meta.label
            for f in Document._meta.get_fields()
            if f.is_relation and not f.auto_created
        }
        self.assertEqual(targets, {"accounts.User"})
