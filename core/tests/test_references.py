"""
R20 — references follow §4 exactly and concurrent uploads cannot collide.
"""

import datetime
import threading
import time
from unittest import skipUnless

from django.db import connection, connections, transaction
from django.db.transaction import TransactionManagementError
from django.test import TestCase, TransactionTestCase

from core.models import ReferenceCounter
from core.references import (
    ReferenceSequenceExhausted,
    next_item_reference,
    next_tender_reference,
)

D2026 = datetime.date(2026, 11, 1)


class ReferenceFormatTests(TestCase):
    def test_tender_reference_format(self):
        with transaction.atomic():
            self.assertEqual(next_tender_reference(on_date=D2026), "IEA-2026-0001")
            self.assertEqual(next_tender_reference(on_date=D2026), "IEA-2026-0002")

    def test_sequence_restarts_each_calendar_year(self):
        with transaction.atomic():
            next_tender_reference(on_date=datetime.date(2026, 12, 31))
            self.assertEqual(
                next_tender_reference(on_date=datetime.date(2026, 12, 31)), "IEA-2026-0002"
            )
            self.assertEqual(
                next_tender_reference(on_date=datetime.date(2027, 1, 1)), "IEA-2027-0001"
            )

    def test_default_year_is_today_in_dar_es_salaam(self):
        from django.utils import timezone

        with transaction.atomic():
            ref = next_tender_reference()
        self.assertEqual(ref, f"IEA-{timezone.localdate().year}-0001")

    def test_item_reference_numbered_within_its_tender(self):
        with transaction.atomic():
            self.assertEqual(next_item_reference("IEA-2026-0042"), "IEA-2026-0042-001")
            self.assertEqual(next_item_reference("IEA-2026-0042"), "IEA-2026-0042-002")
            self.assertEqual(next_item_reference("IEA-2026-0043"), "IEA-2026-0043-001")

    def test_refuses_outside_a_transaction(self):
        # TestCase wraps each test in a transaction, so step outside it.
        conn = transaction.get_connection()
        saved = conn.in_atomic_block
        conn.in_atomic_block = False
        try:
            with self.assertRaises(TransactionManagementError):
                next_tender_reference(on_date=D2026)
        finally:
            conn.in_atomic_block = saved

    def test_rollback_releases_the_number(self):
        with self.assertRaises(RuntimeError), transaction.atomic():
            next_tender_reference(on_date=D2026)
            raise RuntimeError("upload failed")
        with transaction.atomic():
            self.assertEqual(next_tender_reference(on_date=D2026), "IEA-2026-0001")

    def test_tender_sequence_refuses_to_overflow_four_digits(self):
        ReferenceCounter.objects.create(key="tender:2026", last_value=9999)
        with self.assertRaises(ReferenceSequenceExhausted), transaction.atomic():
            next_tender_reference(on_date=D2026)

    def test_item_sequence_refuses_to_overflow_three_digits(self):
        ReferenceCounter.objects.create(key="item:IEA-2026-0001", last_value=999)
        with self.assertRaises(ReferenceSequenceExhausted), transaction.atomic():
            next_item_reference("IEA-2026-0001")


@skipUnless(
    connection.vendor == "postgresql",
    "select_for_update() row locking needs PostgreSQL; SQLite ignores it",
)
class ConcurrentReferenceTests(TransactionTestCase):
    """
    Real threads, real connections, real commits. Each worker takes a number
    and then holds its transaction open for a moment, so any worker that read
    the counter without waiting for the lock would get a duplicate.
    """

    WORKERS = 12
    HOLD_SECONDS = 0.15

    def _race(self, take_reference):
        barrier = threading.Barrier(self.WORKERS)
        results, errors = [], []
        lock = threading.Lock()

        def worker():
            try:
                barrier.wait(timeout=10)
                with transaction.atomic():
                    ref = take_reference()
                    time.sleep(self.HOLD_SECONDS)  # widen the race window
                with lock:
                    results.append(ref)
            except Exception as exc:  # surfaced in the assertion below
                with lock:
                    errors.append(repr(exc))
            finally:
                connections.close_all()

        threads = [threading.Thread(target=worker) for _ in range(self.WORKERS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        self.assertEqual(errors, [])
        return results

    def test_concurrent_tender_references_never_collide_when_counter_is_new(self):
        # No counter row exists yet: the workers also race to create it.
        refs = self._race(lambda: next_tender_reference(on_date=D2026))
        self.assertEqual(len(refs), self.WORKERS)
        self.assertEqual(len(set(refs)), self.WORKERS, f"collision in {sorted(refs)}")
        self.assertEqual(
            sorted(refs), [f"IEA-2026-{n:04d}" for n in range(1, self.WORKERS + 1)]
        )

    def test_concurrent_tender_references_never_collide_when_counter_exists(self):
        ReferenceCounter.objects.create(key="tender:2026", last_value=41)
        refs = self._race(lambda: next_tender_reference(on_date=D2026))
        self.assertEqual(len(set(refs)), self.WORKERS, f"collision in {sorted(refs)}")
        self.assertEqual(
            sorted(refs), [f"IEA-2026-{n:04d}" for n in range(42, 42 + self.WORKERS)]
        )

    def test_concurrent_item_references_never_collide(self):
        refs = self._race(lambda: next_item_reference("IEA-2026-0042"))
        self.assertEqual(len(set(refs)), self.WORKERS, f"collision in {sorted(refs)}")
        self.assertEqual(
            sorted(refs), [f"IEA-2026-0042-{n:03d}" for n in range(1, self.WORKERS + 1)]
        )
