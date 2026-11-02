"""
Pick up tenders in EXTRACTING, read them, and move them to PENDING_VERIFICATION
(CLAUDE.md §5 step 6, §6 "Extraction runs asynchronously").

Invoked by cron every minute, per §4 (no Celery, no Redis, no broker):

    * * * * *  cd /srv/iea-system && .venv/bin/python manage.py extract_tenders >> /var/log/iea/extract.log 2>&1

Safe for overlapping runs. Nothing here relies on the previous run having finished:

* each stage is CLAIMED on the tender before it starts (``prepare_extraction`` for conversion,
  ``extract_fields`` for the model call, which costs money), under a row lock, so two runs can never
  convert or call the model for the same tender;
* a claim left behind by a crashed run goes stale after 15 minutes and is taken over;
* ``--limit`` bounds one run's work (conversion is ~0.4 s per page), so a backlog drains over several runs.

Never traps a tender: before anything else the command finishes any tender still in EXTRACTING
``--stuck-after`` minutes after its upload, with empty fields, whatever the cause (§5 step 6).

If Gemini is briefly unavailable (503 overloaded, 429, a timeout), the tender is left in EXTRACTING and the
next run asks again.

Development (no cron, e.g. on Windows): ``python manage.py extract_tenders --loop`` runs this every 60 seconds
in the foreground until Ctrl+C. It refuses to start when DEBUG is off: production uses real cron
(docs/DEPLOY.md §7), never this loop.

If GEMINI_API_KEY or GEMINI_MODEL is not set the command exits non-zero with an instruction: that is
an operations problem, and the tenders waiting are picked up as soon as it is fixed — or, if it is not
fixed in time, finished with empty fields by the sweep above.
"""

import logging
import time
from datetime import timedelta

from django.conf import settings
from django.db import close_old_connections, connection

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django.utils import timezone

from tenders.models import ExtractionOutcome, Tender, TenderStatus
from tenders.pipeline import service
from tenders.pipeline.extractor import ExtractionConfigError, GeminiExtractor

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Convert and extract tenders in EXTRACTING and move them to PENDING_VERIFICATION. Run by cron every minute."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=3,
                            help="Most tenders to work on in one run (default 3).")
        parser.add_argument("--stuck-after", type=int, default=int(service.STUCK_AFTER.total_seconds() // 60),
                            help="Minutes after upload at which a tender still in EXTRACTING is finished with "
                                 "empty fields (default 30).")
        parser.add_argument("--loop", action="store_true",
                            help="DEVELOPMENT ONLY: keep running, once every --interval seconds, until Ctrl+C. "
                                 "Stands in for cron where there is none (Windows). Refused when DEBUG is off.")
        parser.add_argument("--interval", type=int, default=60,
                            help="Seconds between runs with --loop (default 60, the cron cadence).")

    def handle(self, *args, limit, stuck_after, loop, interval, **options):
        if not loop:
            self.run_once(limit, stuck_after, quiet=False)
            return
        if not settings.DEBUG:
            raise CommandError("--loop is for development only and DEBUG is off. In production this command is "
                               "run by cron every minute (docs/DEPLOY.md section 7).")
        interval = max(interval, 1)
        self.stdout.write(f"Extraction poller: running every {interval}s. Leave this terminal open; Ctrl+C to stop.")
        try:
            while True:
                started = time.monotonic()
                try:
                    self.run_once(limit, stuck_after, quiet=options["verbosity"] < 2)
                except CommandError as exc:  # e.g. GEMINI_MODEL missing: say so every run, keep the sweep going
                    self.stderr.write(f"{self._now()} {exc} Edit .env, then restart this poller.")
                except Exception:  # never let one bad run end the poller
                    logger.exception("extract_tenders --loop: run failed")
                    self.stderr.write(f"{self._now()} run failed (traceback above); trying again next run")
                finally:
                    if not connection.in_atomic_block:  # drop a dead connection (e.g. Postgres restarted) between runs
                        close_old_connections()
                time.sleep(max(0.0, interval - (time.monotonic() - started)))
        except KeyboardInterrupt:
            self.stdout.write("Extraction poller stopped.")

    @staticmethod
    def _now():
        return timezone.localtime().strftime("%H:%M:%S")

    def run_once(self, limit, stuck_after, *, quiet):
        """One cron-equivalent run. ``quiet`` drops the "0 tenders" line so a loop only speaks when it works."""
        # 1. The safety net first: nothing is ever trapped, even if everything below fails.
        for tender in service.fail_stuck(stuck_after=timedelta(minutes=stuck_after)):
            self.stdout.write(f"{tender.reference}: still extracting after {stuck_after} min; finished with empty fields")

        # 2. Configuration. An operations problem, reported loudly, not a per-tender failure.
        try:
            extractor = GeminiExtractor.from_environment()
        except ExtractionConfigError as exc:
            raise CommandError(f"{exc} Tenders stay in EXTRACTING until this is fixed.") from exc

        # 3. Work: tenders nobody is working on. Overlapping runs are safe regardless (claims).
        stale_cutoff = timezone.now() - service.STALE_AFTER
        candidates = (
            Tender.objects.filter(status=TenderStatus.EXTRACTING)
            .filter(
                Q(extraction__isnull=True)
                | Q(extraction__outcome=ExtractionOutcome.PREPARED)
                | Q(extraction__outcome__in=[ExtractionOutcome.PROCESSING, ExtractionOutcome.READING],
                    extraction__updated_at__lt=stale_cutoff)
            )
            .order_by("created_at")[: max(limit, 0)]
        )
        done = 0
        for tender in candidates:
            try:
                extraction = service.process_tender(tender, extractor=extractor)
            except ExtractionConfigError as exc:  # raised mid-run: stop, the claim was released
                raise CommandError(str(exc)) from exc
            except Exception:  # one bad tender must not stop the others; the sweep finishes it if it stays stuck
                logger.exception("extract_tenders: %s failed", tender.reference)
                self.stderr.write(f"{tender.reference}: error (logged); will be retried or finished by the sweep")
                continue
            done += 1
            tender.refresh_from_db()
            if tender.status == TenderStatus.EXTRACTING and extraction.outcome == ExtractionOutcome.PREPARED:
                self.stdout.write(f"{tender.reference}: Gemini unavailable just now; left for the next run")
            else:
                self.stdout.write(f"{tender.reference}: {extraction.outcome} -> {tender.status}")
        if done or not quiet:
            self.stdout.write(f"{self._now()} extract_tenders: {done} tender(s) worked on")
