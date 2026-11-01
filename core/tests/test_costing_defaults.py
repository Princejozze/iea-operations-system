"""CostingDefaults (CLAUDE.md section 4): a seeded, audited, admin-editable singleton."""

import dataclasses
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from django.urls import reverse

from core.models import AuditAction, AuditEntry, CostingDefaults, CostingRates

D = Decimal

# CLAUDE.md section 4 table, as the percentages stored in the database.
SPEC_PERCENTS = {
    "shipping_pct": D("10"), "duty_pct": D("25"), "cnf_pct": D("5"), "overhead_pct": D("10"),
    "margin_pct": D("20"), "maintenance_pct": D("1"), "vat_pct": D("18"),
    "cost_of_capital_pct": D("22.25"),
}


def entries():
    return AuditEntry.objects.filter(model="core.CostingDefaults").order_by("id")


def make_user(role, name, **extra):
    extra.setdefault("must_change_password", False)
    return get_user_model().objects.create_user(
        email=f"{name}@imperialeastafrica.tz", password="x", role=role, **extra
    )


class SeedTests(TestCase):
    def test_exactly_one_row_with_the_template_values(self):
        row = CostingDefaults.objects.get()
        self.assertEqual(row.pk, 1)
        for name, value in SPEC_PERCENTS.items():
            with self.subTest(field=name):
                self.assertEqual(getattr(row, name), value)
        self.assertEqual(row.labour_rate_tzs_per_hour, D("15000"))

    def test_the_seed_is_audited_as_a_system_create(self):
        entry = entries().get(action=AuditAction.CREATE)
        self.assertIsNone(entry.actor)
        self.assertEqual(entry.changes["margin_pct"], [None, "20.00"])
        self.assertEqual(entry.changes["labour_rate_tzs_per_hour"], [None, "15000.00"])

    def test_seed_values_match_the_field_defaults(self):
        # The migration seeds explicit values; the current() fallback uses the
        # field defaults. They must never drift apart.
        fresh = CostingDefaults()
        for name, value in SPEC_PERCENTS.items():
            self.assertEqual(getattr(fresh, name), value)
        self.assertEqual(fresh.labour_rate_tzs_per_hour, D("15000"))


class CurrentRatesTests(TestCase):
    def test_current_returns_fractions_ready_to_multiply(self):
        r = CostingDefaults.current()
        self.assertIsInstance(r, CostingRates)
        self.assertEqual(
            (r.shipping_rate, r.duty_rate, r.cnf_rate, r.overhead_rate, r.margin_rate,
             r.maintenance_rate, r.vat_rate, r.cost_of_capital_rate),
            (D("0.10"), D("0.25"), D("0.05"), D("0.10"), D("0.20"), D("0.01"), D("0.18"), D("0.2225")),
        )
        self.assertEqual(r.labour_rate_tzs_per_hour, D("15000"))

    def test_rates_are_exact_decimals_not_floats(self):
        for key, value in CostingDefaults.current().as_dict().items():
            with self.subTest(field=key):
                self.assertIsInstance(value, Decimal)

    def test_labour_markup_is_1_30_by_default_and_follows_edits(self):
        self.assertEqual(CostingDefaults.current().labour_markup, D("1.30"))
        row = CostingDefaults.objects.get()
        row.margin_pct = D("25")
        row.save()
        self.assertEqual(CostingDefaults.current().labour_markup, D("1.35"))

    def test_the_snapshot_is_immutable(self):
        rates = CostingDefaults.current()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            rates.margin_rate = D("0.99")

    def test_an_edit_reaches_the_next_costing_but_never_a_snapshot_already_taken(self):
        before = CostingDefaults.current()  # what an Item stores when costed
        row = CostingDefaults.objects.get()
        row.margin_pct = D("30")
        row.save()
        self.assertEqual(before.margin_rate, D("0.20"))  # past bid not repriced
        self.assertEqual(CostingDefaults.current().margin_rate, D("0.30"))

    def test_as_dict_can_be_copied_onto_an_item(self):
        d = CostingDefaults.current().as_dict()
        self.assertEqual(
            set(d),
            {"shipping_rate", "duty_rate", "cnf_rate", "labour_rate_tzs_per_hour", "overhead_rate",
             "margin_rate", "maintenance_rate", "vat_rate", "cost_of_capital_rate"},
        )

    def test_a_missing_row_is_recreated_with_spec_values_and_audited(self):
        # Simulates the flushed-table case; deletion itself is blocked (below).
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM core_costingdefaults")
        self.assertEqual(CostingDefaults.current().margin_rate, D("0.20"))
        self.assertEqual(CostingDefaults.objects.count(), 1)
        self.assertEqual(entries().filter(action=AuditAction.CREATE).count(), 2)  # seed + recreate


class SingletonTests(TestCase):
    def test_a_second_row_is_refused(self):
        with self.assertRaises(ValidationError):
            CostingDefaults().save()
        with self.assertRaises(ValidationError):
            CostingDefaults(pk=2).save()
        self.assertEqual(CostingDefaults.objects.count(), 1)

    def test_the_database_refuses_a_second_row_even_around_save(self):
        with self.assertRaises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO core_costingdefaults (id, shipping_pct, duty_pct, cnf_pct, "
                "labour_rate_tzs_per_hour, overhead_pct, margin_pct, maintenance_pct, vat_pct, "
                "cost_of_capital_pct, created_at, updated_at) "
                "VALUES (2, 1, 1, 1, 1, 1, 1, 1, 1, 1, now(), now())"
            )

    def test_cannot_be_deleted(self):
        row = CostingDefaults.objects.get()
        with self.assertRaises(ValidationError):
            row.delete()
        with self.assertRaises(ValidationError):
            CostingDefaults.objects.all().delete()
        self.assertEqual(CostingDefaults.objects.count(), 1)


