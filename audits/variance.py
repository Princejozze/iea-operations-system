"""
Numeric variance flagging for QC audit lines (CLAUDE.md §7, §11).

Where the required and the offered specification both state a quantity of the
same kind, report the difference. FLAG, NEVER JUDGE: nothing here says whether a
difference is acceptable, a shortfall or a pass. It prints "required 656 kg,
offered 620 kg, -36 kg (-5.49%)" and the auditor decides (§11: the verdict is a
person's, and §14 forbids automating it).

Pure Python, no Django. Quantities are recognised only when a number is
immediately followed by a unit this module knows (so "24 x 1.5 ml" yields 1.5 ml
and ignores the 24, and "5 mg/ml" yields nothing rather than a wrong "5 mg").

What it deliberately does NOT do: pair quantities it cannot pair with
confidence. If a dimension appears a different number of times on each side it
emits one UNPAIRED flag for a human to read, rather than guessing which number
answers which. A quantity stated on only one side produces no flag at all —
"both sides state a comparable quantity" is the trigger.
"""

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

D = Decimal

# dimension -> {unit: factor to that dimension's base unit}. Case-insensitive.
# Ambiguous-by-case units (mW/MW, mPa/MPa) and ambiguous words (t, in, a, min)
# are left out on purpose: a missed flag is visible to the auditor in the text,
# a false one erodes trust in all of them.
UNITS = {
    "mass": {"mg": D("0.001"), "g": D(1), "kg": D(1000), "lb": D("453.59237"), "lbs": D("453.59237")},
    "length": {"mm": D("0.001"), "cm": D("0.01"), "m": D(1), "km": D(1000)},
    "volume": {"ul": D("0.000001"), "ml": D("0.001"), "cl": D("0.01"), "l": D(1)},
    "power": {"w": D(1), "kw": D(1000)},
    "voltage": {"v": D(1), "kv": D(1000)},
    "frequency": {"hz": D(1), "khz": D(1000)},
    "pressure": {"pa": D(1), "kpa": D(1000), "mbar": D(100), "bar": D(100000), "psi": D("6894.757293168")},
    "speed": {"rpm": D(1)},
    "temperature": {"°c": D(1)},
}
_UNIT_TO_DIMENSION = {unit: dim for dim, units in UNITS.items() for unit in units}

_NUM = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:[.,]\d+)?"
_UNIT = r"°\s*c|[a-zµ]+"
_RANGE_JOIN = r"\s*(?:-|–|—|to)\s*"
# A number glued to a letter or dot on its left ("M3", "v1.5") is not a quantity.
_QUANTITY = re.compile(
    rf"(?<![\w.])(?P<lo>{_NUM})(?:{_RANGE_JOIN}(?P<hi>{_NUM}))?\s*(?P<unit>{_UNIT})(?![\w/²³])",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Quantity:
    low: Decimal  # the value, or the lower end of a range, in the written unit
    high: Decimal | None  # upper end of a range, else None
    unit: str  # normalised key into UNITS
    dimension: str
    text: str  # exactly as written, for display

    @property
    def is_range(self):
        return self.high is not None

    def base(self, value):
        return value * UNITS[self.dimension][self.unit]


def _number(token):
    token = token.strip()
    if re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?", token):
        token = token.replace(",", "")  # 1,200 -> 1200
    else:
        token = token.replace(",", ".")  # 0,5 -> 0.5
    try:
        return D(token)
    except InvalidOperation:  # pragma: no cover - the regex only passes numbers
        return None


def _unit_key(raw):
    key = re.sub(r"\s+", "", raw.lower()).replace("µ", "u")
    return key if key in _UNIT_TO_DIMENSION else None


def extract_quantities(text):
    """Every unit-bearing quantity in ``text``, in reading order."""
    found = []
    for m in _QUANTITY.finditer(text or ""):
        unit = _unit_key(m.group("unit"))
        low = _number(m.group("lo"))
        high = _number(m.group("hi")) if m.group("hi") else None
        if unit is None or low is None:
            continue
        if high is not None and high < low:  # "20-10 kg" is not a range we understand
            continue
        found.append(Quantity(low, high, unit, _UNIT_TO_DIMENSION[unit], " ".join(m.group(0).split())))
    return found


def _fmt(value):
    """Plain decimal text, no exponent, no trailing zeros: 36, -5.49, 0.1."""
    text = format(value.normalize(), "f")
    return "0" if text in ("-0", "") else text


def _flag(kind, dimension, required, offered, **extra):
    return {"type": kind, "dimension": dimension, "required": required, "offered": offered, **extra}


def _compare(req, off):
    """One flag if the pair differs, else None."""
    if req.is_range or off.is_range:
        r_lo, r_hi = req.base(req.low), req.base(req.high if req.is_range else req.low)
        o_lo, o_hi = off.base(off.low), off.base(off.high if off.is_range else off.low)
        same = (r_lo, r_hi) == (o_lo, o_hi)
        inside = not req.is_range or (not off.is_range and r_lo <= o_lo <= r_hi)
        if same or inside:
            return None
        return _flag("RANGE", req.dimension, req.text, off.text)
    required, offered = req.base(req.low), off.base(off.low)
    if required == offered:
        return None
    # Difference expressed in the REQUIRED side's unit, so it reads like the requirement.
    offered_in_req_unit = offered / UNITS[req.dimension][req.unit]
    diff = offered_in_req_unit - req.low
    percent = None if req.low == 0 else ((diff / req.low) * 100).quantize(D("0.01"))
    return _flag(
        "DIFFERENCE", req.dimension, req.text, off.text,
        unit=req.unit, required_value=_fmt(req.low), offered_value=_fmt(offered_in_req_unit),
        difference=_fmt(diff), percent=None if percent is None else _fmt(percent),
    )


def compare_specifications(required_text, offered_text):
    """
    Flags for the quantities both texts state. Returns a list of dicts, each
    with ``type`` DIFFERENCE, RANGE or UNPAIRED, ``dimension``, and the
    ``required`` / ``offered`` text as written (JSON-safe, so it can be stored).
    An empty list means nothing comparable differed — NOT that the line complies.
    """
    required, offered = extract_quantities(required_text), extract_quantities(offered_text)
    flags = []
    for dimension in UNITS:
        req = [q for q in required if q.dimension == dimension]
        off = [q for q in offered if q.dimension == dimension]
        if not req or not off:
            continue
        if len(req) == len(off):
            flags += [f for f in (_compare(r, o) for r, o in zip(req, off)) if f]
        else:
            flags.append(_flag(
                "UNPAIRED", dimension,
                "; ".join(q.text for q in req), "; ".join(q.text for q in off),
            ))
    return flags


def describe(flag):
    """One human line for a flag, for templates, emails and the admin."""
    label = flag["dimension"].capitalize()
    if flag["type"] == "DIFFERENCE":
        pct = f", {flag['percent']}%" if flag.get("percent") is not None else ""
        sign = "+" if not flag["difference"].startswith("-") else ""
        return (
            f"{label}: required {flag['required']}, offered {flag['offered']} "
            f"({sign}{flag['difference']} {flag['unit']}{pct})"
        )
    if flag["type"] == "RANGE":
        return f"{label}: required {flag['required']}, offered {flag['offered']} (range, check by hand)"
    return (
        f"{label}: required {flag['required']}; offered {flag['offered']} "
        "(different counts of this quantity, check by hand)"
    )
