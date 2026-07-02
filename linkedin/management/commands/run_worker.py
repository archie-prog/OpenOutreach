import logging
import os
import random
import signal
import time
import traceback
from datetime import timedelta

from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


class CycleTimeout(Exception):
    """Raised by the SIGALRM watchdog when one cycle blows its deadline — a hung
    browser op that would otherwise freeze every account for the rest of the day."""


class Command(BaseCommand):
    help = "Browser worker: per-account, working-hours-only sequence sending + reply polling."

    def add_arguments(self, parser):
        parser.add_argument("--interval", type=int, default=120)

    def handle(self, *args, **options):
        from django.utils import timezone

        from linkedin.accounts.limits import in_session
        from linkedin.browser.registry import get_first_active_profile, get_or_create_session
        from pathlib import Path

        from linkedin.inbox.poller import process_pending_sends, sync_inbox
        from linkedin.leads.importer import backfill_lead_profiles, process_pending_searches
        from linkedin.ml.lead_score import score_pending_leads
        from linkedin.models import LeadList, LinkedInProfile
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
        # Manual inbox sync: the Unibox drops this flag file (shared data volume);
        # the worker runs sync_inbox when it's present, then clears it — so LinkedIn
        # is read for messages only when you ask, never on a constant timer.
        INBOX_FLAG = Path("/app/data/.inbox_sync")
        # The Unibox drops this when you hit Send on a manual reply; the worker drains
        # it IMMEDIATELY (any hour), sending from each message's owning account.
        MANUAL_FLAG = Path("/app/data/.manual_send")
        HEAVY_EVERY = 600        # heavy enrichment/scoring cadence (seconds)
        INBOX_EVERY = 300        # reply-poll / manual-send cadence (seconds)
        REAUTH_COOLDOWN = 1800   # don't re-attempt a failing account's login more than this often
        CYCLE_BUDGET = 900       # per-cycle hard deadline; a hang past this exits the worker for a
                                 # clean systemd relaunch (single-threaded loop → 1 hang = whole day lost)
        last_heavy = 0.0
        last_inbox = 0.0
        # Full inbox sync runs ~3x/day at jittered human-hour slots (morning, lunch,
        # late afternoon) so replies are caught without constant polling. The exact
        # times vary every day (never a fixed pattern). Runs regardless of the send
        # window — reading the inbox isn't sending.
        SYNC_SLOTS = ((9, 11), (12, 14), (16, 17))
        sync_day = None
        sync_targets = []
        slots_done = set()
        consecutive_errors = 0
        acct_cooldown = {}       # profile pk -> monotonic time of last failed acquire

        self.stdout.write(self.style.SUCCESS(
            "worker started — working-hours mode (each account opens a session at a random "
            "time in the first 30 min of its window and closes near the end; nothing runs overnight)"))

        def _on_cycle_deadline(signum, frame):
            raise CycleTimeout()
        signal.signal(signal.SIGALRM, _on_cycle_deadline)

        def acquire_session(acct):
            if not acct.cookie_data and not acct.totp_secret and not acct.password_login_ok:
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
            signal.alarm(CYCLE_BUDGET)  # arm the per-cycle watchdog
            try:
                now_dt = timezone.now()
                if now_dt.date() != sync_day:
                    sync_day = now_dt.date()
                    slots_done = set()
                    sync_targets = [
                        now_dt.replace(hour=h0, minute=0, second=0, microsecond=0)
                        + timedelta(minutes=random.randint(0, (h1 - h0) * 60 - 1))
                        for (h0, h1) in SYNC_SLOTS
                    ]
                full_sync_due = any(now_dt >= t and i not in slots_done
                                    for i, t in enumerate(sync_targets))

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
                manual = 0
                extra = ""
                heavy_due = (time.monotonic() - last_heavy) >= HEAVY_EVERY
                inbox_due = (time.monotonic() - last_inbox) >= INBOX_EVERY
                now_m = time.monotonic()

                # ── Pending searches (user-initiated manual research) ─────────
                # A queued people-search is the user's own action and read-only
                # (scrape + profile views, never an invite), so it runs ANY time/
                # day — NOT gated by the send window. Uses the default account's
                # one serialized browser, opened and closed here before the send
                # loop touches any browser (no concurrent sessions).
                if (fallback is not None
                        and LeadList.objects.filter(pending_search=True, archived_at__isnull=True).exists()):
                    s_cool = acct_cooldown.get(fallback.pk)
                    if (fallback.auto_paused_at is None
                            and (fallback.cookie_data or fallback.totp_secret)
                            and not (s_cool is not None and (now_m - s_cool) < REAUTH_COOLDOWN)):
                        s_session = get_or_create_session(fallback)
                        try:
                            acquire_session(fallback)
                            searched = process_pending_searches(s_session, cap=30)
                            if searched:
                                extra += f" searches={searched}"
                        except AuthenticationError as exc:
                            auto_pause(fallback, "LinkedIn rejected the session during search (%s)" % exc)
                        except Exception as exc:
                            acct_cooldown[fallback.pk] = now_m
                            logger.warning("On-demand search skipped: %s", exc)
                        finally:
                            s_session.close_browser()

                # ── Manual Unibox sends (user-initiated; INSTANT, any hour) ────
                # When you hit Send the Unibox drops .manual_send; drain it NOW from
                # each message's OWNING account on the serialized browser, bypassing
                # the send window (a human replying at any hour is genuine). Auto/kit
                # sends stay window-gated + paced; only these human sends skip it.
                if MANUAL_FLAG.exists():
                    from linkedin.models import Message
                    owner_ids = set()
                    for m in (Message.objects.filter(pending_send=True)
                              .select_related("sender_account", "thread__account")):
                        owner = m.sender_account or m.thread.account
                        if owner is not None:
                            owner_ids.add(owner.pk)
                    for acct_m in LinkedInProfile.objects.filter(pk__in=owner_ids):
                        if acct_m.auto_paused_at is not None:
                            continue
                        m_cool = acct_cooldown.get(acct_m.pk)
                        if m_cool is not None and (now_m - m_cool) < REAUTH_COOLDOWN:
                            continue
                        if not (acct_m.cookie_data or acct_m.totp_secret or acct_m.password_login_ok):
                            continue
                        m_session = get_or_create_session(acct_m)
                        try:
                            acquire_session(acct_m)
                            manual += process_pending_sends(m_session, account=acct_m)
                        except AuthenticationError as exc:
                            auto_pause(acct_m, "LinkedIn rejected the session during manual send (%s)" % exc)
                        except Exception as exc:
                            acct_cooldown[acct_m.pk] = now_m
                            logger.warning("Manual send for %s skipped: %s", acct_m.linkedin_username, exc)
                        finally:
                            m_session.close_browser()  # serialize: close before the loop opens any
                    # Clear the wake-signal only once nothing DELIVERABLE is still
                    # queued. If an account was skipped this pass for a transient
                    # reason (browser acquire failed, in-memory cooldown, or the
                    # worker restarted mid-drain), leave the flag so the next cycle
                    # retries — unlinking unconditionally stranded a non-default
                    # account's manual replies FOREVER (this block is their only
                    # drain path). A message whose owner is paused/credless can't be
                    # sent now, so it doesn't keep the signal armed (it goes once the
                    # account recovers and the next manual send re-arms the flag).
                    # MAX_MANUAL_SEND_ATTEMPTS still bounds a permanently-failing send.
                    deliverable = any(
                        (m.sender_account or m.thread.account) is not None
                        and (m.sender_account or m.thread.account).auto_paused_at is None
                        and ((m.sender_account or m.thread.account).cookie_data
                             or (m.sender_account or m.thread.account).totp_secret
                             or (m.sender_account or m.thread.account).password_login_ok)
                        for m in Message.objects.filter(pending_send=True)
                        .select_related("sender_account", "thread__account")
                    )
                    if not deliverable:
                        MANUAL_FLAG.unlink(missing_ok=True)

                # Serialize browsers: Playwright's sync API allows only ONE live
                # instance per OS thread, and concurrent sessions from one IP are a
                # detection signal — so open at most one account's browser at a time
                # and CLOSE it before the next opens. The default account also runs
                # inbox/manual + heavy enrichment inside its own open window.
                sync_requested = INBOX_FLAG.exists()
                # A full inbox sync runs when the Unibox button asked for one, OR at a
                # scheduled ~3x/day slot. Either way it's a READ (allowed off-hours).
                do_sync = sync_requested or full_sync_due
                for acct in accounts:
                    is_default = bool(fallback and acct.pk == fallback.pk)
                    if acct.auto_paused_at is not None:
                        skipped.append(acct.linkedin_username + "(paused)")
                        continue
                    in_sess = in_session(acct, now_dt)
                    if not in_sess and not do_sync:
                        # Off-hours and no inbox read requested — skip. (An on-demand
                        # Unibox sync is a READ, so it's allowed off-hours; sending is
                        # still gated by in_sess below.)
                        get_or_create_session(acct).close_browser()
                        asleep.append(acct.linkedin_username)
                        continue
                    cooled = acct_cooldown.get(acct.pk)
                    if cooled is not None and (now_m - cooled) < REAUTH_COOLDOWN:
                        skipped.append(acct.linkedin_username)
                        continue
                    # Open only when there's work: a non-default account opens for its
                    # due sequence steps; the default also opens for due inbox/heavy.
                    has_send_work = in_sess and bool(groups.get(acct.pk))
                    default_work = is_default and in_sess and (inbox_due or heavy_due)
                    if not has_send_work and not default_work and not do_sync:
                        get_or_create_session(acct).close_browser()
                        continue
                    session = get_or_create_session(acct)
                    try:
                        try:
                            acquire_session(acct)
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
                        did_send = False
                        if in_sess:
                            try:
                                ran_n = run_states(session, groups.get(acct.pk, []))
                                executed += ran_n
                                did_send = ran_n > 0
                                ran.append(acct.linkedin_username)
                            except AuthenticationError as exc:
                                auto_pause(acct, "LinkedIn 401 during sending (%s)" % exc)
                        # Any account with an open session drains its OWN leftover
                        # manual sends (a second, self-healing path behind the instant
                        # .manual_send flag: whenever a non-default account is already
                        # open for its due sequence steps, flush its queued replies too
                        # — so a dropped/missed flag can't strand them). Scoped per
                        # account, so a session only ever types into its own inbox.
                        if acct.auto_paused_at is None:
                            try:
                                manual += process_pending_sends(session, account=acct)
                            except AuthenticationError as exc:
                                auto_pause(acct, "LinkedIn 401 during manual send (%s)" % exc)
                        # The default account also runs heavy enrichment in its window.
                        if is_default and in_sess and acct.auto_paused_at is None:
                            if heavy_due and acct.auto_paused_at is None:
                                try:
                                    backfilled = backfill_lead_profiles(session, limit=8)
                                    scored = score_pending_leads(limit=15)
                                    last_heavy = time.monotonic()
                                    extra += f" backfilled={backfilled} scored={scored}"
                                except AuthenticationError as exc:
                                    auto_pause(acct, "LinkedIn 401 during enrichment (%s)" % exc)
                        # Reply detection is CONVERSATION-DRIVEN (sync_inbox matches the
                        # real participant → misattribution-proof). Fire on the Unibox/
                        # scheduled sync, OR send-anchored (right after this account sent,
                        # or the slow inbox_due within-window fallback) — never a fixed
                        # cadence (a regular beat is itself a tell). Every account syncs
                        # (Toby's too); it's a read, so off-hours is fine.
                        send_anchored = in_sess and (did_send or inbox_due)
                        if acct.auto_paused_at is None and (do_sync or send_anchored):
                            try:
                                synced = sync_inbox(session)
                                extra += f" synced[{acct.linkedin_username}]={synced}"
                                if send_anchored:
                                    last_inbox = time.monotonic()  # keep the 300s floor
                            except AuthenticationError as exc:
                                auto_pause(acct, "LinkedIn 401 during inbox sync (%s)" % exc)
                    finally:
                        # Close before the next account opens (strict serialize).
                        session.close_browser()

                if sync_requested:
                    INBOX_FLAG.unlink(missing_ok=True)
                if full_sync_due:  # mark the slot(s) that just fired so each runs once/day
                    for i, t in enumerate(sync_targets):
                        if now_dt >= t:
                            slots_done.add(i)
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
                    f"manual_sent={manual}{extra}{note}")
            except CycleTimeout:
                # Hung browser op — exit for a clean systemd relaunch (reuses cookies +
                # identical fingerprint: no re-login, no send burst, no detection signal).
                signal.alarm(0)
                logger.error("worker cycle exceeded %ss — hung browser op; exiting for a clean restart", CYCLE_BUDGET)
                try:
                    post_text(":rotating_light: OpenOutreach worker hung (>%ss) — auto-restarting for a clean session." % CYCLE_BUDGET)
                except Exception:
                    pass
                self.stdout.flush()
                self.stderr.flush()
                os._exit(1)
            except Exception as exc:  # keep the worker alive across transient errors
                consecutive_errors += 1
                self.stderr.write(f"cycle error ({consecutive_errors}): {exc!r}")
                logger.error("Worker cycle error:\n%s", traceback.format_exc())
            finally:
                signal.alarm(0)  # never let the alarm fire during the sleep
            sleep_for = interval * (min(consecutive_errors, 5) or 1) if consecutive_errors else interval
            # Wake within ~3s of a manual Unibox send instead of waiting the full
            # cycle, so a human reply goes out near-instantly.
            slept = 0.0
            while slept < sleep_for:
                if MANUAL_FLAG.exists():
                    break
                step = min(3.0, sleep_for - slept)
                time.sleep(step)
                slept += step
