# linkedin/sequences/executor.py
"""Sequence executor — advances ``LeadCampaignState`` through a Sequence tree.

Polls due, active states; runs the current step's action; routes to the right
branch child (``success``=accepted/replied, ``failure``=not-accepted/no-reply)
or completes. Browser actions are isolated behind small module-level helpers so
unit tests can mock them. Coexists with the autonomous AI-discovery path — only
campaigns with a ``sequence`` are driven here.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone

from linkedin.models import ActionLog, LeadCampaignState, SequenceStep

try:
    from linkedin_cli.exceptions import AuthenticationError
except Exception:  # pragma: no cover - linkedin_cli is always present in prod
    class AuthenticationError(Exception):
        pass

logger = logging.getLogger(__name__)

DEFAULT_CONNECT_DECISION_DAYS = 14
Branch = SequenceStep.Branch

# Total times a step is attempted before the lead is parked as STOPPED_ERROR. So
# a transient Playwright timeout / nav hiccup no longer permanently bricks a lead
# (the old code went terminal on the first exception), while a genuinely broken
# lead still gives up after a few tries. This is an ATTEMPT cap: with value 4 a
# lead is tried 4 times (= 3 backoff retries) and parked on the 4th failure.
MAX_STEP_ATTEMPTS = 4
# Delay before retry N (index = error_count - 1); the last value repeats, so the
# table needs MAX_STEP_ATTEMPTS-1 entries. The send-window + pacing gates still
# apply on top, so an off-hours retry just waits for the next session — these are
# floors, not exact wake times.
_RETRY_BACKOFF = [
    timedelta(minutes=15),
    timedelta(hours=1),
    timedelta(hours=4),
]
# When a due state is picked up we push its due time forward by this lease BEFORE
# running the (irreversible) browser action. A crash between the action and its
# DB save then can't re-run the lead immediately (double-send guard), and a
# concurrent worker/process can't grab the same state. Every success path resets
# next_action_due_at to its real value, so the lease only lingers after a crash.
CLAIM_LEASE = timedelta(minutes=30)

# A send-time reply check that couldn't confirm "no reply" (unreadable/off-page
# conversation) HOLDS the follow-up and retries — a safety defer, NOT a step
# failure, so it must never accrue error_count toward STOPPED_ERROR.
REPLY_CHECK_HOLD = timedelta(hours=3)


# ── Enrollment ────────────────────────────────────────────────────────


def busy_lead_ids(exclude_campaign=None) -> set:
    """Lead ids currently live (active/paused) in some campaign — these must not
    be double-enrolled, or we'd contact the same person twice."""
    State = LeadCampaignState.State
    qs = LeadCampaignState.objects.filter(state__in=[State.ACTIVE, State.PAUSED_MANUAL])
    if exclude_campaign is not None:
        qs = qs.exclude(campaign=exclude_campaign)
    return set(qs.values_list("lead_id", flat=True))


def contacted_lead_ids(exclude_campaign=None) -> set:
    """Lead ids that have EVER been enrolled in a campaign — any state, including
    completed/replied/archived. A person we've already worked must never be
    re-contacted by another campaign, even if they resurface in a new search or
    lead list. (A lead row is unique per person — dedup by public_identifier at
    import — so this is the cross-campaign contact guard.)"""
    qs = LeadCampaignState.objects.all()
    if exclude_campaign is not None:
        qs = qs.exclude(campaign=exclude_campaign)
    return set(qs.values_list("lead_id", flat=True))


def enroll_leads(campaign, leads) -> dict:
    """Create ACTIVE states (due now, at the sequence root) for each lead in
    ``leads`` not already enrolled here OR live in another campaign. Idempotent.
    Returns ``{"enrolled": n, "skipped_duplicate": m}``.
    """
    if not campaign.sequence_id:
        return {"enrolled": 0, "skipped_duplicate": 0}
    root = campaign.sequence.root_step
    if root is None:
        return {"enrolled": 0, "skipped_duplicate": 0}

    existing = set(
        LeadCampaignState.objects.filter(campaign=campaign).values_list("lead_id", flat=True)
    )
    # Anyone already enrolled in ANY other campaign (any state, ever) is off-limits:
    # we contact each person at most once across all campaigns.
    contacted = contacted_lead_ids(exclude_campaign=campaign)
    created = 0
    skipped = 0
    for lead in leads:
        if lead.pk in existing:
            continue
        if lead.pk in contacted:
            skipped += 1  # already worked by another campaign — never double-contact
            continue
        LeadCampaignState.objects.create(
            lead=lead,
            campaign=campaign,
            current_step=root,
            current_branch=Branch.ROOT,
            state=LeadCampaignState.State.ACTIVE,
            next_action_due_at=timezone.now(),
        )
        created += 1
        existing.add(lead.pk)
    return {"enrolled": created, "skipped_duplicate": skipped}


