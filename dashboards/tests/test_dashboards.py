"""
The MD and PM dashboards, the shared navigation, breadcrumbs and response compression.
Tenders reach their states through the real transitions wherever the test is about
what those transitions leave behind (R9 returns); force_status elsewhere.
"""

import gzip
import re
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from approvals.models import (
    Approval,
    ApprovalDecision,
    ApprovalKind,
    EligibilityReview,
    FundRequest,
    FundRequestPurpose,
)
from audits.models import QCAudit, Verdict
from core.models import Client
from items.services.rfq import send_rfq
from items.tests.helpers import ItemFixtures, force_status, make_user
from accounts.models import Role
from suppliers.models import Supplier
from tenders.models import TenderStatus as S

from dashboards import queries
from dashboards.navigation import nav_for

D = Decimal


def days_from_now(n):
    return timezone.now() + timedelta(days=n, hours=2)


class DashboardCase(ItemFixtures, TestCase):
    def setUp(self):
        super().setUp()
        self.client_record = Client.objects.get_or_create(name="Muhimbili National Hospital", short_name="MNH")[0]

    def tender(self, status, *, pm=None, days=10, title="Supply of laboratory centrifuges", **fields):
        t = self.new_tender()
        if pm is not None and pm != self.pm:
            t.reassign(self.md, pm, reason="test")
        fields.setdefault("client", self.client_record)
        force_status(t, status, title=title, submission_deadline=days_from_now(days), **fields)
        return t

    def as_(self, user):
        self.client.force_login(user)
        return self.client.get(reverse("dashboard"))

    def audited(self, *, days=10):
        """COSTING -> PENDING_AUDIT -> (positive audit) PENDING_EXECUTIVE_APPROVAL, the real way."""
        t = self.tender(S.COSTING, days=days)
        self.complete_item(t)
        t.submit_for_audit(self.pm)
        audit = QCAudit.objects.start(t, self.auditor)
        return t, audit

    def decide_audit(self, audit, verdict):
        for line in audit.lines.all():
            line.verdict = verdict
            line.comments = "Offered capacity is short." if verdict == Verdict.NEGATIVE else ""
            line.save()
        audit.complete(self.auditor, reason="Capacity below the specification." if verdict == Verdict.NEGATIVE else "")


class WhoGetsWhichDashboardTests(DashboardCase):
    def test_each_role_gets_its_dashboard(self):
        expected = {
            self.md: "dashboards/md.html", self.pm: "dashboards/pm.html",
            self.hops: "dashboards/home.html", self.auditor: "dashboards/home.html",
            self.finance: "dashboards/home.html", self.admin_user: "dashboards/home.html",
            make_user(Role.CLEARING_AGENT, "clear"): "dashboards/home.html",
        }
        for user, template in expected.items():
            with self.subTest(role=user.role):
                self.assertTemplateUsed(self.as_(user), template)

    def test_signed_out_users_go_to_the_login_page(self):
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

    def test_the_view_lives_in_dashboards_not_accounts(self):
        import accounts.views

        self.assertFalse(hasattr(accounts.views, "dashboard"))  # accounts may not import tenders (§1)
        self.assertEqual(self.client.get("/").resolver_match.func.__module__, "dashboards.views")


