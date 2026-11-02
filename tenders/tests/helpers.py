import datetime

from django.apps import apps
from django.db import connection
from django.utils import timezone

from accounts.models import Role, User
from core.models import AuditAction, AuditEntry, Client
from tenders.models import BidSecurityType, Tender


def make_user(role, name, **extra):
    extra.setdefault("must_change_password", False)
    return User.objects.create_user(
        email=f"{name}@imperialeastafrica.tz", password="x", role=role,
        first_name=name.title(), last_name="Test", **extra,
    )


def confirmed_fields(client=None):
    deadline = timezone.now() + datetime.timedelta(days=21)
    return dict(
        title="Supply of laboratory centrifuges",
        client=client or Client.objects.get_or_create(name="Muhimbili National Hospital", short_name="MNH")[0],
        official_reference="MNH/2026/G/017",
        submission_deadline=deadline,
        clarification_deadline=deadline - datetime.timedelta(days=7),
        bid_security_type=BidSecurityType.BANK_GUARANTEE,
        site_visit_required=False,
    )


def force_status(tender, status, **fields):
    """TEST-ONLY shortcut to a state whose real gate lives in an app that does
    not exist yet (approvals, items, audits). Production code never does this:
    status is written only by Tender's transition methods (R19)."""
    tender.refresh_from_db()
    tender.status = status
    for name, value in fields.items():
        setattr(tender, name, value)
    with tender._system_write():
        tender.save()
    tender.refresh_from_db()
    return tender


def backdate_updated_at(instance, *, hours=1):
    """TEST-ONLY: move ``updated_at`` into the past so staleness checks do not depend on the
    clock advancing (Windows ticks ~15.6ms, so a zero threshold is flaky). Raw SQL because
    queryset.update() is refused on audited models (R7); updated_at is excluded from the trail."""
    table = instance._meta.db_table
    with connection.cursor() as cur:
        cur.execute(
            f'UPDATE "{table}" SET updated_at = %s WHERE id = %s',
            [timezone.now() - datetime.timedelta(hours=hours), instance.pk],
        )


def add_item(tender, **fields):
    """An item for ``tender``, so start_costing's item check (§5) can pass. Resolved
    with apps.get_model because tenders may not import items (§1: no downward imports)."""
    fields.setdefault("specification", "Laboratory centrifuge, 12 x 15 ml")
    fields.setdefault("quantity", 2)
    return apps.get_model("items", "Item").objects.create(tender=tender, **fields)


def status_entries(tender):
    return AuditEntry.objects.filter(
        model="tenders.Tender", object_id=str(tender.pk), action=AuditAction.STATUS_CHANGE
    ).order_by("id")


class TenderFixtures:
    """Mixin: the cast of users every tender test needs."""

    @classmethod
    def setUpTestData(cls):
        cls.md = make_user(Role.EXECUTIVE, "faraja")
        cls.hops = make_user(Role.HEAD_OF_OPERATIONS, "mariam")
        cls.pm = make_user(Role.PROJECT_MANAGER, "gift")
        cls.pm2 = make_user(Role.PROJECT_MANAGER, "alice")
        cls.auditor = make_user(Role.AUDITOR, "sylvia")
        cls.finance = make_user(Role.FINANCE, "anna")
        cls.admin_user = make_user(Role.ADMIN, "janeth", is_staff=True, is_superuser=True)

    def new_tender(self, status=None, **fields):
        tender = Tender.objects.create_from_upload(uploaded_by=self.pm)
        if status is not None:
            force_status(tender, status, **fields)
        return tender
