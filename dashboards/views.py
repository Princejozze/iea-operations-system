"""
dashboards — app 12 in CLAUDE.md §1.

Views and templates only — NO MODELS. Aggregates every other app, which is why the home
page lives here and not in accounts: accounts (app 2) may not import tenders (app 5).
"""

from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from accounts.models import Role

from . import queries


@login_required
def dashboard(request):
    user = request.user
    if user.role == Role.EXECUTIVE:
        return render(request, "dashboards/md.html", {
            "signature_queue": queries.signature_queue(user),
            "registrations": queries.pending_registrations(user),
            "eligibility": queries.open_eligibility_reviews(),
            "funds": queries.pending_fund_requests(),
            "at_risk": queries.tenders_at_risk(user),
            "at_risk_days": queries.AT_RISK_DAYS,
        })
    if user.role == Role.PROJECT_MANAGER:
        tenders = queries.pm_open_tenders(user)
        return render(request, "dashboards/pm.html", {
            "hero": queries.nearest_deadline(tenders),
            "tenders": tenders,
            "outstanding": queries.quotes_outstanding(user),
            "returned": queries.returned_to_pm(user),
        })
    counts = {}
    if user.role in (Role.AUDITOR, Role.HEAD_OF_OPERATIONS):
        counts["audits"] = queries.audit_waiting_count()
    if user.role == Role.FINANCE:
        counts["to_pay"] = queries.finance_to_pay_count()
    return render(request, "dashboards/home.html", {"counts": counts})