class ValidationAndAuditTests(TestCase):
    def setUp(self):
        self.md = make_user("EXEC", "faraja")

    def test_edit_is_audited_with_old_new_and_the_person(self):
        row = CostingDefaults.objects.get()
        row.margin_pct = D("25")
        row.labour_rate_tzs_per_hour = D("18000")
        row.save(actor=self.md)
        entry = entries().exclude(action=AuditAction.CREATE).get()
        self.assertEqual(entry.action, AuditAction.UPDATE)
        self.assertEqual(entry.actor, self.md)
        self.assertEqual(
            entry.changes,
            {"margin_pct": ["20.00", "25.00"], "labour_rate_tzs_per_hour": ["15000.00", "18000.00"]},
        )

    def test_percentages_must_be_between_0_and_100(self):
        for bad in (D("-0.01"), D("100.01"), D("2000")):
            with self.subTest(value=bad):
                row = CostingDefaults.objects.get()
                row.vat_pct = bad
                with self.assertRaises(ValidationError):
                    row.full_clean()

    def test_database_enforces_the_percentage_range_too(self):
        with self.assertRaises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("UPDATE core_costingdefaults SET vat_pct = 180")

    def test_labour_rate_cannot_be_negative(self):
        row = CostingDefaults.objects.get()
        row.labour_rate_tzs_per_hour = D("-1")
        with self.assertRaises(ValidationError):
            row.full_clean()

    def test_queryset_update_is_refused_like_every_audited_model(self):
        from core.models import AuditBypassError

        with self.assertRaises(AuditBypassError):
            CostingDefaults.objects.update(margin_pct=D("99"))
        self.assertEqual(CostingDefaults.objects.get().margin_pct, D("20"))


class AdminTests(TestCase):
    """Role decides, not Django permission flags: EXEC and ADMIN only."""

    def setUp(self):
        self.row = CostingDefaults.objects.get()
        self.change_url = reverse("admin:core_costingdefaults_change", args=[self.row.pk])
        self.md = make_user("EXEC", "faraja", is_staff=True)
        self.sysadmin = make_user("ADMIN", "janeth", is_staff=True)

    def post_margin(self, user, value="25"):
        self.client.force_login(user)
        data = {name: str(getattr(self.row, name)) for name in CostingDefaults.PERCENT_FIELDS}
        data["labour_rate_tzs_per_hour"] = str(self.row.labour_rate_tzs_per_hour)
        data["margin_pct"] = value
        return self.client.post(self.change_url, data)

    def test_exec_edits_a_margin_and_it_is_audited_under_their_name(self):
        response = self.post_margin(self.md)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(CostingDefaults.current().margin_rate, D("0.25"))
        entry = entries().exclude(action=AuditAction.CREATE).get()
        self.assertEqual(entry.actor, self.md)
        self.assertEqual(entry.changes, {"margin_pct": ["20.00", "25.00"]})

    def test_system_admin_may_also_edit(self):
        self.assertEqual(self.post_margin(self.sysadmin, "21").status_code, 302)
        self.assertEqual(CostingDefaults.objects.get().margin_pct, D("21"))

    def test_no_other_role_can_see_or_change_it_even_as_superuser(self):
        for role in ("HOPS", "PM", "AUDIT", "FIN", "CLEAR"):
            for superuser in (False, True):
                with self.subTest(role=role, superuser=superuser):
                    user = make_user(
                        role, f"{role.lower()}{int(superuser)}", is_staff=True, is_superuser=superuser
                    )
                    self.client.force_login(user)
                    self.assertEqual(self.client.get(self.change_url).status_code, 403)
                    self.assertEqual(self.post_margin(user, "99").status_code, 403)
        self.assertEqual(CostingDefaults.objects.get().margin_pct, D("20"))

    def test_the_list_page_goes_straight_to_the_one_row(self):
        self.client.force_login(self.md)
        response = self.client.get(reverse("admin:core_costingdefaults_changelist"))
        self.assertRedirects(response, self.change_url, fetch_redirect_response=False)

    def test_cannot_add_a_second_or_delete(self):
        self.client.force_login(self.md)
        self.assertEqual(self.client.get(reverse("admin:core_costingdefaults_add")).status_code, 403)
        delete_url = reverse("admin:core_costingdefaults_delete", args=[self.row.pk])
        self.assertEqual(self.client.get(delete_url).status_code, 403)
        self.assertEqual(self.client.post(delete_url, {"post": "yes"}).status_code, 403)
        self.assertEqual(CostingDefaults.objects.count(), 1)

    def test_an_out_of_range_value_is_rejected_by_the_form(self):
        response = self.post_margin(self.md, "250")
        self.assertEqual(response.status_code, 200)  # form redisplayed with an error
        self.assertEqual(CostingDefaults.objects.get().margin_pct, D("20"))


class NotInSettingsTests(TestCase):
    def test_no_costing_constants_live_in_settings(self):
        banned = ("SHIPPING", "DUTY", "CNF", "LABOUR", "OVERHEAD", "MARGIN", "MAINTENANCE", "VAT", "COST_OF_CAPITAL")
        found = [n for n in dir(settings) if n.isupper() and any(b in n for b in banned)]
        self.assertEqual(found, [], "CLAUDE.md section 4: costing defaults live in CostingDefaults, not settings.py")
