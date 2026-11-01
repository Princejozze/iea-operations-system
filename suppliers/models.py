"""
suppliers — app 4 in CLAUDE.md §1.

Holds: Supplier master data only — no tender or item dependency.

May import from: core, accounts, documents. Never from apps below it.
Every model holding business data inherits core.models.Auditable (R7).
"""

from django.db import models
from django.db.models.functions import Lower

from core.models import Auditable


class Supplier(Auditable):
    """
    A manufacturer, OEM or supplier IEA sources equipment from (§7).

    Master data only. ``items.Quote`` and ``approvals.EligibilityReview.jv_partner``
    point at this table from below; nothing here points down at them (§1).
    """

    name = models.CharField(max_length=255)
    is_oem = models.BooleanField(
        default=False,
        verbose_name="OEM",
        help_text="Original Equipment Manufacturer. The preferred source where one exists (§3).",
    )
    contact_person = models.CharField(max_length=255, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=50, blank=True)
    country = models.CharField(max_length=100, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            # Case-insensitive, like core.Client: "Mettler Toledo" and
            # "METTLER TOLEDO" are one supplier. Looser variants ("Ltd" vs
            # "Limited") are caught by suppliers.dedup before import.
            models.UniqueConstraint(Lower("name"), name="suppliers_supplier_name_ci_unique"),
        ]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        self.name = " ".join(self.name.split())
        super().save(*args, **kwargs)