def enroll_campaign(campaign) -> dict:
    """Enroll every lead in the campaign's own ``lead_list``. Idempotent."""
    if not campaign.lead_list_id:
        return {"enrolled": 0, "skipped_duplicate": 0}
    from crm.models import Lead

    return enroll_leads(campaign, Lead.objects.filter(lead_list_id=campaign.lead_list_id))


def enroll_lead_list(campaign, lead_list) -> dict:
    """Enroll a specific lead list's leads into ``campaign`` — lets a campaign
    accumulate leads from more than one list (the lead_list FK stays as the
    campaign's primary list)."""
    from crm.models import Lead

    return enroll_leads(campaign, Lead.objects.filter(lead_list_id=lead_list.pk))


def enroll_active_campaigns() -> dict:
    """Enroll not-yet-enrolled leads for every ACTIVE sequence-driven campaign.
    Cheap (pure DB) — lets a campaign pick up leads added after launch (e.g. a
    lead list still filling toward its target). Returns summed counts.
    """
    from linkedin.models import Campaign

    totals = {"enrolled": 0, "skipped_duplicate": 0}
    qs = Campaign.objects.filter(status=Campaign.Status.ACTIVE, sequence__isnull=False)
    for campaign in qs:
        r = enroll_campaign(campaign)
        totals["enrolled"] += r["enrolled"]
        totals["skipped_duplicate"] += r["skipped_duplicate"]
    return totals


# ── Executor loop ─────────────────────────────────────────────────────


def due_states(campaign=None):
    from linkedin.models import Campaign

    # Only ever run leads whose campaign is ACTIVE — campaign.status is the single
    # source of truth. This means adding leads to a DRAFT/PAUSED campaign can't
    # start outreach before launch, and pausing a campaign halts it even for
    # states created after the pause (which would otherwise be born ACTIVE+due).
    qs = LeadCampaignState.objects.filter(
        state=LeadCampaignState.State.ACTIVE,
        next_action_due_at__lte=timezone.now(),
        campaign__status=Campaign.Status.ACTIVE,
    )
    if campaign is not None:
        qs = qs.filter(campaign=campaign)
    # Highest AI fit first, so capped actions (esp. the ~15/mo InMails) are
    # spent on the best candidates.
    return (
        qs.select_related("current_step", "lead", "campaign", "sending_account")
        .prefetch_related("campaign__sending_accounts")
        .order_by("-lead__ai_score")
    )


def _claim(state) -> bool:
    """Atomically claim a due state before running it. Pushes ``next_action_due_at``
    forward by ``CLAIM_LEASE`` only if the row is still ACTIVE and due — so a reply
    that just stopped the lead, a campaign paused mid-cycle, or another worker that
    grabbed it first all cause us to skip (returns False). The lease is overwritten
    by every success path; it only survives a hard crash, bounding the re-run /
    double-send window to ``CLAIM_LEASE`` instead of the next cycle."""
    now = timezone.now()
    claimed = LeadCampaignState.objects.filter(
        pk=state.pk,
        state=LeadCampaignState.State.ACTIVE,
        next_action_due_at__lte=now,
    ).update(next_action_due_at=now + CLAIM_LEASE)
    return claimed == 1


def _record_step_failure(state, exc) -> None:
    """A step raised. Retry with backoff until ``MAX_STEP_ATTEMPTS`` total
    attempts, then park the lead as STOPPED_ERROR (terminal). The error text is
    stored either way so the dashboard can show why a lead stalled."""
    state.error_count = (state.error_count or 0) + 1
    state.last_error = f"{type(exc).__name__}: {exc}"[:500]
    state.last_error_at = timezone.now()
    if state.error_count >= MAX_STEP_ATTEMPTS:
        state.state = LeadCampaignState.State.STOPPED_ERROR
        state.next_action_due_at = None
        state.save(update_fields=[
            "state", "next_action_due_at", "error_count", "last_error", "last_error_at",
        ])
        logger.error("Lead %s parked STOPPED_ERROR after %d attempts: %s",
                     state.lead_id, state.error_count, state.last_error)
    else:
        backoff = _RETRY_BACKOFF[min(state.error_count - 1, len(_RETRY_BACKOFF) - 1)]
        floor = timezone.now() + backoff
        # Never SHORTEN an already-scheduled future due time. The connect SEND phase
        # persists awaiting_decision=True + a ~14-day accept-wait BEFORE its _log; if
        # that _log then fails, this must not collapse the wait to the backoff (which
        # would re-run the decision ~15 min later, read "not accepted yet", and route
        # the lead down the FAILURE branch prematurely). state.next_action_due_at is
        # the handler's committed value here (not the claim lease — _claim's UPDATE
        # doesn't touch the in-memory object). Only push out, never pull in.
        existing = state.next_action_due_at
        state.next_action_due_at = max(existing, floor) if existing is not None else floor
        state.save(update_fields=[
            "next_action_due_at", "error_count", "last_error", "last_error_at",
        ])
        logger.warning("Lead %s step failed (attempt %d/%d) — retrying after %s: %s",
                       state.lead_id, state.error_count, MAX_STEP_ATTEMPTS, backoff, state.last_error)


