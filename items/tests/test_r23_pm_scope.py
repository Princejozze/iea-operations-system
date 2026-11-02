"""
R23: a PM sees only their own tenders. Enforced in tenders.views.visible_tenders (the
queryset every view starts from), never in templates: another PM gets 403 on every URL
under a tender they do not hold, and the holder gets in.

Coverage is not a hand-kept list. EveryRouteTests walks the URL resolver for the tenders,
items, audits and approvals namespaces and probes every route that takes an object, htmx
fragment endpoints included (navigation never reaches those, so they are the easiest to
miss). A route it cannot map to an object fails the test: a new endpoint has to be thought
about before it can ship.

Lives in items/tests because the routes reach items, quotes and attachments; items may
import from every app above it except audits and approvals, which these tests reach only
through apps.get_model and the URL resolver.
"""

from decimal import Decimal
from io import StringIO

from django.apps import apps
from django.core.management import call_command
from django.urls import get_resolver, reverse
from django.test import TestCase, override_settings
from django.utils import timezone

from accounts.models import Role, User
from core.models import AuditEntry
from items.models import Item, ItemAttachment, Quote
from tenders.models import Tender, TenderStatus as S

from .helpers import ItemFixtures, make_user

D = Decimal
NAMESPACES = ("tenders", "items", "audits", "approvals")


def routes():
    """(namespace, url name, pattern, kwarg name) for every route that takes an object."""
    found = []
    resolver = get_resolver()
    for ns in NAMESPACES:
        _, sub = resolver.namespace_dict[ns]
        for p in sub.url_patterns:
            kwargs = list(p.pattern.converters)
            if kwargs:
                found.append((ns, p.name, str(p.pattern), kwargs[0]))
    return found


class ScopeFixtures(ItemFixtures):
    """One PM's tender with something under it at every level a URL reaches."""

    def build(self):
        o = {}
        o["tender"] = t = self.costing()  # held by self.pm
        o["item"] = item = self.complete_item(t)
        o["attachment"] = item.attachments.first()
        o["quote"] = self.quote(item)
        FundRequest = apps.get_model("approvals", "FundRequest")
        o["fund"] = FundRequest.objects.create(tender=t, purpose="NEST_APPLICATION_FEE", amount=D("50000"),
                                               currency="TZS", requested_by=self.pm)
        frozen = self.costing()
        o["review"] = apps.get_model("approvals", "EligibilityReview").objects.raise_for(
            frozen, self.pm, requirement="ISO 15189 accreditation")
        audited = self.costing()
        self.complete_item(audited)
        audited.submit_for_audit(self.pm)
        o["audited"] = audited
        o["line"] = apps.get_model("audits", "QCAudit").objects.start(audited, self.auditor).lines.first()
        return o

    @staticmethod
    def object_for(ns, pattern, o):
        """Which object a route's pk names, from the route's own path."""
        def pk(key):
            return o[key].pk if o.get(key) is not None else None
        if ns == "tenders":
            return pk("tender")
        if ns == "items":
            for prefix, key in (("tender/", "tender"), ("item/", "item"), ("attachment/", "attachment"),
                                ("quote/", "quote")):
                if pattern.startswith(prefix):
                    return pk(key)
        if ns == "audits":
            if pattern.startswith("tender/"):
                return pk("audited")
            if pattern.startswith("line/"):
                return pk("line")
        if ns == "approvals":
            for prefix, key in (("registration/", "tender"), ("submission/", "tender"),
                                ("eligibility/raise/", "tender"), ("eligibility/", "review"),
                                ("funds/new/", "tender"), ("funds/", "fund")):
                if pattern.startswith(prefix):
                    return pk(key)
        raise AssertionError(
            f"R23: no mapping for route {ns}:{pattern!r}. Decide which object it reaches and add it "
            "here, so the route is probed for another PM."
        )

    def assert_refused_everywhere(self, user, objects, *, require=()):
        """``user`` gets 403 on every object route: GET or POST (whichever the route takes)
        is 403, the other is 403 or 405. Returns the routes probed."""
        self.client.force_login(user)
        probed = []
        for ns, name, pattern, kwarg in routes():
            pk = self.object_for(ns, pattern, objects)
            if pk is None:
                continue  # the demo tender may have no quote, review or audit line yet
            url = reverse(f"{ns}:{name}", kwargs={kwarg: pk})
            got = (self.client.get(url).status_code, self.client.post(url, {}).status_code)
            with self.subTest(route=f"{ns}:{name}", url=url, got=got):
                self.assertTrue(set(got) <= {403, 405} and 403 in got, f"{url} answered GET/POST {got}")
            probed.append(f"{ns}:{name}")
        for name in require:
            self.assertIn(name, probed)
        return probed


