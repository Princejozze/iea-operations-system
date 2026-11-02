"""Fixtures shared by the alert-command tests in tenders, items and approvals.

Kept here (not in tenders/tests/helpers.py) so the alert work touches only
management/ directories. Lower apps may import it: it is upward for them (§1).
"""

import datetime

from django.apps import apps
from django.core import mail
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from accounts.models import Role, User
from core.models import AuditEntry
from tenders.models import Tender


def make_user(role, name, **extra):
    extra.setdefault("must_change_password", False)
    return User.objects.create_user(
        email=f"{name}@imperialeastafrica.tz", password="x", role=role,
        first_name=name.title(), last_name="Test", **extra,
    )


def local_noon(days_from_today):
    """A deadline at 12:00 local time, ``days_from_today`` calendar days away,
    so T-n tests never straddle midnight."""
    day = timezone.localdate() + datetime.timedelta(days=days_from_today)
    return timezone.make_aware(datetime.datetime.combine(day, datetime.time(12, 0)))


def set_state(tender, status, **fields):
    """TEST-ONLY: put a tender straight into ``status``. Production code changes
    status only through the transition methods (R19)."""
    tender.refresh_from_db()
    tender.status = status
    for name, value in fields.items():
        setattr(tender, name, value)
    with tender._system_write():
        tender.save()
    tender.refresh_from_db()
    return tender


def add_item(tender, **fields):
    fields.setdefault("specification", "Laboratory centrifuge, 12 x 15 ml")
    fields.setdefault("quantity", 2)
    return apps.get_model("items", "Item").objects.create(tender=tender, **fields)


def ledger(kind, tender=None):
    qs = AuditEntry.objects.filter(model=f"alert.{kind}")
    if tender is not None:
        qs = qs.filter(object_id=str(tender.pk))
    return qs


class AlertTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.md = make_user(Role.EXECUTIVE, "faraja")
        cls.hops = make_user(Role.HEAD_OF_OPERATIONS, "mariam")
        cls.pm = make_user(Role.PROJECT_MANAGER, "gift")
        cls.pm2 = make_user(Role.PROJECT_MANAGER, "alice")
        cls.auditor = make_user(Role.AUDITOR, "sylvia")
        cls.finance = make_user(Role.FINANCE, "anna")
        cls.admin_user = make_user(Role.ADMIN, "janeth")

    def new_tender(self, status, *, pm=None, title="Supply of laboratory centrifuges", **fields):
        tender = Tender.objects.create_from_upload(uploaded_by=pm or self.pm)
        return set_state(tender, status, title=title, **fields)

    def run_command(self, name, *args):
        mail.outbox = []
        call_command(name, *args, stdout=_Sink(), stderr=_Sink())
        return list(mail.outbox)


class _Sink:
    def __init__(self):
        self.text = ""

    def write(self, s, *a, **k):
        self.text += s

    def flush(self):
        pass
