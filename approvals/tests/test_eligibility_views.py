"""The eligibility screens (R16, R9a): the PM raises, the MD rules, the tender unfreezes."""

from django.test import TestCase
from django.urls import reverse

from accounts.models import Role
from approvals.models import EligibilityDecision as E, EligibilityReview
from suppliers.models import Supplier
from tenders.models import TenderStatus as S
from tenders.tests.helpers import TenderFixtures, confirmed_fields, make_user

REQUIREMENT = "ISO 17025 accreditation of the bidder calibration laboratory"


class RaiseScreenTests(TenderFixtures, TestCase):
    def setUp(self):
        self.tender = self.new_tender(
            S.COSTING, **confirmed_fields(),
            special_requirements=f"Minimum turnover TZS 2bn over 3 years\n{REQUIREMENT}",
        )
        self.url = reverse("approvals:eligibility_raise", args=[self.tender.pk])

    def test_the_tender_page_offers_it_to_its_pm_only(self):
        detail = reverse("tenders:detail", args=[self.tender.pk])
        self.client.force_login(self.pm)
        self.assertContains(self.client.get(detail), self.url)
        self.client.force_login(self.pm2)
        self.assertNotContains(self.client.get(detail), self.url)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_the_tenders_own_requirements_are_offered(self):
        self.client.force_login(self.pm)
        response = self.client.get(self.url)
        self.assertContains(response, REQUIREMENT)
        self.assertContains(response, "Minimum turnover TZS 2bn")

    def test_raising_a_listed_requirement_freezes_the_tender(self):
        self.client.force_login(self.pm)
        response = self.client.post(self.url, {"requirement_choice": "1", "pm_notes": "Partner with a lab."})
        review = EligibilityReview.objects.get(tender=self.tender)
        self.assertRedirects(response, reverse("approvals:eligibility_review", args=[review.pk]))
        self.assertEqual((review.requirement, review.pm_notes), (REQUIREMENT, "Partner with a lab."))
        self.tender.refresh_from_db()
        self.assertEqual((self.tender.status, self.tender.previous_status), (S.PENDING_ELIGIBILITY_DECISION, S.COSTING))

    def test_a_typed_requirement(self):
        self.client.force_login(self.pm)
        self.client.post(self.url, {"requirement_choice": "__other__", "requirement_other": "  Local  content 30% "})
        self.assertEqual(EligibilityReview.objects.get(tender=self.tender).requirement, "Local content 30%")

    def test_other_without_text_is_refused(self):
        self.client.force_login(self.pm)
        response = self.client.post(self.url, {"requirement_choice": "__other__"})
        self.assertContains(response, "Type the requirement")
        self.assertFalse(EligibilityReview.objects.exists())

    def test_not_from_a_stage_where_it_cannot_be_raised(self):
        tender = self.new_tender(S.PENDING_AUDIT, **confirmed_fields())
        self.client.force_login(self.pm)
        url = reverse("approvals:eligibility_raise", args=[tender.pk])
        self.assertRedirects(self.client.get(url), reverse("tenders:detail", args=[tender.pk]))
        self.assertRedirects(self.client.post(url, {"requirement_choice": "__other__", "requirement_other": "x"}),
                             reverse("tenders:detail", args=[tender.pk]))
        self.assertFalse(EligibilityReview.objects.exists())