class EveryRouteTests(ScopeFixtures, TestCase):
    def setUp(self):
        super().setUp()
        self.o = self.build()

    def test_another_pm_is_refused_on_every_route_and_changes_nothing(self):
        before = (Item.objects.count(), ItemAttachment.objects.count(),
                  list(Quote.objects.values_list("pk", "status")))
        probed = self.assert_refused_everywhere(self.pm2, self.o)
        # Every PM-facing family is in there, htmx fragments included.
        for name in ("tenders:detail", "tenders:status", "tenders:verify", "tenders:rates", "items:sheet",
                     "items:item_save", "items:item_more", "items:item_delete", "items:attachment_add",
                     "items:attachment_download", "items:attachment_delete", "items:quotes",
                     "items:quote_select", "items:quote_reject", "items:quote_sheet_download",
                     "items:quote_sheet_upload", "items:item_add", "items:start_costing",
                     "items:submit_for_audit", "audits:detail", "approvals:fund_new",
                     "approvals:fund_detail", "approvals:eligibility_raise", "approvals:eligibility_review"):
            self.assertIn(name, probed)
        after = (Item.objects.count(), ItemAttachment.objects.count(),
                 list(Quote.objects.values_list("pk", "status")))
        self.assertEqual(after, before)

    def test_the_owner_gets_in(self):
        o = self.o
        self.client.force_login(self.pm)
        pages = [
            reverse("tenders:list"), reverse("tenders:detail", args=[o["tender"].pk]),
            reverse("tenders:rates", args=[o["tender"].pk]), reverse("items:sheet", args=[o["tender"].pk]),
            reverse("items:item_more", args=[o["item"].pk]), reverse("items:quotes", args=[o["item"].pk]),
            reverse("items:attachment_download", args=[o["attachment"].pk]),
            reverse("items:quote_sheet_download", args=[o["tender"].pk]),
            reverse("items:quote_sheet_upload", args=[o["tender"].pk]),
            reverse("audits:detail", args=[o["audited"].pk]),
            reverse("approvals:fund_list"), reverse("approvals:fund_new", args=[o["tender"].pk]),
            reverse("approvals:fund_detail", args=[o["fund"].pk]),
            reverse("approvals:eligibility_raise", args=[o["tender"].pk]),
            reverse("approvals:eligibility_review", args=[o["review"].pk]),
        ]
        for url in pages:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)
        # The htmx fragments the owner posts to are let through (200, or 422 for a refused value).
        posts = [
            (reverse("items:item_save", args=[o["item"].pk]), {"specification": "Centrifuge", "quantity": "2"}),
            (reverse("items:item_add", args=[o["tender"].pk]), {"specification": "Pipette set", "quantity": "1"}),
            (reverse("items:quote_reject", args=[o["quote"].pk]), {}),
        ]
        for url, data in posts:
            with self.subTest(url=url):
                self.assertNotIn(self.client.post(url, data, headers={"hx-request": "true"}).status_code, (403, 404))

    def test_extraction_and_verification_screens(self):
        tender = self.new_tender()  # EXTRACTING, held by self.pm
        status, verify = reverse("tenders:status", args=[tender.pk]), reverse("tenders:verify", args=[tender.pk])
        self.client.force_login(self.pm2)
        self.assertEqual(self.client.get(status).status_code, 403)
        self.client.force_login(self.pm)
        self.assertEqual(self.client.get(status).status_code, 200)
        tender.finish_extraction(None)
        self.assertEqual(self.client.get(verify).status_code, 200)
        self.client.force_login(self.pm2)
        self.assertEqual(self.client.get(verify).status_code, 403)

    def test_a_missing_tender_is_404_not_403(self):
        self.client.force_login(self.pm2)
        self.assertEqual(self.client.get(reverse("tenders:detail", args=[999999])).status_code, 404)


