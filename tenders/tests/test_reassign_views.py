"""The MD's reassignment screen (R13): pick any active PM, confirm, history shown."""

from django.core import mail
from django.test import TestCase
from django.urls import reverse

from accounts.models import Role
from tenders.models import TenderAssignment, TenderStatus as S

from .helpers import TenderFixtures, confirmed_fields, make_user


class ReassignScreenTests(TenderFixtures, TestCase):
    def setUp(self):
        self.tender = self.new_tender(S.COSTING, **confirmed_fields())
        self.url = reverse("tenders:reassign", args=[self.tender.pk])
        self.detail = reverse("tenders:detail", args=[self.tender.pk])

    def post(self, pm, *, confirm=False, reason=""):
        self.client.force_login(self.md)
        data = {"new_pm": pm.pk, "reason": reason}
        if confirm:
            data["confirm"] = "yes"
        return self.client.post(self.url, data)

    def test_the_md_reaches_it_from_the_tender_page(self):
        self.client.force_login(self.md)
        response = self.client.get(self.detail)
        self.assertContains(response, self.url)
        self.assertContains(response, "Assignment history")

    def test_others_see_the_history_but_not_the_control(self):
        self.client.force_login(self.pm)  # the holder: readable under R23 scoping too
        response = self.client.get(self.detail)
        self.assertContains(response, "Assignment history")
        self.assertNotContains(response, self.url)

    def test_only_the_md_may_open_or_post(self):
        for user in (self.pm, self.hops, self.auditor, self.finance, self.admin_user):
            self.client.force_login(user)
            self.assertEqual(self.client.get(self.url).status_code, 403, user.role)
            response = self.client.post(self.url, {"new_pm": self.pm2.pk, "confirm": "yes"})
            self.assertEqual(response.status_code, 403, user.role)
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.project_manager, self.pm)

    def test_offers_every_active_pm_but_the_holder_with_their_load(self):
        inactive = make_user(Role.PROJECT_MANAGER, "dinna", is_active=False)
        self.new_tender(S.DRAFTING)  # gives the holder a second open tender; pm2 has none
        self.client.force_login(self.md)
        response = self.client.get(self.url)
        choices = list(response.context["form"].fields["new_pm"].queryset)
        self.assertEqual(choices, [self.pm2])
        self.assertNotIn(inactive, choices)
        self.assertContains(response, "Alice Test — 0 open tenders")
        self.assertContains(response, "Gift Test")  # who holds it now

    def test_choosing_first_shows_a_confirmation_and_moves_nothing(self):
        response = self.post(self.pm2, reason="Gift is on leave")
        self.assertTemplateUsed(response, "tenders/tender_reassign_confirm.html")
        self.assertContains(response, "Reassign to Alice Test")
        self.assertContains(response, "Gift is on leave")
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.project_manager, self.pm)
        self.assertEqual(len(mail.outbox), 0)

    def test_confirming_reassigns_records_and_emails_both(self):
        before = TenderAssignment.objects.filter(tender=self.tender).count()
        with self.captureOnCommitCallbacks(execute=True):
            response = self.post(self.pm2, confirm=True, reason="Gift is on leave")
        self.assertRedirects(response, self.detail, fetch_redirect_response=False)
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.project_manager, self.pm2)
        moved = TenderAssignment.objects.filter(tender=self.tender).order_by("-id").first()
        self.assertEqual((moved.from_pm, moved.to_pm, moved.assigned_by, moved.reason),
                         (self.pm, self.pm2, self.md, "Gift is on leave"))
        self.assertEqual(TenderAssignment.objects.filter(tender=self.tender).count(), before + 1)
        self.assertEqual(sorted(mail.outbox[-1].to), sorted([self.pm.email, self.pm2.email]))

    def test_the_history_shows_every_move(self):
        self.post(self.pm2, confirm=True, reason="Gift is on leave")
        self.client.force_login(self.md)
        response = self.client.get(self.detail)
        self.assertContains(response, "Gift is on leave")
        self.assertContains(response, "holds it now")
        history = response.context["assignments"]
        self.assertEqual(history[-1].to_pm, self.pm2)

    def test_the_holder_cannot_be_chosen(self):
        response = self.post(self.pm, confirm=True)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors)
        self.tender.refresh_from_db()
        self.assertEqual(self.tender.project_manager, self.pm)

    def test_works_at_any_status(self):
        for status in (S.PENDING_VERIFICATION, S.PENDING_ELIGIBILITY_DECISION, S.SUBMITTED, S.WON, S.LOST, S.WITHDRAWN):
            with self.subTest(status=status):
                extra = {"previous_status": S.COSTING} if status == S.PENDING_ELIGIBILITY_DECISION else {}
                tender = self.new_tender(status, **extra)
                self.client.force_login(self.md)
                url = reverse("tenders:reassign", args=[tender.pk])
                self.client.post(url, {"new_pm": self.pm2.pk, "confirm": "yes"})
                tender.refresh_from_db()
                self.assertEqual((tender.project_manager, tender.status), (self.pm2, status))

    def test_with_no_other_active_pm_it_says_so(self):
        self.pm2.is_active = False
        self.pm2.save()
        self.client.force_login(self.md)
        self.assertContains(self.client.get(self.url), "no other active project manager")
