"""Shared setup for items tests. The cast of users comes from tenders.tests.helpers."""

from decimal import Decimal

from django.core.files.base import ContentFile

from core.models import CostingRates
from items.models import AttachmentKind, ExchangeRate, Item, ItemAttachment
from suppliers.models import Supplier
from tenders.models import Currency, TenderStatus as S
from tenders.tests.helpers import TenderFixtures, force_status, make_user, status_entries  # noqa: F401

D = Decimal

# The §4 table as fractions — written out here, independently of the code under test.
SPEC_RATES = CostingRates(
    shipping_rate=D("0.10"), duty_rate=D("0.25"), cnf_rate=D("0.05"),
    labour_rate_tzs_per_hour=D("15000"), overhead_rate=D("0.10"), margin_rate=D("0.20"),
    maintenance_rate=D("0.01"), vat_rate=D("0.18"), cost_of_capital_rate=D("0.2225"),
)


class ItemFixtures(TenderFixtures):
    """TenderFixtures plus a USD rate, a supplier and item builders."""

    def setUp(self):
        super().setUp() if hasattr(super(), "setUp") else None
        self.usd = ExchangeRate.objects.create(currency=Currency.USD, rate_to_tzs=D("2500"), source="test")
        self.supplier = Supplier.objects.create(name="Eppendorf SE", is_oem=True, country="Germany")

    def drafting(self):
        return self.new_tender(S.DRAFTING)

    def costing(self):
        return self.new_tender(S.COSTING)

    def make_item(self, tender=None, **fields):
        fields.setdefault("specification", "Benchtop centrifuge, 24 x 1.5 ml")
        fields.setdefault("quantity", 1)
        return Item.objects.create(tender=tender or self.drafting(), **fields)

    def priced_item(self, tender=None, **fields):
        """The hand-worked example in test_costing: 100 USD FOB at 2500, qty 10."""
        fields.setdefault("quantity", 10)
        fields.setdefault("fob_unit_price", D("100"))
        fields.setdefault("currency", Currency.USD)
        fields.setdefault("risk_pct", D("5"))
        fields.setdefault("labour_hours_per_unit", D("2"))
        return self.make_item(tender, **fields)

    def complete_item(self, tender, *, omit=(), attachments=tuple(AttachmentKind.values), **fields):
        """An item with every field R2 requires before audit, minus ``omit``."""
        base = dict(
            unit_cost=D("1200.00"), currency=Currency.USD, incoterm="CIF", unit_of_measure="each",
            category="CLINICAL LAB", make_brand="Eppendorf", model_part_number="5424 R",
            country_of_origin="Germany", supplier=self.supplier,
        )
        for name in omit:
            base[name] = None if name in ("unit_cost", "supplier") else ""
        base.update(fields)
        item = self.make_item(tender, **base)
        for kind in attachments:
            self.attach(item, kind)
        return item

    def attach(self, item, kind):
        return ItemAttachment.objects.create(
            item=item, kind=kind, uploaded_by=self.pm, original_filename=f"{kind.lower()}.pdf",
            file=ContentFile(b"%PDF-1.4", name=f"{kind.lower()}.pdf"),
        )

    def quote(self, item, *, user=None, document=b"%PDF-1.4 supplier quotation", **figures):
        """A RECEIVED quote entered the real way: original document plus figures."""
        from items.services.quotes import enter_quote

        figures.setdefault("supplier", self.supplier)
        figures.setdefault("quoted_unit_price", D("1500.00"))
        figures.setdefault("currency", Currency.USD)
        return enter_quote(
            item, user=user or self.pm, original_document=ContentFile(document, name="quotation.pdf"), **figures
        )
