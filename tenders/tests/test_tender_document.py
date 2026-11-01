"""§7: TenderDocument.kind is TENDER_PDF | TECHNICAL_SPEC | SUPPORTING."""

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.test import SimpleTestCase, TestCase

from core.models import AuditAction, AuditEntry
from tenders.models import TenderDocument, TenderDocumentKind as K

from .helpers import TenderFixtures


class TenderDocumentKindVocabularyTests(SimpleTestCase):
    def test_values_are_exactly_those_in_section_7(self):
        self.assertEqual(set(K.values), {"TENDER_PDF", "TECHNICAL_SPEC", "SUPPORTING"})

    def test_field_is_an_enum_that_fits(self):
        field = TenderDocument._meta.get_field("kind")
        self.assertEqual([v for v, _ in field.choices], K.values)
        self.assertGreaterEqual(field.max_length, max(len(v) for v in K.values))


class TenderDocumentTests(TenderFixtures, TestCase):
    def make(self, kind, **extra):
        tender = extra.pop("tender", None) or self.new_tender()
        return TenderDocument.objects.create(
            tender=tender, kind=kind, uploaded_by=self.pm, original_filename="doc.pdf",
            file=ContentFile(b"%PDF-1.4", name="doc.pdf"), **extra,
        )

    def test_every_kind_can_be_stored_and_read_back(self):
        tender = self.new_tender()
        for kind in K.values:
            with self.subTest(kind=kind):
                self.make(kind, tender=tender)
        self.assertEqual({d.kind for d in tender.documents.all()}, set(K.values))

    def test_the_tender_pdf_row_carries_the_markdown(self):
        doc = self.make(K.TENDER_PDF, markdown="<!-- page 1 -->\n# Tender")
        doc.refresh_from_db()
        self.assertIn("page 1", doc.markdown)

    def test_an_unknown_kind_is_refused(self):
        doc = TenderDocument(tender=self.new_tender(), kind="BROCHURE", uploaded_by=self.pm,
                             file=ContentFile(b"x", name="x.pdf"))
        with self.assertRaises(ValidationError):
            doc.full_clean()
        with self.assertRaises(IntegrityError), transaction.atomic():
            doc.save()  # and the database says no on its own

    def test_a_blank_kind_is_refused(self):
        doc = TenderDocument(tender=self.new_tender(), kind="", uploaded_by=self.pm,
                             file=ContentFile(b"x", name="x.pdf"))
        with self.assertRaises(ValidationError):
            doc.full_clean()

    def test_creating_one_is_audited(self):
        doc = self.make(K.SUPPORTING)
        entry = AuditEntry.objects.get(model="tenders.TenderDocument", object_id=str(doc.pk))
        self.assertEqual(entry.action, AuditAction.CREATE)
        self.assertEqual(entry.changes["kind"], [None, K.SUPPORTING])