def _run_one(session, state) -> bool:
    """Claim, then advance, one due state. Returns True iff a step actually RAN
    (advanced the cursor or resolved the lead) — False if it was skipped (claim
    refused), deferred (cap/pacing), or failed. Never raises: a failing step is
    retried/parked via ``_record_step_failure`` so one bad lead never halts the
    rest of the batch."""
    from linkedin.inbox.poller import ReplyCheckUnverified

    if not _claim(state):
        return False  # no longer ACTIVE+due (reply/pause/raced) — skip
    try:
        ran = advance_state(session, state)
    except AuthenticationError:
        # Account-level failure (a 401 or the HTTP-200 restriction/checkpoint
        # page), NOT a per-lead step failure. Let it propagate so the worker
        # auto-pauses the whole ACCOUNT instead of parking this one lead as
        # STOPPED_ERROR and then driving the next lead on the same flagged
        # session (the June-9 failure mode). The claimed lead stays ACTIVE and
        # re-runs once the account is recovered.
        raise
    except ReplyCheckUnverified:
        # Couldn't confirm the lead hasn't replied → HOLD + retry (fail-closed),
        # never park STOPPED_ERROR. A defer, not a step failure: leave error_count.
        LeadCampaignState.objects.filter(pk=state.pk).update(
            next_action_due_at=timezone.now() + REPLY_CHECK_HOLD)
        return False
    except Exception as exc:
        logger.exception("Sequence step failed for %s", state)
        _record_step_failure(state, exc)
        return False
    if ran and state.error_count:
        # A previously-failing step finally RAN to completion — clear the retry
        # counter so transient blips don't accumulate. Only on a real run, NOT on a
        # cap/pacing deferral (which leaves the same step pending), or a broken lead
        # could refill its retry budget every time it happens to hit a cap.
        LeadCampaignState.objects.filter(pk=state.pk).update(error_count=0)
        state.error_count = 0
    return bool(ran)


def run_due_states(session, campaign=None, limit=None) -> int:
    """Execute every due state once. Returns the number advanced."""
    qs = due_states(campaign)
    if limit:
        qs = qs[:limit]
    return sum(1 for state in list(qs) if _run_one(session, state))



def _account_for_state(state):
    """The STICKY owner of an already-claimed lead, or None while it is still
    pre-connect (unowned). Ownership is appointed only when the connect request is
    actually sent (``_handle_connect``) and never changes afterwards — a lead's
    whole sequence stays on the one identity that made its connection (anti-ban
    Rule #6: a mid-sequence identity switch is a cross-account correlation signal).

    Guard on ``last_action_at``: ownership is stamped atomically with the connect
    SEND (which also sets ``last_action_at``), so a pre-connect row carrying a
    ``sending_account`` but no action yet is NOT a real owner — honoring it would
    pin the whole pool to one account; treat it as unowned -> shared round-robin."""
    if state.sending_account_id and state.last_action_at is not None:
        return state.sending_account
    return None


