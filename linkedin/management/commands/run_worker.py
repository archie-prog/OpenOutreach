import logging
import time
import traceback

from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Browser worker: per-account sequence sending, reply polling, lead searches."

    def add_arguments(self, parser):
        parser.add_argument("--interval", type=int, default=120)

    def handle(self, *args, **options):
        from linkedin.browser.registry import get_first_active_profile, get_or_create_session
        from linkedin.inbox.poller import poll_replies, process_pending_sends
        from linkedin.leads.importer import backfill_lead_profiles, process_pending_searches
        from linkedin.ml.lead_score import score_pending_leads
        from linkedin.sequences.executor import (
            active_sending_accounts,
            due_states_by_account,
            enroll_active_campaigns,
            run_states,
        )

        fallback = get_first_active_profile()
        if not fallback:
            self.stderr.write("No active LinkedIn profile.")
            return
        self.stdout.write(self.style.SUCCESS(f"worker started (default sender {fallback.linkedin_username})"))

        interval = options["interval"]
        # Heavy, less-urgent work (reply backfill scrapes, lead enrichment,
        # scoring) runs on its own slower clock so it can't starve the sender.
        HEAVY_EVERY = 600  # seconds
        # Never re-attempt a login for the same account more than once per this
        # window — a real LinkedIn account must not be hammered with logins
        # (lockout/flag risk), especially one with no stored TOTP that needs a
        # human to clear a checkpoint. A failed account just idles until then.
        REAUTH_COOLDOWN = 1800  # seconds
        last_heavy = 0.0
        consecutive_errors = 0
        acct_cooldown = {}  # profile pk -> monotonic time of last failed acquire

        def acquire_session(acct):
            """Live session for *acct*, or raise if it can't run unattended.
            An account with neither saved cookies nor a TOTP secret would block
            on a human 2FA challenge, so we refuse it up front rather than hang."""
            if not acct.cookie_data and not acct.totp_secret:
                raise RuntimeError("no saved session and no TOTP secret — needs a one-time manual login")
            session = get_or_create_session(acct)
            session.ensure_browser()
            return session

        while True:
            from django.db import connection

            # Drop the cached connection so each cycle sees rows committed by the
            # web process (e.g. a freshly queued search or campaign edit).
            connection.close()
            try:
                enrolled = enroll_active_campaigns()

                # ── Per-account SENDING ─────────────────────────────────────
                # Each ACTIVE campaign sends from its sending_account (or the
                # default). Group due states by account and run each under its
                # own browser session; an account that can't authenticate is
                # skipped (with a cooldown) so it never blocks the others.
                groups = {p.pk: states for p, states in due_states_by_account(fallback)}
                accounts = active_sending_accounts(fallback)
                executed = 0
                ran, skipped = [], []
                sessions = {}
                now_m = time.monotonic()
                for acct in accounts:
                    cooled = acct_cooldown.get(acct.pk)
                    if cooled is not None and (now_m - cooled) < REAUTH_COOLDOWN:
                        skipped.append(acct.linkedin_username)
                        continue
                    try:
                        session = acquire_session(acct)
                    except Exception as exc:
                        acct_cooldown[acct.pk] = now_m
                        skipped.append(acct.linkedin_username)
                        logger.warning("Account %s unavailable — skipping: %s",
                                       acct.linkedin_username, exc)
                        continue
                    acct_cooldown.pop(acct.pk, None)
                    sessions[acct.pk] = session
                    ran.append(acct.linkedin_username)
                    executed += run_states(session, groups.get(acct.pk, []))

                # ── Inbox + manual sends (single-account scope for now) ──────
                # process_pending_sends / poll_replies query globally and act via
                # one session, so they run once on the default account's session.
                # (Per-account reply polling is a follow-up; moot until a second
                # account is actually authenticated and sending.)
                default_session = sessions.get(fallback.pk)
                if default_session is None:
                    cooled = acct_cooldown.get(fallback.pk)
                    if cooled is None or (now_m - cooled) >= REAUTH_COOLDOWN:
                        try:
                            default_session = acquire_session(fallback)
                            acct_cooldown.pop(fallback.pk, None)
                        except Exception as exc:
                            acct_cooldown[fallback.pk] = now_m
                            logger.warning("Default account %s unavailable: %s",
                                           fallback.linkedin_username, exc)
                manual = stopped = 0
                if default_session is not None:
                    manual = process_pending_sends(default_session)
                    stopped = poll_replies(default_session, limit=12)

                # ── Heavy work (account-agnostic) on the default session ─────
                extra = ""
                now = time.monotonic()
                if now - last_heavy >= HEAVY_EVERY and default_session is not None:
                    searched = process_pending_searches(default_session, cap=30)
                    backfilled = backfill_lead_profiles(default_session, limit=8)
                    scored = score_pending_leads(limit=15)
                    last_heavy = now
                    extra = f" searches={searched} backfilled={backfilled} scored={scored}"

                consecutive_errors = 0
                acct_note = ""
                if ran:
                    acct_note += f" sent_by={','.join(ran)}"
                if skipped:
                    acct_note += f" skipped={','.join(skipped)}"
                self.stdout.write(
                    f"cycle: executed={executed} enrolled={enrolled['enrolled']} "
                    f"manual_sent={manual} replies_stopped={stopped}{extra}{acct_note}",
                    ending="\n",
                )
            except Exception as exc:  # keep the worker alive across transient errors
                consecutive_errors += 1
                self.stderr.write(f"cycle error ({consecutive_errors}): {exc!r}")
                logger.error("Worker cycle error:\n%s", traceback.format_exc())
            # Back off on a run of failures so a hard-down dependency doesn't spin
            # the loop (and the logs) at full speed.
            sleep_for = interval * (min(consecutive_errors, 5) or 1) if consecutive_errors else interval
            time.sleep(sleep_for)