class MdDashboardTests(DashboardCase):
    def test_the_signature_queue_shows_reference_client_value_and_days_left(self):
        t, audit = self.audited(days=2)
        self.decide_audit(audit, Verdict.POSITIVE)
        response = self.as_(self.md)
        queue = response.context["signature_queue"]
        self.assertEqual([x.pk for x in queue], [t.pk])
        self.assertEqual(queue[0].days_left, 2)
        self.assertContains(response, t.reference)
        self.assertContains(response, t.client.short_name)
        self.assertContains(response, f"TZS {queue[0].summary.total:,.0f}")
        self.assertContains(response, "2 days left")
        self.assertContains(response, reverse("approvals:submission_decide", args=[t.pk]))
        self.assertContains(response, "1 bid awaiting your signature")

    def test_brass_only_where_the_md_is_the_blocker(self):
        quiet = self.as_(self.md).content.decode()
        self.assertNotIn("needs-md-panel", quiet)  # nothing waiting on him: no brass at all
        self.assertIn("No bids awaiting your signature", quiet)

        self.tender(S.PENDING_REGISTRATION_APPROVAL)
        busy = self.as_(self.md).content.decode()
        self.assertEqual(busy.count("needs-md-panel"), 1)  # only the registrations card

    def test_the_other_md_queues(self):
        reg = self.tender(S.PENDING_REGISTRATION_APPROVAL, title="Gas chromatograph")
        frozen = self.tender(S.COSTING)
        EligibilityReview.objects.raise_for(frozen, self.pm, requirement="ISO 17025 accreditation")  # R16, freezes it
        funded = self.tender(S.COSTING)
        fr = FundRequest.objects.create(tender=funded, purpose=FundRequestPurpose.NEST_APPLICATION_FEE,
                                        amount=D("50000"), currency="TZS", requested_by=self.pm)
        response = self.as_(self.md)
        self.assertEqual([t.pk for t in response.context["registrations"]], [reg.pk])
        self.assertEqual([r.tender_id for r in response.context["eligibility"]], [frozen.pk])
        self.assertEqual([f.pk for f in response.context["funds"]], [fr.pk])
        self.assertContains(response, reverse("approvals:registration_decide", args=[reg.pk]))
        self.assertContains(response, reverse("approvals:fund_detail", args=[fr.pk]))
        self.assertContains(response, "ISO 17025 accreditation")

    def test_at_risk_means_unpriced_items_and_a_close_deadline(self):
        close_unpriced = self.tender(S.COSTING, days=2)
        self.make_item(close_unpriced)
        close_priced = self.tender(S.COSTING, days=2)
        self.complete_item(close_priced)
        far_unpriced = self.tender(S.COSTING, days=queries.AT_RISK_DAYS + 5)
        self.make_item(far_unpriced)
        close_empty = self.tender(S.DRAFTING, days=1)
        submitted = self.tender(S.SUBMITTED, days=1, exchange_rate_locked_at=timezone.now())

        at_risk = [t.pk for t in self.as_(self.md).context["at_risk"]]
        self.assertEqual(at_risk, [close_empty.pk, close_unpriced.pk])  # nearest deadline first
        self.assertNotIn(submitted.pk, at_risk)

    def test_at_risk_is_never_brass(self):
        self.make_item(self.tender(S.COSTING, days=1))
        html = self.as_(self.md).content.decode()
        section = html[html.index('id="risk-h"'):]
        self.assertNotIn("needs-md", section)


