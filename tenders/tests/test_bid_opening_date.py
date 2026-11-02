"""Tender.bid_opening_date: empty means "at the submission deadline"; never before it."""

import datetime

from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.test import TestCase

from tenders.models import TenderStatus as S

from .helpers import TenderFixtures, confirmed_fields


class BidOpeningDateTests(TenderFixtures, TestCase):
    def setUp(self):
        self.tender = self.new_tender(S.DRAFTING, **confirmed_fields())

    def test_defaults_to_the_submission_deadline(self):
        self.assertIsNone(self.tender.bid_opening_date)
        self.assertEqual(self.tender.opening_at, self.tender.submission_deadline)

    def test_a_stated_later_opening_is_used(self):
        later = self.tender.submission_deadline + datetime.timedelta(days=1)
        self.tender.bid_opening_date = later
        self.tender.full_clean()
        self.tender.save()
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.opening_at, later)

    def test_follows_an_extended_deadline_when_empty(self):
        """Why the deadline is not copied in: an extension must move the opening too."""
        extended = self.tender.submission_deadline + datetime.timedelta(days=7)
        self.tender.submission_deadline = extended
        self.tender.save()
        self.assertEqual(self.tender.opening_at, extended)

    def test_opening_before_the_deadline_is_refused(self):
        self.tender.bid_opening_date = self.tender.submission_deadline - datetime.timedelta(hours=1)
        with self.assertRaises(ValidationError) as ctx:
            self.tender.clean()
        self.assertIn("bid_opening_date", ctx.exception.error_dict)
        with self.assertRaises(ValidationError):
            self.tender.save()

    def test_database_constraint(self):
        with self.assertRaises(IntegrityError), transaction.atomic(), connection.cursor() as cur:
            cur.execute(
                "UPDATE tenders_tender SET bid_opening_date = submission_deadline - interval '1 hour' WHERE id = %s",
                [self.tender.pk],
            )