def due_states_by_account(fallback_profile):
    """Group every due state by the account that will run it.

    Owned states stay STICKY to their ``sending_account``. Unowned (pre-connect)
    states are a SHARED POOL: each is handed to a campaign account that is active,
    not auto-paused, in its send window and still has connect headroom — taking the
    one with the most remaining cap this cycle (ties → least-recently-used), so
    volume spreads evenly across accounts and maxes each one's configured cap. The
    choice is NOT persisted here (the owner is recorded only on send), so an unsent
    lead re-routes to whoever has room next cycle. Timing is untouched:
    ``advance_state``'s ``has_capacity``/``next_action_at`` gate still drips each
    account's connects randomly across ITS own working-day window. Returns
    ``(profile, [states])`` in ai-score order within each account."""
    from django.utils import timezone

    from linkedin.accounts.limits import cap_for, daily_count, has_capacity, in_session

    now = timezone.now()
    groups = {}      # pk -> [profile, [states]]
    pool_cache = {}  # campaign_id -> [eligible profiles, LRU-sorted]
    cap = {}         # profile pk -> its OWN connect cap today
    sent = {}        # profile pk -> connects it has already sent today
    given = {}       # profile pk -> leads handed to it THIS cycle (round-robin counter)

    def _eligible(campaign):
        if campaign.id not in pool_cache:
            elig = []
            for pr in campaign.sending_accounts.all():
                if not pr.active or pr.auto_paused_at is not None:
                    continue
                if not in_session(pr, now) or not has_capacity(pr, "connect"):
                    continue
                cap[pr.pk] = cap_for(pr, "connect")
                sent[pr.pk] = daily_count(pr, "connect")
                given[pr.pk] = 0
                elig.append(pr)
            # never-used first, then oldest last_used_at — LRU round-robin.
            elig.sort(key=lambda a: (a.last_used_at is not None, a.last_used_at or now))
            pool_cache[campaign.id] = elig
        return pool_cache[campaign.id]

    def _add(acct, st):
        groups.setdefault(acct.pk, [acct, []])[1].append(st)

    for st in due_states():
        owner = _account_for_state(st)
        if owner is not None:                       # already-connected → sticky
            _add(owner, st)
            continue
        pool = _eligible(st.campaign)
        if not pool:
            if fallback_profile is not None and fallback_profile.auto_paused_at is None:
                _add(fallback_profile, st)          # pool-less campaign → default account
            continue
        # Each account operates INDEPENDENTLY: bounded only by ITS OWN daily cap, with
        # leads dealt round-robin (fewest handed out THIS cycle, ties → least-recently-
        # used) — an account's share never depends on how much the OTHERS have sent. Its
        # own randomised cap + next_action_at pacing govern its rate across its own window.
        free = [a for a in pool if sent[a.pk] + given[a.pk] < cap[a.pk]]
        if not free:
            continue                                # every eligible account at its own cap
        chosen = min(free, key=lambda a: given[a.pk])
        given[chosen.pk] += 1
        _add(chosen, st)
    return [(prof, states) for prof, states in groups.values()]


def run_states(session, states) -> int:
    """Advance a specific list of due states under *session*. Returns how many
    advanced; each state is claimed first and a step that raises is retried with
    backoff (then parked STOPPED_ERROR), so one bad lead never halts the rest."""
    return sum(1 for state in states if _run_one(session, state))


def active_sending_accounts(fallback_profile):
    """Distinct accounts to service this cycle: the union of every ACTIVE
    campaign's sending_accounts pool (fallback_profile for campaigns whose pool is
    empty). Drives per-account reply polling even with no due state right now."""
    from linkedin.models import Campaign

    accts = {}
    for c in Campaign.objects.filter(status=Campaign.Status.ACTIVE).prefetch_related("sending_accounts"):
        pool = list(c.sending_accounts.all()) or ([fallback_profile] if fallback_profile else [])
        for a in pool:
            if a.auto_paused_at is None:  # never schedule an auto-paused (flagged) account
                accts.setdefault(a.pk, a)
    return list(accts.values())


_STEP_ACTION = {
    SequenceStep.StepType.CONNECT: ActionLog.ActionType.CONNECT,
    SequenceStep.StepType.MESSAGE: ActionLog.ActionType.MESSAGE,
    SequenceStep.StepType.INMAIL: ActionLog.ActionType.INMAIL,
    SequenceStep.StepType.PROFILE_VISIT: ActionLog.ActionType.PROFILE_VISIT,
    SequenceStep.StepType.LIKE_POST: ActionLog.ActionType.LIKE_POST,
}


