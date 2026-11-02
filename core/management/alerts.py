"""
Shared plumbing for the cron-driven alert commands (CLAUDE.md §10 "Alerts").

Lives in ``core`` so every app's alert command can use it without importing
downward (§1). It imports nothing from any other local app.

Each command decides WHO gets WHAT about WHICH tender; ``send_once`` makes
sending it safe:

* **Idempotent.** Every alert sent is recorded as an ``AuditEntry`` (R7) with
  ``model="alert.<KIND>"`` and ``object_id=<tender pk>``. Before sending, the
  ledger is checked: the same alert for the same tender is never sent twice on
  the same day (Africa/Dar_es_Salaam). Phase 1 has no notification model (§4
  "Notifications"), and the audit trail is exactly the right home anyway: it
  is immutable, so it is also the proof that a warning went out — "the MD was
  told at T-1" can never be quietly edited away.

* **Safe for overlapping runs.** The check, the send and the ledger write
  happen in one transaction holding a PostgreSQL advisory lock on
  (kind, tender). A second run that arrives meanwhile does not wait: it skips
  that tender (``BUSY``), and on the next run finds the ledger entry.

* **At-least-once, never silent.** The email is sent inside the transaction.
  If sending raises, the transaction rolls back, no ledger entry is written,
  and the next run retries. The one window for a duplicate is the send
  succeeding and the commit then failing — preferred over the opposite risk,
  an alert recorded as sent that never left.
"""

import hashlib
import logging
from datetime import datetime, time

from django.conf import settings
from django.core.mail import EmailMessage
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils import timezone

from core.models import AuditAction, AuditEntry

logger = logging.getLogger(__name__)

SENT = "sent"
ALREADY_SENT = "already_sent"
BUSY = "busy"
NO_RECIPIENTS = "no_recipients"


def ledger_model(kind):
    """The ``AuditEntry.model`` label an alert of ``kind`` is recorded under."""
    return f"alert.{kind}"


def local_today():
    return timezone.localdate()


def start_of_local_day(day):
    return timezone.make_aware(datetime.combine(day, time.min))


def days_until(moment, today=None):
    """Whole calendar days from ``today`` (local) to the local date of ``moment``.
    Deadline tomorrow = 1, whatever the hour; deadline today = 0; past = negative."""
    today = today or local_today()
    return (timezone.localdate(moment) - today).days


def last_sent(kind, tender):
    """Timestamp of the most recent ``kind`` alert for ``tender``, or None."""
    return (
        AuditEntry.objects.filter(model=ledger_model(kind), object_id=str(tender.pk))
        .order_by("-timestamp", "-id")
        .values_list("timestamp", flat=True)
        .first()
    )


def _lock_key(kind, tender_pk):
    """A signed 64-bit key for pg_try_advisory_xact_lock. A hash collision can
    only make one alert skip a run as BUSY, never send twice."""
    digest = hashlib.sha256(f"iea-alert:{kind}:{tender_pk}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def _try_lock(kind, tender_pk):
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_xact_lock(%s)", [_lock_key(kind, tender_pk)])
        return cursor.fetchone()[0]


def clean_recipients(users):
    """Unique, non-empty addresses of active users, in a stable order."""
    seen, out = set(), []
    for user in users:
        if user is None or not user.is_active or not user.email:
            continue
        address = user.email.lower()
        if address not in seen:
            seen.add(address)
            out.append(user.email)
    return out


def send_once(*, kind, tender, recipients, subject, body, not_since=None):
    """
    Send one alert email about ``tender`` unless an alert of ``kind`` for it is
    already recorded at or after ``not_since`` (default: the start of today,
    local time). Returns SENT, ALREADY_SENT, BUSY or NO_RECIPIENTS.

    ``recipients`` is a list of email addresses (see ``clean_recipients``).
    """
    if not recipients:
        logger.warning("alert %s for %s: nobody to send to", kind, tender.reference)
        return NO_RECIPIENTS
    today = local_today()
    if not_since is None:
        not_since = start_of_local_day(today)

    with transaction.atomic():
        if not _try_lock(kind, tender.pk):
            return BUSY
        already = AuditEntry.objects.filter(
            model=ledger_model(kind), object_id=str(tender.pk), timestamp__gte=not_since
        ).exists()
        if already:
            return ALREADY_SENT

        EmailMessage(
            subject=subject, body=body, from_email=settings.DEFAULT_FROM_EMAIL, to=recipients
        ).send(fail_silently=False)

        AuditEntry.objects.create(
            actor=None,  # the system (cron)
            action=AuditAction.CREATE,
            model=ledger_model(kind),
            object_id=str(tender.pk),
            object_repr=str(tender)[:255],
            changes={
                "alert": [None, kind],
                "for_date": [None, today.isoformat()],
                "recipients": [None, recipients],
                "subject": [None, subject],
            },
        )
    return SENT


def tender_header(tender):
    """First lines of every alert body: reference, title, client, deadline."""
    lines = [f"{tender.reference} — {tender.title or '(untitled)'}"]
    if tender.client_id:
        lines.append(f"Client: {tender.client}")
    if tender.official_reference:
        lines.append(f"Tender number: {tender.official_reference}")
    if tender.submission_deadline:
        deadline = timezone.localtime(tender.submission_deadline)
        lines.append(f"Submission deadline: {deadline:%A %d %B %Y, %H:%M}")
    return "\n".join(lines)


class AlertCommand(BaseCommand):
    """
    Base for the alert commands. Subclasses implement ``handle_alerts()`` and
    call ``self.deliver(...)`` once per alert. One failing tender (bad SMTP
    response, a data surprise) is logged and does not stop the others; the run
    still exits non-zero at the end so cron mail / monitoring notices.
    """

    def handle(self, *args, **options):
        self.tally = {SENT: 0, ALREADY_SENT: 0, BUSY: 0, NO_RECIPIENTS: 0, "failed": 0}
        self.handle_alerts(**options)
        summary = ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in self.tally.items())
        self.stdout.write(f"{self.name_for_log()}: {summary}")
        if self.tally["failed"]:
            raise CommandError(f"{self.tally['failed']} alert(s) failed; see the log. They will be retried next run.")

    def handle_alerts(self, **options):  # pragma: no cover - abstract
        raise NotImplementedError

    def name_for_log(self):
        return type(self).__module__.rsplit(".", 1)[-1]

    def deliver(self, *, kind, tender, recipients, subject, body, not_since=None):
        try:
            result = send_once(kind=kind, tender=tender, recipients=recipients,
                               subject=subject, body=body, not_since=not_since)
        except Exception:
            logger.exception("alert %s for %s failed", kind, tender.reference)
            self.stderr.write(f"{tender.reference}: {kind} FAILED (logged); will retry next run")
            self.tally["failed"] += 1
            return "failed"
        self.tally[result] += 1
        if result == SENT:
            self.stdout.write(f"{tender.reference}: {kind} sent to {', '.join(recipients)}")
        elif result == NO_RECIPIENTS:
            self.stderr.write(f"{tender.reference}: {kind} has no active recipient; not sent")
        return result
