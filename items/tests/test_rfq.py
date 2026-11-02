"""
RFQ dispatch (§5 step 12) and R8: every RFQ is copied to sales@imperialeastafrica.tz.
The system adds it; the user cannot remove it.
"""

import datetime

from django.core import mail
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from django.utils import timezone

from core.models import AuditAction, AuditEntry
from documents.models import Document
from items.models import RFQ
from items.services.rfq import send_rfq
from suppliers.models import Supplier
from tenders.models import TenderStatus as S, TransitionError

from .helpers import ItemFixtures, force_status

SALES = "sales@imperialeastafrica.tz"


class RFQFixtures(ItemFixtures):
    def setUp(self):
        super().setUp()
        self.supplier.email = "tenders@eppendorf.example"
        self.supplier.contact_person = "Jana Weber"
        self.supplier.save()
        self.tender = self.costing()
        self.items = [
            self.make_item(self.tender, specification="Benchtop centrifuge, 24 x 1.5 ml", quantity=2,
                           unit_of_measure="each"),
            self.make_item(self.tender, specification="Rotor F-45-24-11", quantity=4),
        ]

    def send(self, **kwargs):
        kwargs.setdefault("items", self.items)
        kwargs.setdefault("user", self.pm)
        return send_rfq(self.tender, kwargs.pop("supplier", self.supplier), kwargs.pop("items"), kwargs.pop("user"),
                        **kwargs)


class SendTests(RFQFixtures, TestCase):
    def test_the_rfq_is_sent_registered_and_recorded(self):
        rfq = self.send(reply_by=datetime.date(2026, 11, 20), message="Delivery to Dar es Salaam.")

        self.assertEqual(len(mail.outbox), 1)
        email = mail.outbox[0]
        self.assertEqual(email.to, ["tenders@eppendorf.example"])
        self.assertEqual(email.cc, [SALES])
        self.assertEqual(email.reply_to, [self.pm.email])
        self.assertIn(self.tender.reference, email.subject)
        for item in self.items:
            self.assertIn(item.item_reference, email.body)
        self.assertIn("20 November 2026", email.body)
        self.assertEqual(email.extra_headers["Message-ID"], rfq.message_id)
        self.assertTrue(rfq.message_id.endswith("@imperialeastafrica.tz>"))
        self.assertEqual(email.attachments[0][2], "application/pdf")

        self.assertEqual((rfq.tender, rfq.supplier, rfq.sent_by), (self.tender, self.supplier, self.pm))
        self.assertEqual(set(rfq.items.all()), set(self.items))
        self.assertEqual(list(self.items[0].rfqs.all()), [rfq])
        self.assertEqual(rfq.document.kind, "RFQ")
        self.assertEqual(rfq.document.related_reference, self.tender.reference)
        self.assertEqual(rfq.document.created_by, self.pm)
        created = AuditEntry.objects.get(model="items.RFQ", object_id=str(rfq.pk), action=AuditAction.CREATE)
        self.assertEqual(created.actor, self.pm)

    def test_r8_sales_is_added_and_cannot_be_removed(self):
        for cc in ([], ["SALES@ImperialEastAfrica.tz"], ["gift@imperialeastafrica.tz"]):
            with self.subTest(cc=cc):
                mail.outbox.clear()
                rfq = self.send(cc=cc)
                sent = [address.lower() for address in mail.outbox[0].cc]
                self.assertEqual(sent.count(SALES), 1)
                self.assertEqual(rfq.cc[0], SALES)
                self.assertIn(SALES, rfq.document.cc)
        self.assertIn("gift@imperialeastafrica.tz", mail.outbox[0].cc)

    def test_r8_the_database_refuses_an_rfq_row_without_sales(self):
        rfq = self.send()
        with self.assertRaises(IntegrityError), transaction.atomic(), connection.cursor() as cur:
            cur.execute("UPDATE items_rfq SET cc = '[]'::jsonb WHERE id = %s", [rfq.pk])

    def test_r8_saving_the_row_puts_sales_back(self):
        rfq = self.send()
        rfq.cc = ["someone@example.com"]
        rfq.save()
        rfq.refresh_from_db()
        self.assertEqual(rfq.cc, [SALES, "someone@example.com"])

    def test_an_explicit_recipient_overrides_the_supplier_email(self):
        self.send(to=["quotes@eppendorf.example"])
        self.assertEqual(mail.outbox[0].to, ["quotes@eppendorf.example"])

    def test_a_supplier_with_no_email_needs_a_recipient(self):
        nameless = Supplier.objects.create(name="Hettich")
        with self.assertRaises(ValidationError) as ctx:
            self.send(supplier=nameless)
        self.assertIn("to", ctx.exception.message_dict)
        self.assertEqual(mail.outbox, [])

    def test_a_bad_address_is_refused(self):
        with self.assertRaises(ValidationError):
            self.send(to=["not an address\nBcc: x@example.com"])
        self.assertEqual(mail.outbox, [])

    def test_items_must_be_given_and_belong_to_the_tender(self):
        with self.assertRaises(ValidationError):
            self.send(items=[])
        stranger = self.make_item(self.costing())
        with self.assertRaises(ValidationError):
            self.send(items=[self.items[0], stranger])
        self.assertFalse(RFQ.objects.exists())

    def test_a_failed_send_leaves_no_record_claiming_it_went(self):
        class Refusing:
            def send_messages(self, messages):
                raise ConnectionRefusedError("SMTP down")

        with self.assertRaises(ConnectionRefusedError):
            self.send(connection=Refusing())
        self.assertFalse(RFQ.objects.exists())
        self.assertFalse(Document.objects.exists())


class WhoAndWhenTests(RFQFixtures, TestCase):
    def test_only_this_tenders_pm_sends(self):
        for user in (self.pm2, self.md, self.auditor, self.finance):
            with self.subTest(user=user.email), self.assertRaises(PermissionDenied):
                self.send(user=user)
        self.assertEqual(mail.outbox, [])

    def test_rfqs_go_out_only_from_costing(self):
        force_status(self.tender, S.DRAFTING)
        with self.assertRaises(TransitionError):
            self.send()
        self.assertEqual(mail.outbox, [])

    def test_r9a_no_rfq_while_frozen(self):
        force_status(self.tender, S.PENDING_ELIGIBILITY_DECISION, previous_status=S.COSTING)
        with self.assertRaises(TransitionError):
            self.send()
        self.assertEqual(mail.outbox, [])

    def test_r15_no_rfq_after_costing_has_moved_on(self):
        for status in (S.PENDING_AUDIT, S.WITHDRAWN, S.SUBMITTED):
            force_status(self.tender, status, exchange_rate_locked_at=timezone.now())
            with self.subTest(status=status), self.assertRaises(TransitionError):
                self.send()
        self.assertEqual(mail.outbox, [])
