"""
Seed the CostingDefaults singleton with the IMPERIAL EAST AFRICA LIMITED MASTER
TEMPLATE 2026 values (CLAUDE.md section 4):

    shipping 10%   duty 25%   C&F 5%   labour 15,000 TZS/hour   overhead 10%
    margin 20%     maintenance 1%      VAT 18%
    cost of capital 22.25%  (the rate the template actually applies; whether and
                             how it enters the selling price is an open question
                             for Faraja, so it is stored but not yet used)

Historical models skip Auditable.save(), so this writes the CREATE AuditEntry
itself (R7). Safe to re-run: an existing row is left untouched.
"""

from decimal import Decimal

from django.db import migrations
from django.utils import timezone

VALUES = {
    "shipping_pct": Decimal("10"),
    "duty_pct": Decimal("25"),
    "cnf_pct": Decimal("5"),
    "labour_rate_tzs_per_hour": Decimal("15000"),
    "overhead_pct": Decimal("10"),
    "margin_pct": Decimal("20"),
    "maintenance_pct": Decimal("1"),
    "vat_pct": Decimal("18"),
    "cost_of_capital_pct": Decimal("22.25"),
}


def seed(apps, schema_editor):
    CostingDefaults = apps.get_model("core", "CostingDefaults")
    AuditEntry = apps.get_model("core", "AuditEntry")
    if CostingDefaults.objects.filter(pk=1).exists():
        return
    CostingDefaults.objects.create(pk=1, **VALUES)
    AuditEntry.objects.create(
        actor=None,
        action="CREATE",
        model="core.CostingDefaults",
        object_id="1",
        object_repr="Costing defaults",
        changes={"id": [None, 1], **{k: [None, str(v.quantize(Decimal("0.01")))] for k, v in VALUES.items()}},
        timestamp=timezone.now(),
    )


class Migration(migrations.Migration):
    dependencies = [("core", "0005_costingdefaults")]

    # Forward-only: deleting the singleton on un-apply would lose edited rates.
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