def advance_state(session, state) -> bool:
    """Run the current step once. Returns True if a step actually ran (cursor
    advanced or lead resolved to a terminal state), False if it was DEFERRED
    (account at cap / paced to a later slot / out of monthly InMail) — i.e. the
    same step is still pending. The caller uses this to avoid counting a deferral
    as progress or clearing the retry counter on it."""
    step = state.current_step
    if step is None:
        _complete(state)
        return True
    # Connection-provenance guard at the MESSAGE stage: never directly message
    # someone whose connection the kit didn't make itself (unless the campaign
    # opts into the existing network). Checked before the cap/pacing gate so an
    # excluded lead doesn't burn a send slot. (Connect runs its own status-aware
    # guard inside _handle_connect; InMail targets non-connectors by design;
    # profile-visit/like are warm-up signals that may legitimately precede a
    # connect.) This is what stops the kit re-messaging existing connections /
    # already-contacted people pulled in by the AI finder.
    if step.step_type == SequenceStep.StepType.MESSAGE and not _may_message(state):
        _skip_existing(state)
        return True
    # M6: defer when this account is at its daily cap for the step's action.
    action = _STEP_ACTION.get(step.step_type)
    consumes_cap = action and not (
        step.step_type == SequenceStep.StepType.CONNECT and state.awaiting_decision
    )
    if consumes_cap:
        from linkedin.accounts.limits import has_capacity, next_action_at
        if not has_capacity(session.linkedin_profile, action):
            _defer_to_tomorrow(state)
            return False
        # Pace the day's budget across the account's send window (and never send
        # outside it) — drip, don't burst.
        slot = next_action_at(session.linkedin_profile, action)
        if slot > timezone.now():
            _defer_until(state, slot)
            return False
    # InMail also respects the monthly Premium allowance.
    if step.step_type == SequenceStep.StepType.INMAIL:
        from linkedin.accounts.limits import has_inmail_monthly_capacity
        if not has_inmail_monthly_capacity(session.linkedin_profile):
            _defer_to_tomorrow(state)
            return False
    handler = _HANDLERS.get(step.step_type)
    if handler is None:
        raise ValueError(f"Unknown step_type {step.step_type!r}")
    # Browser actions assume a live page; ensure it (idempotent) for any
    # step that touches LinkedIn (wait/end don't).
    if step.step_type not in (SequenceStep.StepType.WAIT, SequenceStep.StepType.END, SequenceStep.StepType.BLANK):
        session.ensure_browser()
    handler(session, state, step)
    return True


# ── Step handlers ─────────────────────────────────────────────────────


def _handle_connect(session, state, step):
    from linkedin_cli.enums import ProfileState

    if not state.awaiting_decision:
        # ── Connection-provenance guard ──────────────────────────────────
        # The kit only builds on connections IT makes. Check the live status
        # BEFORE sending: if the person is ALREADY connected (or already has a
        # request pending) — i.e. a connection this kit did not create — exclude
        # them, unless the campaign deliberately includes the existing network.
        status = connection_status(session, state)
        preexisting = status in (str(ProfileState.CONNECTED), str(ProfileState.PENDING))
        if preexisting and not state.campaign.include_current_network:
            _skip_existing(state, status)
            return
        if status == str(ProfileState.CONNECTED):
            # Already a connection AND the campaign opted into the existing network
            # — there's no request to send; proceed straight down the accepted
            # branch. (connected_via_tool stays False: this wasn't the kit's doing,
            # it's an intentional existing-network contact.)
            _goto(state, step.next_step(Branch.SUCCESS))
            return
        # NOT_CONNECTED (or PENDING with the toggle on) → send the kit's request.
        send_connection_request(session, state, step)
        # Persist the "request sent → awaiting accept" checkpoint BEFORE logging,
        # so a failure in the ActionLog/cap write can't re-enter this SEND phase on
        # retry and fire a SECOND invite. A lost _log only under-counts the connect
        # (acceptable); a double-invite is the cardinal sin this system avoids.
        wait_days = int(step.config.get("wait_days_before_branch_decision", DEFAULT_CONNECT_DECISION_DAYS))
        # Appoint the owner at the instant the request is sent: pre-connect leads
        # are an unowned shared pool, but from here the lead is STICKY to this
        # identity for the rest of its sequence (anti-ban Rule #6).
        state.sending_account = session.linkedin_profile
        state.awaiting_decision = True
        state.last_action_at = timezone.now()
        state.next_action_due_at = timezone.now() + timedelta(days=wait_days)
        state.save(update_fields=["sending_account", "awaiting_decision", "last_action_at", "next_action_due_at"])
        _log(session, state, step, ActionLog.ActionType.CONNECT)
        return
    # Decision phase: accepted → success branch, else → failure branch.
    accepted = is_connection_accepted(session, state)
    if accepted:
        # The kit's OWN connection request was accepted → this is a tool-made
        # connection, so messaging is allowed downstream.
        state.connected_via_tool = True
        # Record the acceptance (a detected result of our outreach, not a capped
        # action) so the dashboard can report connections accepted. get_or_create
        # keyed on (profile, campaign, lead, type) so a decision-phase retry (e.g.
        # _goto's save failed after this ran) doesn't double-count the acceptance —
        # an accept is a once-per-(lead, campaign) event.
        ActionLog.objects.get_or_create(
            linkedin_profile=session.linkedin_profile,
            campaign=state.campaign,
            lead=state.lead,
            action_type=ActionLog.ActionType.CONNECT_ACCEPTED,
        )
    # Clear awaiting_decision as part of the cursor move below (NOT before the
    # ActionLog write): if anything here fails, the row stays awaiting_decision=True
    # and a retry re-runs this idempotent decision (re-reads live status) instead of
    # re-entering the SEND phase and firing a duplicate invite. _goto/_complete
    # persist awaiting_decision + connected_via_tool atomically with the move.
    state.awaiting_decision = False
    branch = Branch.SUCCESS if accepted else Branch.FAILURE
    _goto(state, step.next_step(branch))


