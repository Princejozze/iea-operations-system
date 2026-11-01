"""
The costing chain, CLAUDE.md §4 — IMPERIAL EAST AFRICA LIMITED MASTER TEMPLATE 2026.

A pure function: numbers in, numbers out, no database, no clock. ``Item`` feeds
it the rates it snapshotted and stores every figure it returns (§4: "store every
intermediate value ... so a bid submitted months ago can still be explained line
by line").

    import_material  = fob_unit_price × fx_rate × quantity
    shipping         = import_material × shipping_rate     ┐
    cif              = import_material + shipping          │ only when
    duty             = cif × duty_rate                     │ shipping_applies,
    cnf              = cif × cnf_rate                      ┘ otherwise all zero
    total_material   = (local_unit_cost × quantity) + import_material
                       + shipping + duty + cnf
    risk_management  = total_material × risk_rate
    overhead         = (total_material + risk_management) × overhead_rate
    margin           = (total_material + risk_management + overhead) × margin_rate
    material_selling = total_material + risk_management + overhead + margin

    labour_hours     = hours_per_unit × quantity
    labour_cost      = labour_hours × labour_rate
    labour_selling   = labour_cost + labour_cost × overhead_rate + labour_cost × margin_rate
                     = labour_cost × (1 + overhead_rate + margin_rate)     ADDITIVE: 1.30, not 1.32

    maintenance      = (material_selling + labour_selling) × maintenance_rate
    total_selling    = material_selling + labour_selling + maintenance
    unit_rate        = floor(total_selling / quantity)

Cost of capital (``total_material × cost_of_capital_rate``) is computed and
returned but is NOT part of ``total_selling``: the template computes it and
never adds it, and two questions to Faraja are still open (§4). Do not add it
until they are answered.

Arithmetic is exact Decimal at 60 significant digits. Every returned figure is
quantised to SIX decimal places — what the database stores — and ``unit_rate``
is taken from the quantised total, so what is stored always agrees with itself
(the database enforces unit_rate × quantity ≤ total_selling < (unit_rate + 1) × quantity).
"""

from dataclasses import dataclass, fields as dataclass_fields
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal, localcontext

ZERO = Decimal(0)
STORED_PLACES = Decimal("0.000001")  # six decimal places, matching the model fields


def _d(value):
    return value if isinstance(value, Decimal) else Decimal(str(value))


@dataclass(frozen=True)
class CostingInputs:
    """The per-item inputs (§4). ``risk_rate`` is a FRACTION (0.05 is 5%);
    ``fx_rate`` is TZS per one unit of the item's currency."""

    fob_unit_price: Decimal
    fx_rate: Decimal
    local_unit_cost: Decimal
    quantity: int
    shipping_applies: bool
    risk_rate: Decimal
    labour_hours_per_unit: Decimal


@dataclass(frozen=True)
class CostBreakdown:
    """Every intermediate and final figure, all in TZS."""

    import_material: Decimal
    shipping: Decimal
    cif: Decimal
    duty: Decimal
    cnf: Decimal
    local_material: Decimal
    total_material: Decimal
    cost_of_capital: Decimal  # computed and stored, NOT in total_selling
    risk_management: Decimal
    overhead: Decimal
    margin: Decimal
    material_selling: Decimal
    labour_hours: Decimal
    labour_cost: Decimal
    labour_selling: Decimal
    maintenance: Decimal
    total_selling: Decimal
    unit_rate: int

    def as_dict(self):
        return {f.name: getattr(self, f.name) for f in dataclass_fields(self)}


def compute_costing(inputs, rates):
    """Run the §4 chain. ``rates`` is a ``core.models.CostingRates`` (fractions)."""
    if inputs.quantity < 1:
        raise ValueError("quantity must be at least 1")
    with localcontext() as ctx:
        ctx.prec = 60
        quantity = Decimal(inputs.quantity)

        import_material = _d(inputs.fob_unit_price) * _d(inputs.fx_rate) * quantity
        if inputs.shipping_applies:
            shipping = import_material * rates.shipping_rate
            cif = import_material + shipping
            duty = cif * rates.duty_rate
            cnf = cif * rates.cnf_rate
        else:
            shipping = cif = duty = cnf = ZERO

        local_material = _d(inputs.local_unit_cost) * quantity
        total_material = local_material + import_material + shipping + duty + cnf
        cost_of_capital = total_material * rates.cost_of_capital_rate  # stored, never added

        risk_management = total_material * _d(inputs.risk_rate)
        overhead = (total_material + risk_management) * rates.overhead_rate
        margin = (total_material + risk_management + overhead) * rates.margin_rate
        material_selling = total_material + risk_management + overhead + margin

        labour_hours = _d(inputs.labour_hours_per_unit) * quantity
        labour_cost = labour_hours * rates.labour_rate_tzs_per_hour
        # Additive, literally as the template writes it: AB + (overhead × AB) + (margin × AB).
        labour_selling = labour_cost + labour_cost * rates.overhead_rate + labour_cost * rates.margin_rate

        maintenance = (material_selling + labour_selling) * rates.maintenance_rate
        total_selling = material_selling + labour_selling + maintenance

        figures = dict(
            import_material=import_material, shipping=shipping, cif=cif, duty=duty, cnf=cnf,
            local_material=local_material, total_material=total_material,
            cost_of_capital=cost_of_capital, risk_management=risk_management,
            overhead=overhead, margin=margin, material_selling=material_selling,
            labour_hours=labour_hours, labour_cost=labour_cost, labour_selling=labour_selling,
            maintenance=maintenance, total_selling=total_selling,
        )
        figures = {name: value.quantize(STORED_PLACES, rounding=ROUND_HALF_UP) for name, value in figures.items()}
        unit_rate = int((figures["total_selling"] / quantity).to_integral_value(rounding=ROUND_FLOOR))
        return CostBreakdown(unit_rate=unit_rate, **figures)
