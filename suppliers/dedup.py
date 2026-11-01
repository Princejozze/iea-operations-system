"""
Supplier de-duplication for the legacy import (CLAUDE.md §7, §11).

The Smartsheet exports hold 426 supplier name strings for roughly 62 real
companies: "Mettler Toledo", "METTLER TOLEDO LTD.", "Mettler-Toledo Ltd" and so
on. This module is pure Python — no database, no Django — so it is trivially
testable and the importer decides what to do with the result.

Two tiers, because §11 says "system drafts, person confirms":

* ``group_exact``   merges names whose normalised key is identical. Safe to
                    apply automatically.
* ``suggest_fuzzy`` proposes merges between *different* keys that look like
                    typos of each other. Never applied automatically: a person
                    reviews them, because "Alpha Labs" vs "Alpha Lab" can be
                    one company or two.
"""

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from difflib import SequenceMatcher

# Legal-form words that say nothing about *which* company it is. Removed only
# as whole tokens, so "Limited" is dropped but "Unlimited Labs" keeps "unlimited".
LEGAL_SUFFIXES = frozenset({
    "ltd", "limited", "llc", "inc", "incorporated", "corp", "corporation",
    "co", "company", "plc", "gmbh", "ag", "sa", "bv", "nv", "srl", "spa",
    "pty", "pvt", "private", "llp", "kg", "ab", "as", "oy", "sdn", "bhd",
})


def normalise_name(name):
    """
    Reduce a supplier name to a comparison key. Never stored or displayed.

    Case, accents, punctuation, "&" vs "and", spacing and legal-form words are
    ignored. Returns "" if nothing meaningful is left (the caller must skip it).
    """
    text = unicodedata.normalize("NFKD", name or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = text.replace("&", " and ")
    tokens = re.sub(r"[^a-z0-9]+", " ", text).split()
    meaningful = [t for t in tokens if t not in LEGAL_SUFFIXES and t != "the"]
    # A name made only of suffix words (e.g. "Co Ltd") would otherwise vanish;
    # fall back to every token so it still compares with itself.
    return " ".join(meaningful or tokens)


@dataclass
class DuplicateGroup:
    """One real company and every spelling of it found in the legacy data."""

    key: str
    canonical: str
    variants: list = field(default_factory=list)  # distinct spellings, canonical first
    occurrences: int = 0  # how many raw rows collapsed into this group


def _tidy(name):
    return " ".join(name.split())


def _pick_canonical(spellings):
    """Most common spelling wins; ties prefer a mixed-case form (not shouting
    or all-lower), then the longer one (keeps "Ltd"), then alphabetical so the
    result is deterministic."""
    counts = Counter(spellings)

    def rank(item):
        spelling, count = item
        mixed = spelling not in (spelling.upper(), spelling.lower())
        return (-count, not mixed, -len(spelling), spelling)

    return min(counts.items(), key=rank)[0]


def group_exact(names):
    """
    Collapse raw name strings into groups of identical normalised key.

    Blank and unnormalisable names are returned separately — never guessed at,
    in the same spirit as §12 "quarantine, never discard".

    Returns ``(groups, unusable)``; groups are sorted by canonical name.
    """
    by_key = {}
    unusable = []
    for raw in names:
        tidy = _tidy(raw or "")
        key = normalise_name(tidy)
        if not key:
            unusable.append(raw)
            continue
        by_key.setdefault(key, []).append(tidy)

    groups = []
    for key, spellings in by_key.items():
        canonical = _pick_canonical(spellings)
        variants = [canonical] + sorted({s for s in spellings if s != canonical})
        groups.append(DuplicateGroup(key, canonical, variants, len(spellings)))
    groups.sort(key=lambda g: g.canonical.lower())
    return groups, unusable


@dataclass(frozen=True)
class FuzzySuggestion:
    left: DuplicateGroup
    right: DuplicateGroup
    score: float


def suggest_fuzzy(groups, threshold=0.9):
    """
    Pairs of groups whose keys are near-identical but not equal (typos such as
    "Shimadzu" / "Shimazdu"). For human review only — nothing here merges.

    Keys shorter than 5 characters are never compared: on short strings one
    letter is a big fraction and the false-positive rate is too high.
    Highest score first.
    """
    out = []
    ordered = sorted((g for g in groups if len(g.key) >= 5), key=lambda g: g.key)
    for i, a in enumerate(ordered):
        for b in ordered[i + 1:]:
            score = SequenceMatcher(None, a.key, b.key).ratio()
            if score >= threshold:
                out.append(FuzzySuggestion(a, b, round(score, 3)))
    out.sort(key=lambda s: (-s.score, s.left.key))
    return out


def find_existing(name, suppliers):
    """
    The first supplier in ``suppliers`` (any iterable of objects with ``.name``)
    whose normalised key equals ``name``'s, or None. Lets a re-run of the import
    attach to rows it created earlier instead of duplicating them.
    """
    key = normalise_name(name)
    if not key:
        return None
    for supplier in suppliers:
        if normalise_name(supplier.name) == key:
            return supplier
    return None
