"""
Context-contract helpers shared by every document kind (RFQ, MAF, resolution...).

A context is a plain dict of strings, numbers, booleans, lists and dicts. The
caller flattens its records into that; this module never sees a model or enum.
"""

import json

from django.core.exceptions import ValidationError


def is_plain(value):
    """True for exactly what json round-trips unchanged: no Decimal, date,
    set, model instance or enum — those belong to the caller to flatten."""
    if value is None or isinstance(value, (str, bool, int, float)):
        return type(value) in (type(None), str, bool, int, float)
    if isinstance(value, (list, tuple)):
        return all(is_plain(v) for v in value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and is_plain(v) for k, v in value.items())
    return False


def validate_context(context, *, label, required, rows=None):
    """
    Check ``context`` and return it as a deep, JSON-clean copy.

    ``required``: top-level keys that must be non-empty.
    ``rows``: optional ``(list_key, row_required_keys, row_name)``. The list is
    itself required to be non-empty, and each row must be a dict holding every
    one of ``row_required_keys``. All problems are reported together.
    """
    if not isinstance(context, dict):
        raise ValidationError(f"The {label} context must be a dict.")
    if not is_plain(context):
        raise ValidationError(
            f"The {label} context may hold only plain strings, numbers, booleans, lists "
            "and dicts. Flatten model instances, enums, Decimals and dates first."
        )
    keys = list(required) + ([rows[0]] if rows else [])
    errors = [f"Missing '{key}'." for key in keys if context.get(key) in (None, "", [])]
    if rows:
        list_key, row_keys, row_name = rows
        value = context.get(list_key)
        if isinstance(value, (list, tuple)):
            for n, row in enumerate(value, start=1):
                if not isinstance(row, dict):
                    errors.append(f"{row_name} {n} must be a dict.")
                    continue
                errors += [
                    f"{row_name} {n}: missing '{key}'."
                    for key in row_keys
                    if row.get(key) in (None, "")
                ]
        elif value not in (None, "", []):
            errors.append(f"'{list_key}' must be a list.")
    if errors:
        raise ValidationError(errors)
    return json.loads(json.dumps(context))
