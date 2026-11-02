"""
Log formatting for settings.LOGGING: request context on every record that has one, and
secrets scrubbed from the finished text, traceback included.

Why scrub the finished text rather than trust callers: an exception message can carry a
connection string, and a traceback quotes source lines. Scrubbing after formatting is the
one place that sees everything a log line will contain.
"""

import logging
import re

REDACTED = "[redacted]"

# Credentials in a URL: scheme://user:PASSWORD@host
_URL_CREDENTIALS = re.compile(r"(\b[a-z][a-z0-9+.-]*://[^:/\s@]+:)[^@\s/]+@", re.IGNORECASE)
# key=value, key: value, 'key': 'value', "key": "value" for keys that hold secrets.
_SECRET_PAIRS = re.compile(
    r"""(?ix)
    \b(password\d?|passwd|pwd|secret(?:_key)?|token|api[_-]?key|authorization|
       sessionid|csrftoken|csrfmiddlewaretoken|aws_secret_access_key)
    (['"]?\s*[:=]\s*(?:b?['"]|\[['"])?)     # separator, optionally opening a quoted value
    ([^\s'"&,;\]}]+)
    """
)
# Values shorter than this are not searched for literally: too likely to match ordinary text.
MIN_LITERAL = 6


def _configured_secrets():
    """The secret values in settings, so they are scrubbed wherever they appear."""
    try:
        from django.conf import settings

        if not settings.configured:
            return []
        candidates = [
            getattr(settings, "SECRET_KEY", ""),
            settings.DATABASES.get("default", {}).get("PASSWORD", ""),
            getattr(settings, "EMAIL_HOST_PASSWORD", ""),
            getattr(settings, "AWS_SECRET_ACCESS_KEY", ""),
            getattr(settings, "GEMINI_API_KEY", ""),
            getattr(settings, "EXCHANGE_RATE_API_KEY", ""),
        ]
    except Exception:  # never let logging fail because settings could not be read
        return []
    return sorted({str(c) for c in candidates if c and len(str(c)) >= MIN_LITERAL}, key=len, reverse=True)


def redact(text):
    for value in _configured_secrets():
        text = text.replace(value, REDACTED)
    text = _URL_CREDENTIALS.sub(rf"\1{REDACTED}@", text)
    return _SECRET_PAIRS.sub(rf"\1\2{REDACTED}", text)


def request_context(record):
    """' [POST /path status=500 user=12 ip=…]' for records that carry a request
    (django.request, django.security), else ''. Never the query string, body, headers or
    cookies: those are where tokens and passwords travel."""
    request = getattr(record, "request", None)
    if request is None or not hasattr(request, "META"):
        return ""
    parts = [getattr(request, "method", "?") or "?", getattr(request, "path", "?") or "?"]
    status = getattr(record, "status_code", None)
    if status is not None:
        parts.append(f"status={status}")
    # Only a user already loaded: evaluating request.user here could hit the database
    # while handling the very error the database caused.
    user = getattr(request, "_cached_user", None)
    if user is not None and getattr(user, "is_authenticated", False):
        parts.append(f"user={user.pk}")
    meta = request.META
    forwarded = (meta.get("HTTP_X_FORWARDED_FOR") or "").split(",")[0].strip()
    parts.append(f"ip={forwarded or meta.get('REMOTE_ADDR', '?')}")
    return " [" + " ".join(parts) + "]"


class RedactingFormatter(logging.Formatter):
    """settings.LOGGING's formatter: adds ``%(request_context)s``, then scrubs secrets
    from the whole formatted record, traceback included."""

    def format(self, record):
        record.request_context = request_context(record)
        return redact(super().format(record))
