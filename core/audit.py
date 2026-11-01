"""
Who is acting, for the audit trail (R7).

The acting user is held in a context variable rather than passed through
every save() call. It is set in three ways, innermost wins:

1. ``AuditActorMiddleware`` sets it to ``request.user`` for every web request,
   so admin and view saves are attributed without extra code.
2. ``with audit_actor(user):`` sets it for a block — use this in management
   commands, cron jobs and transition methods.
3. ``instance.save(actor=user)`` / ``instance.delete(actor=user)`` sets it for
   that one call.

If none applies the entry is written with ``actor=None``, meaning "the system"
(e.g. a cron-driven alert). It is never silently skipped.
"""

from contextlib import contextmanager
from contextvars import ContextVar

_current_actor = ContextVar("iea_audit_actor", default=None)


def get_current_actor():
    return _current_actor.get()


@contextmanager
def audit_actor(user):
    """Attribute every audited change inside this block to ``user``."""
    if user is not None and not getattr(user, "is_authenticated", False):
        user = None  # AnonymousUser is recorded as "system", never as a user.
    token = _current_actor.set(user)
    try:
        yield
    finally:
        _current_actor.reset(token)


class AuditActorMiddleware:
    """Must sit after ``AuthenticationMiddleware`` in ``MIDDLEWARE``."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        with audit_actor(getattr(request, "user", None)):
            return self.get_response(request)