def _may_message(state) -> bool:
    """Messaging is only allowed when the kit made this connection itself, or the
    campaign deliberately includes the existing network."""
    return state.connected_via_tool or state.campaign.include_current_network


def _skip_existing(state, status="") -> None:
    """Exclude a lead the kit didn't connect itself — no outreach sent."""
    state.state = LeadCampaignState.State.SKIPPED_EXISTING
    state.next_action_due_at = None
    state.save(update_fields=["state", "next_action_due_at"])
    logger.info(
        "Lead %s skipped — existing/non-tool connection (%s); enable 'include current "
        "network' on the campaign to contact them deliberately.", state.lead_id, status or "preexisting",
    )


def _mark_contacted(session, state):
    """Record that THIS tool messaged the lead — gates the Unibox so it never
    shows the account's pre-existing (e.g. other-tool) LinkedIn conversations."""
    from linkedin.models import MessageThread

    MessageThread.objects.update_or_create(
        lead=state.lead, account=session.linkedin_profile,
        defaults={"contacted_by_tool": True},
    )


def _handle_message(session, state, step):
    # Live reply-check FIRST: if the lead replied (e.g. during the preceding
    # Wait), STOP — never send the next message after a reply. The background
    # poller may not have scanned this lead yet, so we re-check at send time.
    from linkedin.inbox.poller import has_new_reply
    if has_new_reply(session, state):
        logger.info("Lead %s replied before the scheduled message — stopping sequence", state.lead_id)
        _set_state(state, LeadCampaignState.State.STOPPED_REPLY)
        return
    send_message(session, state, step)
    # Advance the cursor BEFORE bookkeeping: if _mark_contacted/_log fails, the
    # retry resumes at the NEXT step instead of re-sending this message.
    # (contacted_by_tool is self-healed by the poller; a lost _log only
    # under-counts.) Continue down the no-reply (failure) branch; M3 reply
    # detection halts on reply.
    _goto(state, step.next_step(Branch.FAILURE))
    _mark_contacted(session, state)
    _log(session, state, step, ActionLog.ActionType.MESSAGE)


def _handle_inmail(session, state, step):
    # Don't spend a precious InMail on someone who connected during the wait.
    if is_connection_accepted(session, state):
        logger.info("Lead %s connected before InMail — skipping InMail", state.lead_id)
        _goto(state, step.next_step(Branch.SUCCESS))
        return
    from linkedin.inbox.poller import has_new_reply
    if has_new_reply(session, state):
        logger.info("Lead %s replied before the scheduled InMail — stopping sequence", state.lead_id)
        _set_state(state, LeadCampaignState.State.STOPPED_REPLY)
        return
    result = send_inmail(session, state, step)
    # Advance the cursor BEFORE bookkeeping so a failed _log can't re-send (and
    # waste) the scarce monthly InMail on retry.
    _goto(state, step.next_step(Branch.SUCCESS))
    if result.get("success"):
        _mark_contacted(session, state)
        _log(session, state, step, ActionLog.ActionType.INMAIL)
    else:
        logger.info("InMail not sent for %s (%s) — continuing", state, result.get("error"))


def _handle_wait(session, state, step):
    # "Wait N days" = N *working* days ahead, at a random time within the
    # account's send window — not an exact 24h, and never on a non-working day.
    from linkedin.accounts.limits import random_slot_in_working_days

    days = int(step.config.get("days", 0))
    due = random_slot_in_working_days(session.linkedin_profile, days)
    _goto(state, step.next_step(Branch.SUCCESS), delay=(due - timezone.now()))


def _handle_end(session, state, step):
    # Explicit terminal step — the lead has reached the end of this branch.
    _complete(state)


def _handle_blank(session, state, step):
    # No-op pass-through — flips an End back on: continues to the next step
    # without doing anything itself.
    _goto(state, step.next_step(Branch.SUCCESS))


def _handle_profile_visit(session, state, step):
    visit_profile(session, state)
    # Advance BEFORE logging so a failed _log can't re-visit (and re-consume the
    # profile-visit cap) on retry.
    _goto(state, step.next_step(Branch.SUCCESS))
    _log(session, state, step, ActionLog.ActionType.PROFILE_VISIT)


