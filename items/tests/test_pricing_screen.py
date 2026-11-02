"""
The MD's pricing screen (R22) over TenderCostingRates.preview() and .apply().

Lives in items/tests because the totals need real priced items. Figures are the
hand-worked ones from test_r22_tender_rates: one item at 5,398,399.5 TZS at the
baseline, 5,174,728.6875 at a 15% margin.
"""

from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from core.models import AuditEntry
from items.models import Item
from tenders.models import TenderCostingRates, TenderStatus as S

from .helpers import ItemFixtures, force_status

D = Decimal
HX = {"HTTP_HX_REQUEST": "true"}


def rate_params(**overrides):
    values = {
        "shipping_pct": "10", "duty_pct": "25", "cnf_pct": "5", "labour_rate_tzs_per_hour": "15000",
        "overhead_pct": "10", "margin_pct": "20", "maintenance_pct": "1", "cost_of_capital_pct": "22.25",
    }
    values.update(overrides)
    return values


class PricingScreenTests(ItemFixtures, TestCase):
    def setUp(self):
        super().setUp()
        self.tender = self.costing()
        self.item = self.priced_item(self.tender)
        self.url = reverse("tenders:rates", args=[self.tender.pk])
        self.apply_url = reverse("tenders:rates_apply", args=[self.tender.pk])

    # -- what each role sees -----------------------------------------------------

    def test_md_gets_an_input_per_rate_and_the_current_total(self):
        self.client.force_login(self.md)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        for name in TenderCostingRates.RATE_FIELDS:
            self.assertContains(response, f'name="{name}"')
        self.assertContains(response, "5,398,400")  # 5,398,399.5 rounded to whole TZS
        self.assertContains(response, "Review and apply")
        self.assertContains(response, 'hx-get="%s"' % self.url)

    def test_formulas_are_described_never_editable(self):
        self.client.force_login(self.md)
        response = self.client.get(self.url)
        self.assertContains(response, "% of CIF")
        self.assertNotContains(response, 'name="formula')
        self.assertEqual(len(response.context["form"].fields), len(TenderCostingRates.RATE_FIELDS))

    def test_other_readers_see_the_rates_read_only(self):
        for user in (self.pm, self.hops, self.auditor, self.finance):
            with self.subTest(role=user.role):
                self.client.force_login(user)
                response = self.client.get(self.url)
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, 'name="margin_pct"')
                self.assertNotContains(response, "Review and apply")
                self.assertContains(response, "Only the MD sets")
                self.assertNotContains(response, "At these rates")

    def test_others_cannot_preview_or_apply(self):
        self.client.force_login(self.pm)
        self.assertEqual(self.client.get(self.url, rate_params(margin_pct="15"), **HX).status_code, 403)
        response = self.client.post(self.apply_url, {**rate_params(margin_pct="15"), "confirm": "yes"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(TenderCostingRates.objects.get(tender=self.tender).margin_pct, D("20"))

    def test_clearing_agents_see_nothing(self):
        from accounts.models import Role
        from tenders.tests.helpers import make_user

        self.client.force_login(make_user(Role.CLEARING_AGENT, "clear"))
        self.assertEqual(self.client.get(self.url).status_code, 403)

    # -- live preview ------------------------------------------------------------

    def test_preview_shows_baseline_candidate_and_difference_and_saves_nothing(self):
        self.client.force_login(self.md)
        entries = AuditEntry.objects.count()
        response = self.client.get(self.url, rate_params(margin_pct="15"), **HX)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "tenders/_rates_result.html")
        self.assertTemplateNotUsed(response, "base.html")
        c = response.context["comparison"]
        self.assertEqual((c["baseline"], c["candidate"]), ("5,398,400", "5,174,729"))
        # 5,174,728.6875 - 5,398,399.5 = -223,670.8125 -> -4.14%
        self.assertEqual((c["diff"], c["diff_pct"]), ("−223,671", "−4.14%"))
        self.assertContains(response, "Margin")
        self.assertEqual(AuditEntry.objects.count(), entries)
        self.assertEqual(TenderCostingRates.objects.get(tender=self.tender).margin_pct, D("20"))
        self.assertEqual(Item.objects.get(pk=self.item.pk).total_selling_price_tzs, D("5398399.5"))

    def test_unpriced_items_are_counted_and_flagged(self):
        self.make_item(self.tender)
        self.client.force_login(self.md)
        response = self.client.get(self.url, rate_params(margin_pct="15"), **HX)
        c = response.context["comparison"]
        self.assertEqual((c["priced"], c["unpriced"]), (1, 1))
        self.assertContains(response, "The total is incomplete")

    def test_an_invalid_rate_shows_an_error_not_a_total(self):
        self.client.force_login(self.md)
        response = self.client.get(self.url, rate_params(margin_pct="140"), **HX)
        self.assertIsNone(response.context["comparison"])
        self.assertContains(response, "Cannot total those rates")

    def test_preview_works_without_javascript(self):
        self.client.force_login(self.md)
        response = self.client.get(self.url, rate_params(margin_pct="15"))
        self.assertTemplateUsed(response, "tenders/tender_rates.html")
        self.assertEqual(response.context["comparison"]["candidate"], "5,174,729")
        self.assertContains(response, 'value="15"')

    def test_inputs_show_plain_numbers(self):
        self.client.force_login(self.md)
        response = self.client.get(self.url)
        self.assertContains(response, 'value="15000"')
        self.assertNotContains(response, 'value="20.00"')

    # -- apply, with confirmation ------------------------------------------------

    def test_apply_asks_for_confirmation_first_and_changes_nothing(self):
        self.client.force_login(self.md)
        response = self.client.post(self.apply_url, rate_params(margin_pct="15"))
        self.assertTemplateUsed(response, "tenders/tender_rates_confirm.html")
        self.assertContains(response, "This changes the bid price")
        self.assertContains(response, 'name="confirm" value="yes"')
        self.assertEqual(TenderCostingRates.objects.get(tender=self.tender).margin_pct, D("20"))

    def test_confirmed_apply_saves_and_recosts(self):
        self.client.force_login(self.md)
        response = self.client.post(self.apply_url, {**rate_params(margin_pct="15"), "confirm": "yes"}, follow=True)
        self.assertRedirects(response, self.url)
        self.assertEqual(TenderCostingRates.objects.get(tender=self.tender).margin_pct, D("15"))
        self.assertEqual(Item.objects.get(pk=self.item.pk).total_selling_price_tzs, D("5174728.6875"))
        self.assertContains(response, "5,398,400 → 5,174,729")

    def test_applying_unchanged_rates_is_a_no_op(self):
        self.client.force_login(self.md)
        response = self.client.post(self.apply_url, {**rate_params(), "confirm": "yes"}, follow=True)
        self.assertContains(response, "nothing to apply")

    # -- locked tenders ----------------------------------------------------------

    def test_locked_tender_shows_reason_and_still_previews_but_cannot_apply(self):
        force_status(self.tender, S.PENDING_AUDIT)
        self.client.force_login(self.md)
        response = self.client.get(self.url)
        self.assertContains(response, "Rates locked")
        self.assertContains(response, "left costing for QC audit")
        self.assertNotContains(response, "Review and apply")
        preview = self.client.get(self.url, rate_params(margin_pct="15"), **HX)
        self.assertEqual(preview.context["comparison"]["candidate"], "5,174,729")
        response = self.client.post(self.apply_url, {**rate_params(margin_pct="15"), "confirm": "yes"}, follow=True)
        self.assertContains(response, "were not changed")
        self.assertEqual(TenderCostingRates.objects.get(tender=self.tender).margin_pct, D("20"))

    def test_eligibility_freeze_has_its_own_reason(self):
        force_status(self.tender, S.PENDING_ELIGIBILITY_DECISION, previous_status=S.COSTING)
        self.client.force_login(self.md)
        self.assertContains(self.client.get(self.url), "R9a")

    def test_tender_detail_links_to_the_rates(self):
        self.client.force_login(self.pm)
        response = self.client.get(reverse("tenders:detail", args=[self.tender.pk]))
        self.assertContains(response, self.url)