class PmDashboardTests(DashboardCase):
    def test_the_hero_is_my_nearest_live_deadline_with_its_blocker(self):
        later = self.tender(S.COSTING, days=9)
        nearest = self.tender(S.COSTING, days=3, title="Autoclaves for MUHAS")
        self.make_item(nearest)  # unpriced: the blocker
        self.tender(S.COSTING, days=1, pm=self.pm2)  # someone else's
        self.tender(S.COSTING, days=-2)  # passed: not the hero
        response = self.as_(self.pm)
        hero = response.context["hero"]
        self.assertEqual(hero.pk, nearest.pk)
        self.assertContains(response, "Autoclaves for MUHAS")
        self.assertContains(response, "1 of 1 items have no price yet")
        self.assertIn(("price", 1), hero.blockers)
        self.assertNotIn(later.pk, [response.context["hero"].pk])

    def test_my_tenders_with_pricing_progress_and_only_mine(self):
        mine = self.tender(S.COSTING)
        self.complete_item(mine)
        self.make_item(mine)
        theirs = self.tender(S.COSTING, pm=self.pm2)
        self.tender(S.WON, exchange_rate_locked_at=timezone.now())  # closed: not listed
        tenders = self.as_(self.pm).context["tenders"]
        self.assertEqual([t.pk for t in tenders], [mine.pk])
        self.assertEqual((tenders[0].items_priced, tenders[0].items_total, tenders[0].percent_priced), (1, 2, 50))
        self.assertNotIn(theirs.pk, [t.pk for t in tenders])

    def test_a_negative_qc_verdict_is_returned_to_me(self):
        t, audit = self.audited()
        self.decide_audit(audit, Verdict.NEGATIVE)
        t.refresh_from_db()
        self.assertEqual(t.status, S.COSTING)
        response = self.as_(self.pm)
        returned = response.context["returned"]
        self.assertEqual([(r.tender.pk, r.what) for r in returned], [(t.pk, "Rejected at QC audit")])
        self.assertContains(response, "Capacity below the specification.")

    def test_a_declined_submission_is_returned_to_me_with_the_reason(self):
        t, audit = self.audited()
        self.decide_audit(audit, Verdict.POSITIVE)
        Approval.objects.sign(t, ApprovalKind.SUBMISSION, ApprovalDecision.DECLINED, self.md,
                              typed_name=self.md.get_full_name(), password="x",
                              comments="Margin too thin against the last MSD award.")
        response = self.as_(self.pm)
        self.assertEqual([r.what for r in response.context["returned"]], ["Submission declined by the MD"])
        self.assertContains(response, "Margin too thin against the last MSD award.")

    def test_a_declined_fund_request_is_returned_until_a_new_one_is_raised(self):
        t = self.tender(S.COSTING)
        fr = FundRequest.objects.create(tender=t, purpose=FundRequestPurpose.NEST_APPLICATION_FEE,
                                        amount=D("50000"), currency="TZS", requested_by=self.pm)
        fr.decline(self.md, "Use the tender document fee line.")
        self.assertEqual(len(self.as_(self.pm).context["returned"]), 1)
        FundRequest.objects.create(tender=t, purpose=FundRequestPurpose.NEST_APPLICATION_FEE,
                                   amount=D("40000"), currency="TZS", requested_by=self.pm)
        self.assertEqual(self.as_(self.pm).context["returned"], [])

    def test_quotes_outstanding_until_the_supplier_quotes(self):
        t = self.tender(S.COSTING)
        item = self.make_item(t)
        supplier = Supplier.objects.create(name="Hettich", email="sales@hettich.example", country="Germany")
        send_rfq(t, supplier, [item], self.pm)
        outstanding = self.as_(self.pm).context["outstanding"]
        self.assertEqual([(o.rfq.supplier_id, o.waiting_items) for o in outstanding], [(supplier.pk, 1)])
        self.quote(item, supplier=supplier)
        self.assertEqual(self.as_(self.pm).context["outstanding"], [])