def _handle_like_post(session, state, step):
    result = like_recent_post(session, state) or {}
    # Advance BEFORE logging so a failed _log can't re-run the like on retry.
    _goto(state, step.next_step(Branch.SUCCESS))
    # Only record the like (which consumes the daily cap and feeds the "posts
    # liked" KPI) when it actually succeeded — e.g. the lead has no recent post,
    # or the like button wasn't found. Otherwise we'd inflate the metric and burn
    # cap on no-ops. The sequence still advances either way.
    if result.get("success") or result.get("liked"):
        _log(session, state, step, ActionLog.ActionType.LIKE_POST, target_url=result.get("post_url", ""))
    else:
        logger.info("Like skipped for %s (%s) — no record", state.lead_id, result.get("error") or "no recent post")


_HANDLERS = {
    SequenceStep.StepType.CONNECT: _handle_connect,
    SequenceStep.StepType.MESSAGE: _handle_message,
    SequenceStep.StepType.INMAIL: _handle_inmail,
    SequenceStep.StepType.WAIT: _handle_wait,
    SequenceStep.StepType.PROFILE_VISIT: _handle_profile_visit,
    SequenceStep.StepType.LIKE_POST: _handle_like_post,
    SequenceStep.StepType.END: _handle_end,
    SequenceStep.StepType.BLANK: _handle_blank,
}


# ── Cursor movement ───────────────────────────────────────────────────


def _goto(state, next_step, delay=None):
    if next_step is None:
        _complete(state)
        return
    state.current_step = next_step
    state.current_branch = next_step.branch
    state.last_action_at = timezone.now()
    state.next_action_due_at = timezone.now() + (delay or timedelta(0))
    # awaiting_decision/connected_via_tool are persisted here (not in a separate
    # earlier save) so the connect decision phase clears awaiting_decision
    # ATOMICALLY with the cursor move — a mid-step failure then re-runs the
    # idempotent decision, never the SEND phase. For every other caller these
    # carry their unchanged values, so writing them is a harmless no-op.
    state.save(update_fields=[
        "current_step", "current_branch", "last_action_at", "next_action_due_at",
        "awaiting_decision", "connected_via_tool",
    ])


def _complete(state):
    state.state = LeadCampaignState.State.COMPLETED
    state.next_action_due_at = None
    # Same rationale as _goto: persist awaiting_decision/connected_via_tool with the
    # terminal move so the decision phase's clear is atomic (e.g. an accepted
    # connect whose SUCCESS branch is empty completes here).
    state.save(update_fields=[
        "state", "next_action_due_at", "awaiting_decision", "connected_via_tool",
    ])


def _defer_to_tomorrow(state):
    state.next_action_due_at = timezone.now() + timedelta(days=1)
    state.save(update_fields=["next_action_due_at"])


def _defer_until(state, when):
    state.next_action_due_at = when
    state.save(update_fields=["next_action_due_at"])


def _set_state(state, new_state):
    state.state = new_state
    state.save(update_fields=["state"])


def _log(session, state, step, action_type, target_url=""):
    from linkedin.accounts.limits import record_action

    ActionLog.objects.create(
        linkedin_profile=session.linkedin_profile,
        campaign=state.campaign,
        lead=state.lead,
        action_type=action_type,
        sequence_step=step,
        target_url=target_url or "",
    )
    record_action(session.linkedin_profile, action_type)


# ── Template rendering ────────────────────────────────────────────────


def _spin(text):
    """Expand spintax: ``{a|b|c}`` -> one random choice, innermost-first so nesting
    works. Leaves single-tag braces like ``{first_name}`` (no ``|``) untouched, so
    each recipient gets a structurally different message (defeats NLP templating
    detection)."""
    import random
    import re

    pattern = re.compile(r"\{([^{}]*\|[^{}]*)\}")
    guard = 0
    while guard < 200:
        m = pattern.search(text)
        if not m:
            break
        text = text[:m.start()] + random.choice(m.group(1).split("|")) + text[m.end():]
        guard += 1
    return text


def render_template(template: str, context: dict, fallback: str = "") -> str:
    """Render a template against ``context``. Supports both HeyReach-style
    ``{first_name}`` placeholders and Jinja ``{{ first_name }}``. Empty result
    falls back to ``fallback``.
    """
    if not template:
        return fallback
    try:
        import re

        from jinja2 import Template

        # Jinja FIRST so ``{{ first_name }}`` resolves correctly; a single brace
        # ``{first_name}`` is literal text to Jinja and survives untouched.
        text = Template(template).render(**context)
        # Then the HeyReach-style single-brace tags. Only substitute KNOWN keys —
        # an unknown ``{tag}`` is left verbatim rather than silently blanked, so a
        # typo'd tag is visible instead of producing a half-empty message.
        text = re.sub(
            r"\{(\w+)\}",
            lambda m: str(context[m.group(1)]) if m.group(1) in context else m.group(0),
            text,
        )
        text = _spin(text)
        rendered = text.strip()
    except Exception:
        return fallback
    return rendered or fallback