class RulingScreenTests(TenderFixtures, TestCase):
    def setUp(self):
        self.tender = self.new_tender(S.DRAFTING, **confirmed_fields())
        self.review = EligibilityReview.objects.raise_for(self.tender, self.pm, requirement=REQUIREMENT)
        self.url = reverse("approvals:eligibility_review", args=[self.review.pk])

    def rule(self, decision, **extra):
        self.client.force_login(self.md)
        return self.client.post(self.url, {"md_decision": decision, **extra})

    def refreshed(self):
        self.tender.refresh_from_db()
        self.review.refresh_from_db()
        return self.tender, self.review

    def test_md_sees_the_ruling_form_and_others_do_not(self):
        self.client.force_login(self.md)
        self.assertContains(self.client.get(self.url), "Record ruling")
        for user in (self.pm, self.hops, self.auditor, self.finance):
            self.client.force_login(user)
            response = self.client.get(self.url)
            self.assertEqual(response.status_code, 200)
            self.assertNotContains(response, "Record ruling")
            self.assertContains(response, REQUIREMENT)
            told = "You will be emailed the ruling"
            (self.assertContains if user == self.pm else self.assertNotContains)(response, told)

    def test_only_the_md_can_rule(self):
        for user in (self.pm, self.hops, self.admin_user):
            self.client.force_login(user)
            response = self.client.post(self.url, {"md_decision": E.PROCEED})
            self.assertEqual(response.status_code, 403, user.role)
        self.assertTrue(self.refreshed()[1].is_open)

    def test_clearing_agents_cannot_see_it(self):
        self.client.force_login(make_user(Role.CLEARING_AGENT, "clear"))
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_proceed_returns_the_tender_to_where_it_was(self):
        response = self.rule(E.PROCEED)
        self.assertRedirects(response, reverse("tenders:detail", args=[self.tender.pk]))
        tender, review = self.refreshed()
        self.assertEqual((tender.status, review.md_decision, review.decided_by), (S.DRAFTING, E.PROCEED, self.md))

    def test_remediate_needs_notes(self):
        response = self.rule(E.REMEDIATE)
        self.assertContains(response, "Tell the PM why")
        self.assertTrue(self.refreshed()[1].is_open)
        self.rule(E.REMEDIATE, decision_notes="Get the accreditation certificate renewed first.")
        tender, review = self.refreshed()
        self.assertEqual((tender.status, review.decision_notes),
                         (S.DRAFTING, "Get the accreditation certificate renewed first."))

    def test_joint_venture_with_an_existing_firm(self):
        partner = Supplier.objects.create(name="Bureau Veritas Tanzania", country="Tanzania")
        self.rule(E.JOINT_VENTURE, jv_partner=partner.pk, decision_notes="They hold ISO 17025.")
        tender, review = self.refreshed()
        self.assertEqual((tender.status, review.jv_partner), (S.DRAFTING, partner))

    def test_joint_venture_needs_a_partner(self):
        response = self.rule(E.JOINT_VENTURE, decision_notes="Partner needed.")
        self.assertContains(response, "Name the joint venture partner")
        self.assertTrue(self.refreshed()[1].is_open)

    def test_joint_venture_with_a_new_firm_reuses_a_matching_supplier(self):
        existing = Supplier.objects.create(name="SGS Tanzania Superintendence Co. Ltd")
        self.rule(E.JOINT_VENTURE, new_partner_name="SGS TANZANIA SUPERINTENDENCE", decision_notes="JV.")
        self.assertEqual(self.refreshed()[1].jv_partner, existing)
        self.assertEqual(Supplier.objects.count(), 1)

    def test_joint_venture_with_a_firm_not_yet_known_creates_it(self):
        self.rule(E.JOINT_VENTURE, new_partner_name="Calibration Lab East Africa", decision_notes="JV.")
        self.assertEqual(self.refreshed()[1].jv_partner.name, "Calibration Lab East Africa")

    def test_a_partner_on_another_ruling_is_ignored_not_stored(self):
        partner = Supplier.objects.create(name="Bureau Veritas Tanzania")
        self.rule(E.PROCEED, jv_partner=partner.pk)
        self.assertIsNone(self.refreshed()[1].jv_partner)

    def test_withdraw_must_be_confirmed_and_then_withdraws(self):
        response = self.rule(E.WITHDRAW, decision_notes="Cannot meet it in time.")
        self.assertContains(response, "Tick to confirm")
        self.assertEqual(self.refreshed()[0].status, S.PENDING_ELIGIBILITY_DECISION)
        self.rule(E.WITHDRAW, decision_notes="Cannot meet it in time.", confirm_withdraw="on")
        tender, review = self.refreshed()
        self.assertEqual((tender.status, review.md_decision), (S.WITHDRAWN, E.WITHDRAW))

    def test_a_decided_review_is_a_record_not_a_form(self):
        self.rule(E.PROCEED)
        response = self.client.get(self.url)
        self.assertNotContains(response, "Record ruling")
        self.assertContains(response, "Proceed")
        again = self.client.post(self.url, {"md_decision": E.WITHDRAW, "decision_notes": "x", "confirm_withdraw": "on"})
        self.assertRedirects(again, self.url)
        self.assertEqual(self.refreshed()[0].status, S.DRAFTING)

    def test_the_md_reaches_it_from_the_queue_dashboard_and_tender(self):
        self.client.force_login(self.md)
        self.assertContains(self.client.get(reverse("approvals:eligibility_queue")), self.url)
        self.assertContains(self.client.get(reverse("tenders:detail", args=[self.tender.pk])), self.url)
        self.assertContains(self.client.get(reverse("dashboard")), self.url)

    def test_queue_is_md_only(self):
        self.client.force_login(self.pm)
        self.assertEqual(self.client.get(reverse("approvals:eligibility_queue")).status_code, 403)
