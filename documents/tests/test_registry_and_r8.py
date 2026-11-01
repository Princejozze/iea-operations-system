"""
The Document registry and R8: RFQs always cc sales@imperialeastafrica.tz and
no caller, form, admin or raw SQL can take it off. Runs on PostgreSQL.
"""

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.test import RequestFactory, TestCase

from core.models import AuditAction, AuditBypassError, AuditEntry
from documents.models import Document, DocumentKind
from documents.rfq import SALES_CC, normalise_recipients, with_required_cc
from documents.services import build_email, create_document

from .test_rendering import rfq_context

SUPPLIER = "sales@mettlertoledo.example"


class RecipientRuleTests(TestCase):
    def test_sales_is_added_first_when_absent(self):
        self.assertEqual(with_required_cc([]), [SALES_CC])
        self.assertEqual(with_required_cc(["pm@imperialeastafrica.tz"]), [SALES_CC, "pm@imperialeastafrica.tz"])

    def test_sales_is_not_duplicated_whatever_its_case(self):
        self.assertEqual(with_required_cc(["Sales@ImperialEastAfrica.tz", "a@x.example"]), [SALES_CC, "a@x.example"])

    def test_addresses_are_stripped_deduplicated_and_blanks_dropped(self):
        self.assertEqual(
            normalise_recipients([" a@x.example ", "A@X.example", "", "  "]), ["a@x.example"]
        )

    def test_invalid_and_header_injecting_addresses_are_rejected(self):
        for bad in ("not-an-address", "a@x.example\nBcc: evil@example.com", "a@x.example,b@x.example"):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                normalise_recipients([bad])


class CreateDocumentTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            email="gift@imperialeastafrica.tz", password="x", role="PM"
        )

    def make(self, **kwargs):
        kwargs.setdefault("to", [SUPPLIER])
        return create_document(DocumentKind.RFQ, rfq_context(), user=self.user, **kwargs)

    def test_registers_the_rendered_document(self):
        doc = self.make()
        doc.refresh_from_db()
        self.assertEqual(doc.kind, "RFQ")
        self.assertEqual(doc.title, "Request for Quotation - IEA-2026-0042")
        self.assertEqual(doc.related_reference, "IEA-2026-0042")
        self.assertEqual(doc.to, [SUPPLIER])
        self.assertEqual(doc.context["supplier_name"], "Mettler Toledo")
        self.assertIn("Autoclave 100 L", doc.body_text)
        self.assertIn("<table", doc.body_html)
        self.assertEqual(doc.created_by, self.user)

    def test_creation_is_audited_with_the_actor_and_without_bulky_bodies(self):
        doc = self.make()
        entry = AuditEntry.objects.get(model="documents.Document", object_id=str(doc.pk))
        self.assertEqual(entry.action, AuditAction.CREATE)
        self.assertEqual(entry.actor, self.user)
        self.assertEqual(entry.changes["cc"], [None, [SALES_CC]])
        self.assertNotIn("body_html", entry.changes)
        self.assertNotIn("body_text", entry.changes)

    def test_cc_always_contains_sales_even_when_the_caller_passes_none(self):
        for cc in ((), [], None, ["pm@imperialeastafrica.tz"]):
            with self.subTest(cc=cc):
                self.assertEqual(self.make(cc=cc).cc[0], SALES_CC)

    def test_a_bad_context_registers_nothing(self):
        with self.assertRaises(ValidationError):
            create_document(DocumentKind.RFQ, {"items": []}, user=self.user)
        self.assertEqual(Document.objects.count(), 0)

    def test_a_bad_recipient_registers_nothing(self):
        with self.assertRaises(ValidationError):
            self.make(to=["nope"])
        self.assertEqual(Document.objects.count(), 0)

    def test_no_user_means_the_system(self):
        doc = create_document(DocumentKind.RFQ, rfq_context(), to=[SUPPLIER])
        self.assertIsNone(doc.created_by)
        self.assertIsNone(AuditEntry.objects.get(model="documents.Document", object_id=str(doc.pk)).actor)


class R8CannotBeRemovedTests(TestCase):
    def setUp(self):
        self.doc = create_document(DocumentKind.RFQ, rfq_context(), to=[SUPPLIER])

    def test_model_layer_save_restores_it(self):
        self.doc.cc = []
        self.doc.save()
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.cc, [SALES_CC])

    def test_model_layer_clean_restores_it(self):
        self.doc.cc = ["pm@imperialeastafrica.tz"]
        self.doc.full_clean(exclude=["created_by"])
        self.assertEqual(self.doc.cc, [SALES_CC, "pm@imperialeastafrica.tz"])

    def test_database_refuses_raw_sql_that_removes_it(self):
        with self.assertRaises(IntegrityError), transaction.atomic(), connection.cursor() as cur:
            cur.execute("UPDATE documents_document SET cc = '[]'::jsonb WHERE id = %s", [self.doc.pk])

    def test_database_refuses_a_raw_insert_of_an_rfq_without_it(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Document.objects.bulk_create(
                [Document(kind="RFQ", title="x", cc=["a@x.example"], context={})]
            )

    def test_queryset_update_is_refused_by_the_audit_layer(self):
        with self.assertRaises(AuditBypassError):
            Document.objects.filter(pk=self.doc.pk).update(cc=[])

    def test_the_emailed_message_has_it_even_if_the_stored_row_was_tampered_with(self):
        # Simulate a row written before the constraint existed / by a superuser
        # with the constraint dropped: build_email re-applies R8 regardless.
        self.doc.cc = []
        message = build_email(self.doc)
        self.assertEqual(message.cc, [SALES_CC])

    def test_built_email_carries_text_html_and_recipients(self):
        message = build_email(self.doc)
        self.assertEqual(message.to, [SUPPLIER])
        self.assertEqual(message.cc[0], SALES_CC)
        self.assertEqual(message.subject, "Request for Quotation - IEA-2026-0042")
        self.assertIn("Autoclave 100 L", message.body)
        self.assertEqual(message.alternatives[0].mimetype, "text/html")
        self.assertIn(SALES_CC, message.recipients())  # actually delivered to, not just listed

    def test_admin_cannot_add_change_or_delete_documents(self):
        model_admin = admin.site._registry[Document]
        user = get_user_model().objects.create_superuser(
            email="root@imperialeastafrica.tz", password="x", role="ADMIN"
        )
        request = RequestFactory().get("/")
        request.user = user
        self.assertFalse(model_admin.has_add_permission(request))
        self.assertFalse(model_admin.has_change_permission(request, self.doc))
        self.assertFalse(model_admin.has_delete_permission(request, self.doc))
