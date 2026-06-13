import logging
import time
import traceback

from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Browser worker: per-account, working-hours-only sequence sending + reply polling."

    def add_arguments(self, parser):
        parser.add_argument("--interval", type=int, default=120)

    def handle(self, *args, **options):
        from django.utils import timezone

        from linkedin.accounts.limits import in_session
        from linkedin.browser.registry import get_first_active_profile, get_or_create_session
        from linkedin.inbox.poller import poll_replies, process_pending_sends
        from linkedin.leads.importer import backfill_lead_profiles, process_pending_searches
        from linkedin.ml.lead_score import score_pending_leads
        from linkedin.models import LinkedInProfile
        from linkedin.notify.slack import post_text
        from linkedin.sequences.executor import (
            active_sending_accounts,
            due_states_by_account,
            enroll_active_campaigns,
            run_states,
        )
        try:
            from linkedin_cli.exceptions import AuthenticationError
        except Exception:  # pragma: no cover
            class AuthenticationError(Exception):
                pass

        interval = options["interval"]
        HEAVY_EVERY = 600        # heavy enrichment/scoring cadence (seconds)
        REAUTH_COOLDOWN = 1800   # don't re-attempt a failing account's login more than this often
        last_heavy = 0.0
        consecutive_errors = 0
        acct_cooldown = {}       # profile pk -> monotonic time of last failed acquire

        self.stdout.write(self.style.SUCCESS(
            "worker started — working-hours mode (each account opens a session at a random "
            "time in the first 30 min of its window and closes near the end; nothing runs overnight)"))

        def acquire_session(acct):
            if not acct.cookie_data and not acct.totp_secret:
                raise RuntimeError("no saved session and no TOTP secret — needs a one-time manual login")
            session = get_or_create_session(acct)
            session.ensure_browser()
            session.assert_not_restricted()  # HTTP-200 restriction page → AuthenticationError → auto_pause
            return session

        def auto_pause(prof, reason):
            """Stop touching a flagged account and alert — never hammer a restricted
            session (the 401-loop incident). Cleared by a successful connection test."""
            if prof.auto_paused_at is not None:
                return  # already paused — don't re-alert
            prof.auto_paused_at = timezone.now()
            prof.last_verify_ok = False
            prof.last_verify_error = reason[:300]
            prof.save(update_fields=["auto_paused_at", "last_verify_ok", "last_verify_error"])
            logger.error("AUTO-PAUSED %s: %s", prof.linkedin_username, reason)
            try:
                post_text(":rotating_light: OpenOutreach paused *%s* — %s. Resolve it in LinkedIn, "
                          "then hit 'Test connection' in the Accounts tab to resume." % (prof.linkedin_username, reason))
            except Exception:
                pass

        while True:
            from django.db import connection
            connection.close()
            try:
                now_dt = timezone.now()

                # ── Connection tests (user-initiated; runs anytime) ──────────
                import threading
                from linkedin.browser.launch import verify_account
                for prof in LinkedInProfile.objects.filter(verify_requested=True):
                    res = {}

                    def _run(_p=prof, _r=res):
                        _r["v"] = verify_account(_p)

                    t = threading.Thread(target=_run, daemon=True)
                    t.start()
                    t.join(timeout=150)
                    ok, err = res.get("v", (False, "verification timed out"))
                    prof.refresh_from_db(fields=["cookie_data"])
                    prof.verify_requested = False
                    prof.last_verified_at = timezone.now()
                    prof.last_verify_ok = ok
                    prof.last_verify_error = (err or "")[:300]
                    fields = ["verify_requested", "last_verified_at", "last_verify_ok", "last_verify_error"]
                    if ok and prof.auto_paused_at is not None:
                        prof.auto_paused_at = None  # a successful test clears an auto-pause
                        fields.append("auto_paused_at")
                    prof.save(update_fields=fields)
                    self.stdout.write(f"verified {prof.linkedin_username}: ok={ok} {err}")

                enrolled = enroll_active_campaigns()  # DB-only; safe anytime

                fallback = get_first_active_profile()
                groups = {p.pk: st for p, st in due_states_by_account(fallback)} if fallback else {}
                accounts = active_sending_accounts(fallback) if fallback else []

                executed = 0
                ran, skipped, asleep = [], [], []
                sessions = {}
                now_m = time.monotonic()
                for acct in accounts:
                    if acct.auto_paused_at is not None:
                        skipped.append(acct.linkedin_username + "(paused)")
                        continue
                    if not in_session(acct, now_dt):
                        # Outside working hours — close any open browser, do nothing.
                        existing = get_or_create_session(acct)
                        if existing.page is not None:
                            existing.close_browser()
                        asleep.append(acct.linkedin_username)
                        continue
                    cooled = acct_cooldown.get(acct.pk)
                    if cooled is not None and (now_m - cooled) < REAUTH_COOLDOWN:
                        skipped.append(acct.linkedin_username)
                        continue
                    try:
                        session = acquire_session(acct)
                    except AuthenticationError as exc:
                        auto_pause(acct, "LinkedIn rejected the session (%s)" % exc)
                        skipped.append(acct.linkedin_username + "(paused)")
                        continue
                    except Exception as exc:
                        acct_cooldown[acct.pk] = now_m
                        skipped.append(acct.linkedin_username)
                        logger.warning("Account %s unavailable — skipping: %s", acct.linkedin_username, exc)
                        continue
                    acct_cooldown.pop(acct.pk, None)
                    sessions[acct.pk] = session
                    try:
                        executed += run_states(session, groups.get(acct.pk, []))
                        ran.append(acct.linkedin_username)
                    except AuthenticationError as exc:
                        auto_pause(acct, "LinkedIn 401 during sending (%s)" % exc)

                # ── Inbox + manual sends on the default account (in-session only) ──
                manual = stopped = 0
                default_session = sessions.get(fallback.pk) if fallback else None
                if default_session is not None:
                    try:
                        manual = process_pending_sends(default_session)
                        stopped = poll_replies(default_session, limit=12)
                    except AuthenticationError as exc:
                        auto_pause(fallback, "LinkedIn 401 during reply-poll (%s)" % exc)
                        default_session = None

                # ── Heavy work, only when the default account is in-session ──────
                extra = ""
                now = time.monotonic()
                if now - last_heavy >= HEAVY_EVERY and default_session is not None:
                    try:
                        searched = process_pending_searches(default_session, cap=30)
                        backfilled = backfill_lead_profiles(default_session, limit=8)
                        scored = score_pending_leads(limit=15)
                        last_heavy = now
                        extra = f" searches={searched} backfilled={backfilled} scored={scored}"
                    except AuthenticationError as exc:
                        auto_pause(fallback, "LinkedIn 401 during enrichment (%s)" % exc)

                consecutive_errors = 0
                note = ""
                if ran:
                    note += f" sent_by={','.join(ran)}"
                if asleep:
                    note += f" off-hours={','.join(asleep)}"
                if skipped:
                    note += f" skipped={','.join(skipped)}"
                self.stdout.write(
                    f"cycle: executed={executed} enrolled={enrolled['enrolled']} "
                    f"manual_sent={manual} replies_stopped={stopped}{extra}{note}")
            except Exception as exc:  # keep the worker alive across transient errors
                consecutive_errors += 1
                self.stderr.write(f"cycle error ({consecutive_errors}): {exc!r}")
                logger.error("Worker cycle error:\n%s", traceback.format_exc())
            sleep_for = interval * (min(consecutive_errors, 5) or 1) if consecutive_errors else interval
            time.sleep(sleep_for)
