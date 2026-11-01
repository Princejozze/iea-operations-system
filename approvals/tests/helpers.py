"""Shared setup for approvals tests. The cast of users comes from tenders.tests.helpers."""

from decimal import Decimal

from django.utils import timezone

from approvals.models import Approval, FundRequest, FundRequestPurpose
from tenders.models import Currency, TenderStatus as S
from tenders.tests.helpers import TenderFixtures, force_status, make_user, status_entries  # noqa: F401


def sign(tender, kind, decision, user, *, comments="", password="x", typed_name=None):
    """The MD signs: typed full name + re-entered password (make_user sets the password to "x")."""
    return Approval.objects.sign(
        tender, kind, decision, user,
        typed_name=user.get_full_name() if typed_name is None else typed_name,
        password=password, comments=comments,
    )


def pending_executive_approval(case, tender=None):
    """A tender awaiting the MD's submission decision, with its rate lock set (R4)."""
    tender = tender or case.new_tender()
    return force_status(tender, S.PENDING_EXECUTIVE_APPROVAL, exchange_rate_locked_at=timezone.now())


def fund_request(tender, pm, *, purpose=FundRequestPurpose.NEST_APPLICATION_FEE, amount="50000.00"):
    return FundRequest.objects.create(
        tender=tender, purpose=purpose, amount=Decimal(amount), currency=Currency.TZS, requested_by=pm
    )


def paid_fund_request(case, tender, *, purpose=FundRequestPurpose.NEST_APPLICATION_FEE):
    """Run a request through PM → MD → Finance, the only route to PAID."""
    fr = fund_request(tender, case.pm, purpose=purpose)
    fr.approve(case.md)
    fr.mark_paid(case.finance, "BANK-REF-001")
    return fr