def _lead_context(state, profile=None) -> dict:
    lead = state.lead
    return {
        "first_name": lead.first_name or "",
        "last_name": lead.last_name or "",
        "company": lead.company or "",
        "public_identifier": lead.public_identifier,
        # {sender_first_name} = the sending account's customisable sign-off name.
        "sender_first_name": (getattr(profile, "signoff_name", "") or ""),
        "sender_name": (getattr(profile, "signoff_name", "") or ""),  # alias for {sender_first_name}
    }


# ── Browser-action wrappers (mocked in tests) ─────────────────────────


def send_connection_request(session, state, step):
    from linkedin_cli.actions.connect import send_connection_request as _send
    from linkedin_cli.actions.status import get_connection_status

    lead = state.lead
    pdict = {"public_identifier": lead.public_identifier, "url": lead.linkedin_url, "urn": lead.urn or ""}
    # The connect verb assumes the profile page is already open; this navigates
    # there. get_connection_status takes a profile dict, not the id string.
    get_connection_status(session, pdict)
    # Last guard before the irreversible invite: get_connection_status just
    # navigated to the profile, so if it's actually the restriction page, stop
    # here (auto-pause) rather than send. A double/at-risk invite is the cardinal
    # sin this path avoids.
    session.assert_not_restricted()
    from linkedin.browser.humanize import humanize_page
    humanize_page(session.page)
    # If the step carries a personalised note, send WITH it via the app-side flow
    # (linkedin_cli's connect verb is note-less). Render placeholders first; fall
    # back to the note-less verb if the note is empty or the with-note flow fails.
    note = render_template(step.config.get("personalised_note", ""), _lead_context(state, session.linkedin_profile), "")
    if note:
        from linkedin.actions.connect_note import send_connection_request_with_note

        result = send_connection_request_with_note(session, pdict, note)
        if result.get("success"):
            return
        logger.warning("Connect-with-note failed for %s (%s) — sending note-less request",
                       lead.public_identifier, result.get("error"))
    _send(session, pdict)


def connection_status(session, state) -> str:
    """The lead's current connection status as a ProfileState string
    (CONNECTED / PENDING / NOT_CONNECTED). The single mockable boundary the
    provenance guard and acceptance check both read."""
    from linkedin_cli.actions.status import get_connection_status

    lead = state.lead
    pdict = {"public_identifier": lead.public_identifier, "url": lead.linkedin_url, "urn": lead.urn or ""}
    status = str(get_connection_status(session, pdict))
    # get_connection_status navigated to the profile. If LinkedIn served its
    # restriction/checkpoint page instead (HTTP 200 — reads as 'no connect button'
    # → NOT_CONNECTED, so we'd fire an invite at a flagged account), catch it now.
    # Raises AuthenticationError → _run_one re-raises → worker auto-pauses. This is
    # the mid-cycle guard the feed-load-only check at session-open misses.
    session.assert_not_restricted()
    return status


def is_connection_accepted(session, state) -> bool:
    from linkedin_cli.enums import ProfileState

    return connection_status(session, state) == str(ProfileState.CONNECTED)


def send_message(session, state, step):
    from linkedin_cli.actions.message import send_raw_message

    lead = state.lead
    body = render_template(
        step.config.get("template", ""),
        _lead_context(state, session.linkedin_profile),
        step.config.get("fallback", ""),
    )
    urn = lead.urn or lead.get_urn(session)
    pdict = {"public_identifier": lead.public_identifier, "url": lead.linkedin_url, "urn": urn}
    send_raw_message(session, pdict, body)


def send_inmail(session, state, step):
    from linkedin.actions.inmail import send_inmail as _send

    ctx = _lead_context(state, session.linkedin_profile)
    subject = render_template(step.config.get("subject", ""), ctx, step.config.get("subject_fallback", ""))
    body = render_template(step.config.get("body", ""), ctx, step.config.get("body_fallback", ""))
    return _send(session, state.lead, subject, body)


def visit_profile(session, state):
    from linkedin_cli.actions.search import visit_profile as _visit

    _visit(session, {"public_identifier": state.lead.public_identifier, "url": state.lead.linkedin_url})
    from linkedin.browser.humanize import humanize_page
    humanize_page(session.page)


def like_recent_post(session, state):
    from linkedin.actions.like import like_most_recent_post

    return like_most_recent_post(session, state.lead)
