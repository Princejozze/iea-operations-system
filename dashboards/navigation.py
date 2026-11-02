"""
The one navigation bar every screen shares: which links a role sees, the counts on them,
and which one is the current section. Rendered by ``{% main_nav %}`` in base.html.

Brass on a badge means exactly one thing: the MD is the blocker. Every other badge is ink.
"""

from dataclasses import dataclass

from django.urls import reverse

from accounts.models import Role

from . import queries


@dataclass
class NavLink:
    key: str
    label: str
    url: str
    count: int = 0
    needs_md: bool = False
    active: bool = False


# (url namespace, url name prefix) -> nav section. First match wins.
SECTIONS = (
    ("", "dashboard", "dashboard"),
    ("tenders", "upload", "upload"),
    ("tenders", "", "tenders"),
    ("items", "", "tenders"),  # the item sheet and quotes are tender work
    ("audits", "", "audits"),
    ("approvals", "registration", "registrations"),
    ("approvals", "submission", "submissions"),
    ("approvals", "fund", "funds"),
    ("admin", "", "admin"),
)


def section_of(request):
    match = getattr(request, "resolver_match", None)
    if match is None:
        return ""
    namespace, name = match.namespace or "", match.url_name or ""
    for ns, prefix, section in SECTIONS:
        if namespace == ns and name.startswith(prefix):
            return section
    return ""


def links_for(user):
    """The role's links, in the order that role works through them (§2 roles)."""
    role = user.role
    url = reverse
    if role == Role.EXECUTIVE:
        c = queries.md_counts()
        return [
            NavLink("dashboard", "Dashboard", url("dashboard"), sum(c.values()), needs_md=True),
            NavLink("tenders", "Tenders", url("tenders:list")),
            NavLink("registrations", "Registrations", url("approvals:registration_queue"), c["registrations"], needs_md=True),
            NavLink("submissions", "Submissions", url("approvals:submission_queue"), c["submissions"], needs_md=True),
            NavLink("funds", "Fund requests", url("approvals:fund_list"), c["funds"], needs_md=True),
            NavLink("audits", "QC audits", url("audits:queue")),
        ]
    if role == Role.PROJECT_MANAGER:
        c = queries.pm_counts(user)
        return [
            NavLink("dashboard", "Dashboard", url("dashboard"), c["to_verify"] + c["returned"]),
            NavLink("tenders", "Tenders", url("tenders:list")),
            NavLink("upload", "Upload tender", url("tenders:upload")),
            NavLink("funds", "Fund requests", url("approvals:fund_list")),
        ]
    if role == Role.HEAD_OF_OPERATIONS:
        return [
            NavLink("dashboard", "Dashboard", url("dashboard")),
            NavLink("tenders", "Tenders", url("tenders:list")),
            NavLink("audits", "QC audits", url("audits:queue"), queries.audit_waiting_count()),
            NavLink("funds", "Fund requests", url("approvals:fund_list")),
        ]
    if role == Role.AUDITOR:
        return [
            NavLink("dashboard", "Dashboard", url("dashboard")),
            NavLink("audits", "QC audits", url("audits:queue"), queries.audit_waiting_count()),
            NavLink("tenders", "Tenders", url("tenders:list")),
        ]
    if role == Role.FINANCE:
        return [
            NavLink("dashboard", "Dashboard", url("dashboard")),
            NavLink("funds", "Fund requests", url("approvals:fund_list"), queries.finance_to_pay_count()),
            NavLink("tenders", "Tenders", url("tenders:list")),
        ]
    if role == Role.ADMIN:
        return [
            NavLink("dashboard", "Dashboard", url("dashboard")),
            NavLink("tenders", "Tenders", url("tenders:list")),
            NavLink("admin", "Administration", url("admin:index")),
        ]
    # CLEAR: post-award only (R5). Its screens arrive in Phase 2.
    return [NavLink("dashboard", "Dashboard", url("dashboard"))]


def nav_for(user, request):
    links = links_for(user)
    current = section_of(request)
    for link in links:
        link.active = link.key == current
    # Collapsed on a phone, the menu hides its badges: the toggler carries their total.
    # For the MD and the PM the dashboard badge already is that total; for everyone else
    # it is the sum of their section badges.
    total = links[0].count or sum(link.count for link in links[1:])
    return {
        "links": links,
        "total": total,
        "total_needs_md": user.role == Role.EXECUTIVE and total > 0,
    }
