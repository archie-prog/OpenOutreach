import logging
import time
import traceback

from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Browser worker: reply polling + queued lead searches (owns the LinkedIn session)."

    def add_arguments(self, parser):
        parser.add_argument("--interval", type=int, default=120)

    def handle(self, *args, **options):
        from linkedin.browser.registry import get_first_active_profile, get_or_create_session
        from linkedin.inbox.poller import poll_replies, process_pending_sends
        from linkedin.leads.importer import backfill_lead_profiles, process_pending_searches
        from linkedin.ml.lead_score import score_pending_leads
        from linkedin.sequences.executor import enroll_active_campaigns, run_due_states

        try:
            from linkedin_cli.exceptions import AuthenticationError
        except Exception:  # pragma: no cover - cli always present in prod
            class AuthenticationError(Exception):
                pass

        profile = get_first_active_profile()
        if not profile:
            self.stderr.write("No active LinkedIn profile.")
            return
        session = get_or_create_session(profile)
        self.stdout.write(self.style.SUCCESS(f"worker started for {profile.linkedin_username}"))

        interval = options["interval"]
        # Heavy, less-urgent work (reply backfill scrapes, lead enrichment,
        # scoring) runs on its own slower clock so it can't starve the sender.
        HEAVY_EVERY = 600  # seconds
        last_heavy = 0.0
        consecutive_errors = 0
        while True:
            from django.db import connection

            # Drop the cached connection so each cycle sees rows committed by the
            # web process (e.g. a freshly queued search).
            connection.close()
            try:
                # Sender FIRST and every cycle, so paced actions fire on schedule
                # instead of waiting behind the slow reply/enrichment work.
                enrolled = enroll_active_campaigns()
                executed = run_due_states(session)
                manual = process_pending_sends(session)
                # Bounded reply scan every cycle (each scan is a live fetch).
                stopped = poll_replies(session, limit=12)

                extra = ""
                now = time.monotonic()
                if now - last_heavy >= HEAVY_EVERY:
                    searched = process_pending_searches(session, cap=30)
                    backfilled = backfill_lead_profiles(session, limit=8)
                    scored = score_pending_leads(limit=15)
                    last_heavy = now
                    extra = f" searches={searched} backfilled={backfilled} scored={scored}"

                consecutive_errors = 0
                self.stdout.write(
                    f"cycle: executed={executed} enrolled={enrolled['enrolled']} "
                    f"manual_sent={manual} replies_stopped={stopped}{extra}",
                    ending="\n",
                )
            except AuthenticationError as exc:
                # The LinkedIn session is no longer valid (cookie revoked, password
                # changed, etc). Try to recover by logging in fresh (TOTP-aware)
                # instead of silently failing every cycle forever.
                consecutive_errors += 1
                self.stderr.write(f"auth error — re-authenticating: {exc!r}")
                logger.warning("Worker auth error; attempting reauthenticate()", exc_info=True)
                try:
                    session.reauthenticate()
                    self.stdout.write(self.style.SUCCESS("re-authenticated OK"))
                    consecutive_errors = 0
                except Exception:
                    logger.error("Re-authentication failed:\n%s", traceback.format_exc())
            except Exception as exc:  # keep the worker alive across transient errors
                consecutive_errors += 1
                # Log the FULL traceback (the old 200-char repr hid the cause) so a
                # recurring failure is diagnosable from the worker logs.
                self.stderr.write(f"cycle error ({consecutive_errors}): {exc!r}")
                logger.error("Worker cycle error:\n%s", traceback.format_exc())
            # Back off on a run of failures so a hard-down dependency doesn't spin
            # the loop (and the logs) at full speed.
            sleep_for = interval * (min(consecutive_errors, 5) or 1) if consecutive_errors else interval
            time.sleep(sleep_for)