class NavigationTests(DashboardCase):
    def nav(self, user, url=None):
        self.client.force_login(user)
        html = self.client.get(url or reverse("dashboard")).content.decode()
        return html[html.index('aria-label="Main"'):html.index("</nav>")]

    def test_each_role_sees_its_own_links(self):
        expected = {
            self.md: ["Dashboard", "Tenders", "Registrations", "Submissions", "Fund requests", "QC audits"],
            self.pm: ["Dashboard", "Tenders", "Upload tender", "Fund requests"],
            self.auditor: ["Dashboard", "QC audits", "Tenders"],
            self.finance: ["Dashboard", "Fund requests", "Tenders"],
            self.hops: ["Dashboard", "Tenders", "QC audits", "Fund requests"],
            self.admin_user: ["Dashboard", "Tenders", "Administration"],
        }
        every = {"Registrations", "Submissions", "Upload tender", "QC audits", "Fund requests", "Administration"}
        for user, labels in expected.items():
            html = self.nav(user)
            with self.subTest(role=user.role):
                for label in labels:
                    self.assertIn(f">\n            {label}", html.replace("\r", ""))
                for label in every - set(labels):
                    self.assertNotIn(f">\n            {label}", html.replace("\r", ""))

    def test_a_clearing_agent_sees_the_dashboard_only(self):
        html = self.nav(make_user(Role.CLEARING_AGENT, "clear"))
        self.assertNotIn(reverse("tenders:list"), html)

    def test_md_badges_are_brass_and_count_his_queues(self):
        self.tender(S.PENDING_REGISTRATION_APPROVAL)
        self.tender(S.PENDING_REGISTRATION_APPROVAL)
        self.client.force_login(self.md)
        response = self.client.get(reverse("dashboard"))
        data = nav_for(self.md, response.wsgi_request)
        by_key = {link.key: link for link in data["links"]}
        self.assertEqual((by_key["registrations"].count, by_key["registrations"].needs_md), (2, True))
        self.assertEqual(by_key["dashboard"].count, 2)
        self.assertEqual(data["total"], 2)
        self.assertIn("needs-md-badge", self.nav(self.md))

    def test_other_roles_badges_are_never_brass(self):
        t, _ = self.audited()
        html = self.nav(self.auditor)
        self.assertIn('aria-label="1 waiting"', html)
        self.assertNotIn("needs-md-badge", html)

    def test_the_current_section_is_marked(self):
        t = self.tender(S.COSTING)
        item = self.make_item(t)
        html = self.nav(self.pm, reverse("items:quotes", args=[item.pk]))
        active = html[:html.index('aria-current="page"')]
        self.assertTrue(active.rstrip().endswith(f'href="{reverse("tenders:list")}"'))

    def test_a_user_who_must_change_password_sees_no_menu(self):
        self.pm.must_change_password = True
        self.pm.save()
        self.client.force_login(self.pm)
        html = self.client.get(reverse("password_change")).content.decode()
        self.assertNotIn(reverse("tenders:list"), html)
        self.assertIn(reverse("logout"), html)


class BreadcrumbTests(DashboardCase):
    def test_item_quote_and_audit_pages_lead_back_to_the_tender(self):
        t, audit = self.audited()
        item = t.items.get()
        self.client.force_login(self.pm)
        pages = {
            reverse("items:sheet", args=[t.pk]): "Item sheet",
            reverse("items:quotes", args=[item.pk]): "Quotes",
        }
        self.client.force_login(self.md)
        pages_md = {reverse("audits:detail", args=[t.pk]): "QC audit"}
        for user, urls in ((self.pm, pages), (self.md, pages_md)):
            self.client.force_login(user)
            for url, here in urls.items():
                with self.subTest(url=url):
                    response = self.client.get(url)
                    self.assertEqual(response.status_code, 200)
                    html = response.content.decode()
                    crumbs = html[html.index('aria-label="Breadcrumb"'):]
                    crumbs = crumbs[:crumbs.index("</nav>")]
                    self.assertIn(reverse("tenders:detail", args=[t.pk]), crumbs)
                    self.assertIn(f'data-copy="{t.reference}"', crumbs)  # R18: copyable
                    self.assertIn(here, crumbs)
        quotes = self.client.get(reverse("items:quotes", args=[item.pk])).content.decode()
        self.assertIn(f'{reverse("items:sheet", args=[t.pk])}#item-{item.pk}', quotes)


class CompressionTests(DashboardCase):
    def test_pages_are_gzipped_for_clients_that_accept_it(self):
        t = self.tender(S.COSTING)
        for _ in range(20):
            self.make_item(t, specification="Benchtop centrifuge, refrigerated, 24 x 1.5 ml, 15,000 rpm " * 3)
        self.client.force_login(self.pm)
        url = reverse("items:sheet", args=[t.pk])
        plain = self.client.get(url)
        zipped = self.client.get(url, headers={"accept-encoding": "gzip"})
        self.assertNotIn("Content-Encoding", plain.headers)
        self.assertEqual(zipped.headers["Content-Encoding"], "gzip")
        # The CSRF token is masked afresh on every response (part of the BREACH defence),
        # so compare the pages with it blanked out.
        def unmasked(body):
            return re.sub(rb'("X-CSRFToken": "|name="csrfmiddlewaretoken" value=")[^"]+', rb"", body)
        self.assertEqual(unmasked(gzip.decompress(zipped.content)), unmasked(plain.content))
        self.assertLess(len(zipped.content), len(plain.content) / 4)
