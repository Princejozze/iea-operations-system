"""core.management.alerts.send_once: idempotency, overlap safety, retry on failure."""

import threading
from unittest import mock

from django.core import mail
from django.db import connections

from core.management import alerts
from core.models import AuditEntry
from tenders.management.tests.alert_fixtures import AlertTestCase, ledger
from tenders.models import TenderStatus as S

KIND = "TEST_ALERT"


class SendOnceTests(AlertTestCase):
    def setUp(self):
        self.tender = self.new_tender(S.COSTING)
        mail.outbox = []

    def send(self, **kw):
        kw.setdefault("recipients", [self.pm.email])
        return alerts.send_once(kind=KIND, tender=self.tender, subject="s", body="b", **kw)

    def test_sends_once_then_records(self):
        self.assertEqual(self.send(), alerts.SENT)
        self.assertEqual(self.send(), alerts.ALREADY_SENT)
        self.assertEqual(len(mail.outbox), 1)
        entry = ledger(KIND, self.tender).get()
        self.assertIsNone(entry.actor)
        self.assertEqual(entry.changes["recipients"], [None, [self.pm.email]])
        self.assertEqual(entry.changes["for_date"][1], alerts.local_today().isoformat())

    def test_different_kinds_and_tenders_are_independent(self):
        other = self.new_tender(S.COSTING)
        self.assertEqual(self.send(), alerts.SENT)
        self.assertEqual(alerts.send_once(kind="OTHER", tender=self.tender, recipients=["x@y.tz"],
                                          subject="s", body="b"), alerts.SENT)
        self.assertEqual(alerts.send_once(kind=KIND, tender=other, recipients=["x@y.tz"],
                                          subject="s", body="b"), alerts.SENT)

    def test_yesterdays_alert_does_not_block_today(self):
        from datetime import timedelta
        from django.utils import timezone
        from core.models import AuditAction
        AuditEntry.objects.create(action=AuditAction.CREATE, model=f"alert.{KIND}",
                                  object_id=str(self.tender.pk),
                                  timestamp=alerts.start_of_local_day(alerts.local_today()) - timedelta(minutes=1))
        self.assertEqual(self.send(), alerts.SENT)
        self.assertLess(ledger(KIND, self.tender).order_by("timestamp").first().timestamp, timezone.now())

    def test_no_recipients_sends_and_records_nothing(self):
        self.assertEqual(self.send(recipients=[]), alerts.NO_RECIPIENTS)
        self.assertFalse(ledger(KIND, self.tender).exists())

    def test_failed_send_records_nothing_and_is_retried(self):
        with mock.patch("core.management.alerts.EmailMessage.send", side_effect=OSError("SMTP down")):
            with self.assertRaises(OSError):
                self.send()
        self.assertFalse(ledger(KIND, self.tender).exists())
        self.assertEqual(self.send(), alerts.SENT)
        self.assertEqual(ledger(KIND, self.tender).count(), 1)

    def test_overlapping_run_holding_the_lock_makes_this_one_skip(self):
        """A second process mid-send holds the advisory lock: we must neither
        wait for it nor send a duplicate."""
        key = alerts._lock_key(KIND, self.tender.pk)
        locked, release = threading.Event(), threading.Event()

        def other_run():
            conn = connections.create_connection("default")
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_lock(%s)", [key])
                    locked.set()
                    release.wait(10)
                    cur.execute("SELECT pg_advisory_unlock(%s)", [key])
            finally:
                conn.close()

        t = threading.Thread(target=other_run)
        t.start()
        try:
            self.assertTrue(locked.wait(10))
            self.assertEqual(self.send(), alerts.BUSY)
            self.assertEqual(mail.outbox, [])
            self.assertFalse(ledger(KIND, self.tender).exists())
        finally:
            release.set()
            t.join(10)
        self.assertEqual(self.send(), alerts.SENT)

    def test_clean_recipients(self):
        self.pm2.is_active = False
        self.assertEqual(
            alerts.clean_recipients([self.pm, None, self.pm2, self.md, self.pm]),
            [self.pm.email, self.md.email],
        )


class AlertCommandFailureTests(AlertTestCase):
    def test_one_failure_does_not_stop_the_rest_and_exits_nonzero(self):
        from django.core.management import CommandError
        from tenders.management.tests.alert_fixtures import local_noon
        a = self.new_tender(S.SUBMITTED, submission_deadline=local_noon(-1))
        b = self.new_tender(S.SUBMITTED, submission_deadline=local_noon(-2))
        real_send = alerts.EmailMessage.send
        calls = []

        def flaky(msg, *args, **kwargs):
            calls.append(msg.subject)
            if len(calls) == 1:
                raise OSError("SMTP hiccup")
            return real_send(msg, *args, **kwargs)

        with mock.patch("core.management.alerts.EmailMessage.send", flaky):
            with self.assertRaises(CommandError):
                self.run_command("alert_award_capture")
        self.assertEqual(len(calls), 2)
        self.assertEqual(ledger("AWARD_CAPTURE").count(), 1)
        self.assertEqual(len(mail.outbox), 1)
        # Next run retries only the one that failed.
        self.assertEqual(len(self.run_command("alert_award_capture")), 1)
        self.assertEqual(ledger("AWARD_CAPTURE").count(), 2)
        self.assertEqual({e.object_id for e in ledger("AWARD_CAPTURE")}, {str(a.pk), str(b.pk)})
