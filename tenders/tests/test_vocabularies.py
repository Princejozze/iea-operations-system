"""§8: BidSecurityType replaces bid_security_required + free text; category is an FK to core."""

from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.models import ProtectedError
from django.test import SimpleTestCase, TestCase

from core.models import TenderCategory
from tenders.models import BidSecurityType, Tender, TenderStatus as S, TransitionError

from .helpers import TenderFixtures, confirmed_fields


class BidSecurityTypeVocabularyTests(SimpleTestCase):
    def test_values_are_exactly_those_in_section_8(self):
        self.assertEqual(
            set(BidSecurityType.values),
            {
                "NOT_APPLICABLE", "TENDER_SECURING_DECLARATION", "BANK_LETTER_PROOF_OF_FUNDS",
                "BANK_GUARANTEE", "INSURANCE_BOND",
            },
        )

    def test_there_is_no_separate_required_flag(self):
        names = {f.name for f in Tender._meta.get_fields()}
        self.assertNotIn("bid_security_required", names)
        self.assertIn("bid_security_type", names)

    def test_field_is_an_enum_not_free_text(self):
        field = Tender._meta.get_field("bid_security_type")
        self.assertEqual([v for v, _ in field.choices], BidSecurityType.values)
        self.assertGreaterEqual(field.max_length, max(len(v) for v in BidSecurityType.values))


class BidSecurityTypeBehaviourTests(TenderFixtures, TestCase):
    def setUp(self):
        self.tender = self.new_tender(S.PENDING_VERIFICATION)

    def test_every_value_can_be_confirmed(self):
        for value in BidSecurityType.values:
            with self.subTest(value=value):
                tender = self.new_tender(S.PENDING_VERIFICATION)
                fields = {**confirmed_fields(), "bid_security_type": value}
                tender.confirm_extraction(self.pm, **fields)
                self.assertEqual(tender.bid_security_type, value)

    def test_not_applicable_is_a_real_answer_not_an_omission(self):
        self.tender.confirm_extraction(
            self.pm, **{**confirmed_fields(), "bid_security_type": BidSecurityType.NOT_APPLICABLE}
        )
        self.assertEqual(self.tender.status, S.PENDING_REGISTRATION_APPROVAL)

    def test_the_pm_must_choose_one_before_the_tender_goes_to_the_md(self):
        fields = confirmed_fields()
        del fields["bid_security_type"]
        with self.assertRaises(TransitionError) as ctx:
            self.tender.confirm_extraction(self.pm, **fields)
        self.assertIn("bid_security_type", ctx.exception.message_dict)
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.status, S.PENDING_VERIFICATION)

    def test_free_text_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.tender.confirm_extraction(
                self.pm, **{**confirmed_fields(), "bid_security_type": "Bank guarantee"}
            )
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.status, S.PENDING_VERIFICATION)

    def test_database_rejects_a_value_outside_the_enum(self):
        with self.assertRaises(IntegrityError), transaction.atomic(), connection.cursor() as cur:
            cur.execute(
                "UPDATE tenders_tender SET bid_security_type = 'CASH' WHERE id = %s", [self.tender.pk]
            )

    def test_blank_means_unverified_and_is_allowed_before_confirmation(self):
        self.assertEqual(self.new_tender().bid_security_type, "")


class TenderCategoryFkTests(TenderFixtures, TestCase):
    def test_category_is_a_foreign_key_to_the_core_lookup_table(self):
        field = Tender._meta.get_field("category")
        self.assertTrue(field.is_relation)
        self.assertIs(field.related_model, TenderCategory)

    def test_pm_confirms_a_category_and_it_is_audited(self):
        tender = self.new_tender(S.PENDING_VERIFICATION)
        category = TenderCategory.objects.get(name="CLINICAL LAB")
        tender.confirm_extraction(self.pm, **{**confirmed_fields(), "category": category})
        self.assertEqual(tender.category, category)
        entry = tender.__class__.objects.get(pk=tender.pk)
        self.assertEqual(entry.category.name, "CLINICAL LAB")

    def test_category_is_optional_at_confirmation(self):
        tender = self.new_tender(S.PENDING_VERIFICATION)
        tender.confirm_extraction(self.pm, **confirmed_fields())
        self.assertIsNone(tender.category)

    def test_a_category_added_at_runtime_is_usable_without_a_migration(self):
        category = TenderCategory.objects.create(name="Cold Chain")
        tender = self.new_tender(S.PENDING_VERIFICATION)
        tender.confirm_extraction(self.pm, **{**confirmed_fields(), "category": category})
        self.assertEqual(tender.category.name, "COLD CHAIN")

    def test_a_category_in_use_cannot_be_deleted(self):
        tender = self.new_tender(S.PENDING_VERIFICATION)
        category = TenderCategory.objects.get(name="ANALYTICAL")
        tender.confirm_extraction(self.pm, **{**confirmed_fields(), "category": category})
        with self.assertRaises(ProtectedError):
            category.delete()