class ListScopeTests(ScopeFixtures, TestCase):
    def test_the_list_and_search_show_a_pm_only_their_own(self):
        mine, theirs = self.costing(), Tender.objects.create_from_upload(uploaded_by=self.pm2)
        self.client.force_login(self.pm)
        listed = [t.pk for t in self.client.get(reverse("tenders:list")).context["page"]]
        self.assertIn(mine.pk, listed)
        self.assertNotIn(theirs.pk, listed)
        searched = self.client.get(reverse("tenders:list"), {"q": theirs.reference}).context["page"]
        self.assertEqual(list(searched), [])  # searching for it by reference finds nothing (R18 within R23)

    def test_audit_hops_exec_admin_and_finance_stay_unscoped_and_clear_is_refused(self):
        tender = self.costing()
        url = reverse("tenders:detail", args=[tender.pk])
        for user in (self.md, self.hops, self.auditor, self.admin_user, self.finance):
            self.client.force_login(user)
            with self.subTest(role=user.role):
                self.assertEqual(self.client.get(url).status_code, 200)
        self.client.force_login(make_user(Role.CLEARING_AGENT, "clearer"))
        self.assertEqual(self.client.get(url).status_code, 403)  # R5


class ReassignmentTransfersAccessTests(ScopeFixtures, TestCase):
    """R13 + R23: the new PM gains the tender and the previous PM loses it, at once."""

    def test_the_old_pm_loses_access_and_the_new_pm_gains_it(self):
        o = self.build()
        t = o["tender"]
        urls = [
            reverse("tenders:detail", args=[t.pk]), reverse("items:sheet", args=[t.pk]),
            reverse("items:quotes", args=[o["item"].pk]), reverse("items:item_more", args=[o["item"].pk]),
            reverse("items:attachment_download", args=[o["attachment"].pk]),
            reverse("items:quote_sheet_download", args=[t.pk]),
            reverse("approvals:fund_detail", args=[o["fund"].pk]),
        ]
        self.client.force_login(self.pm2)
        for url in urls:
            self.assertEqual(self.client.get(url).status_code, 403, url)
        written_by_old_pm = AuditEntry.objects.filter(actor=self.pm).count()
        self.assertGreater(written_by_old_pm, 0)

        t.reassign(self.md, self.pm2, reason="Gift is on leave until the deadline.")

        for user, expected in ((self.pm, 403), (self.pm2, 200)):
            self.client.force_login(user)
            for url in urls:
                with self.subTest(user=user.email, url=url):
                    self.assertEqual(self.client.get(url).status_code, expected)
        self.client.force_login(self.pm)
        self.assertNotIn(t.reference, self.client.get(reverse("tenders:list")).content.decode())
        # The former PM is refused everywhere, fragments and POSTs included.
        self.assert_refused_everywhere(self.pm, {k: v for k, v in o.items() if k in
                                                 ("tender", "item", "attachment", "quote", "fund")})
        # R7: what they wrote while they held it stays in the audit trail.
        self.assertEqual(AuditEntry.objects.filter(actor=self.pm).count(), written_by_old_pm)


class DemoDataTests(ScopeFixtures, TestCase):
    """The walkthrough data: pm2@ holds IEA-<year>-0008, so pm@ is refused on every URL of it."""

    def test_pm_is_refused_on_every_url_of_pm2s_demo_tender(self):
        # By command name, not by import: items may not import dashboards (§1).
        with override_settings(DEBUG=True):
            call_command("seed_demo", stdout=StringIO())
        reference = f"IEA-{timezone.localdate().year}-0008"
        tender = Tender.objects.get(reference=reference)
        pm, pm2 = User.objects.get(email="pm@demo.invalid"), User.objects.get(email="pm2@demo.invalid")
        self.assertEqual(tender.project_manager, pm2)
        items = Item.objects.filter(tender=tender)
        objects = {
            "tender": tender, "item": items.first(),
            "attachment": ItemAttachment.objects.filter(item__tender=tender).first(),
            "quote": Quote.objects.filter(item__tender=tender).first(),
            "fund": apps.get_model("approvals", "FundRequest").objects.filter(tender=tender).first(),
            "review": apps.get_model("approvals", "EligibilityReview").objects.filter(tender=tender).first(),
            "audited": tender,
            "line": apps.get_model("audits", "QCAuditLine").objects.filter(audit__tender=tender).first(),
        }
        self.assert_refused_everywhere(pm, objects, require=("tenders:detail", "items:sheet", "tenders:rates"))
        self.client.force_login(pm2)
        self.assertEqual(self.client.get(reverse("tenders:detail", args=[tender.pk])).status_code, 200)
