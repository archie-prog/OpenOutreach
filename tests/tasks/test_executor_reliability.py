# tests/tasks/test_executor_reliability.py
"""§6 reliability hardening for the sequence executor:

- a step that raises is RETRIED with backoff (error_count climbs) and only parked
  as STOPPED_ERROR after MAX_STEP_ATTEMPTS — the old code went terminal on the
  first exception, so one Playwright timeout permanently bricked a lead;
- a cap/pacing DEFERRAL is not a success: it neither counts as advanced nor
  resets the retry counter (else a broken lead refills its budget on every cap);
- a success clears the retry counter (only after a real run);
- each due state is CLAIMED atomically before it runs (state=ACTIVE AND due<=now),
  so a stopped/paused/leased/not-yet-due row is skipped — never double-run;
- every successful advance overwrites the 30-min claim lease (terminal → due=None
  or a fresh handler due time), so the lease never becomes a lead's real schedule.
"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from tests.factories import UserFactory


def _one_connect_state(owner, fake_session):
    """A campaign with a single root CONNECT step and one ACTIVE, due lead state."""
    from crm.models import Lead
    from linkedin.models import (
        Campaign,
        LeadCampaignState,
        LeadList,
        Sequence,
        SequenceStep as S,
    )

    seq = Sequence.objects.create(name="S", owner=owner)
    connect = S.objects.create(
        sequence=seq, branch=S.Branch.ROOT, step_type=S.StepType.CONNECT, config={},
    )
    ll = LeadList.objects.create(name="L", owner=owner, source_type=LeadList.SourceType.CSV)
    lead = Lead.objects.create(
        linkedin_url="https://www.linkedin.com/in/x/", public_identifier="x", lead_list=ll,
    )
    campaign = Campaign.objects.create(
        name="C", sequence=seq, lead_list=ll, status=Campaign.Status.ACTIVE,
    )
    campaign.users.add(fake_session.django_user)
    state = LeadCampaignState.objects.create(
        lead=lead, campaign=campaign, current_step=connect, current_branch=S.Branch.ROOT,
        state=LeadCampaignState.State.ACTIVE, next_action_due_at=timezone.now(),
    )
    return campaign, state


def _arm(state):
    """Re-arm a state to ACTIVE + due now (simulates the next worker cycle)."""
    from linkedin.models import LeadCampaignState

    LeadCampaignState.objects.filter(pk=state.pk).update(
        state=LeadCampaignState.State.ACTIVE, next_action_due_at=timezone.now())


def _boom(*a, **k):
    raise RuntimeError("playwright timeout")


@pytest.mark.django_db
def test_step_failure_is_retried_with_backoff_then_parked(fake_session):
    from linkedin.models import LeadCampaignState
    from linkedin.sequences import executor

    State = LeadCampaignState.State
    campaign, state = _one_connect_state(UserFactory(), fake_session)

    with patch.multiple(executor, connection_status=_boom):
        # Each failure short of the cap leaves the lead ACTIVE, backed off PER THE
        # SCHEDULE (not merely > now — which the 30-min claim lease would satisfy).
        for attempt in range(1, executor.MAX_STEP_ATTEMPTS):
            _arm(state)
            before = timezone.now()
            assert executor.run_due_states(fake_session, campaign=campaign) == 0  # failure not counted
            state.refresh_from_db()
            assert state.state == State.ACTIVE
            assert state.error_count == attempt
            assert "playwright timeout" in state.last_error
            assert state.last_error_at is not None
            expected = before + executor._RETRY_BACKOFF[min(attempt - 1, len(executor._RETRY_BACKOFF) - 1)]
            assert abs((state.next_action_due_at - expected).total_seconds()) < 120
            # Specifically NOT the claim lease (the gap would be 15min vs 30min on attempt 1).
            assert abs((state.next_action_due_at - (before + executor.CLAIM_LEASE)).total_seconds()) > 60 \
                or executor._RETRY_BACKOFF[attempt - 1] == executor.CLAIM_LEASE

        # The MAX-th consecutive failure parks the lead (terminal), error preserved.
        _arm(state)
        executor.run_due_states(fake_session, campaign=campaign)
        state.refresh_from_db()
        assert state.state == State.STOPPED_ERROR
        assert state.error_count == executor.MAX_STEP_ATTEMPTS
        assert state.next_action_due_at is None
        assert "playwright timeout" in state.last_error

    # A parked lead is NOT re-run on the next cycle (due_states excludes STOPPED_ERROR).
    calls = []
    with patch.object(executor, "connection_status", lambda *a, **k: calls.append(1) or "not_connected"):
        assert executor.run_due_states(fake_session, campaign=campaign) == 0
    assert calls == []
    state.refresh_from_db()
    assert state.state == State.STOPPED_ERROR
    assert state.error_count == executor.MAX_STEP_ATTEMPTS  # unchanged


@pytest.mark.django_db
def test_error_count_resets_only_after_a_real_success(fake_session):
    """fail, fail (count=2) → succeed (count=0) → fail (count=1, NOT resuming at 3)."""
    from linkedin.models import LeadCampaignState
    from linkedin.sequences import executor

    State = LeadCampaignState.State
    campaign, state = _one_connect_state(UserFactory(), fake_session)

    with patch.object(executor, "connection_status", _boom):
        for _ in range(2):
            _arm(state)
            executor.run_due_states(fake_session, campaign=campaign)
    state.refresh_from_db()
    assert state.error_count == 2 and state.state == State.ACTIVE

    # A real success (connect sent → awaiting accept) clears the counter.
    _arm(state)
    with patch.multiple(
        executor,
        connection_status=lambda *a, **k: "not_connected",
        send_connection_request=lambda *a, **k: None,
    ):
        assert executor.run_due_states(fake_session, campaign=campaign) == 1
    state.refresh_from_db()
    assert state.error_count == 0
    assert state.awaiting_decision is True
    # Due time is the connect-decision wait (~14 days), not the 30-min claim lease.
    assert state.next_action_due_at > timezone.now() + timedelta(days=10)

    # A subsequent failure (now in the decision phase) starts again at 1, not 3.
    _arm(state)
    with patch.object(executor, "is_connection_accepted", _boom):
        executor.run_due_states(fake_session, campaign=campaign)
    state.refresh_from_db()
    assert state.error_count == 1
    assert state.state == State.ACTIVE


@pytest.mark.django_db
def test_cap_deferral_is_not_a_success(fake_session):
    """A cap deferral must NOT count as advanced and must NOT reset error_count
    (else a broken lead refills its retry budget whenever it hits a cap)."""
    from linkedin.models import LeadCampaignState
    from linkedin.sequences import executor

    campaign, state = _one_connect_state(UserFactory(), fake_session)
    LeadCampaignState.objects.filter(pk=state.pk).update(
        error_count=2, next_action_due_at=timezone.now())
    # Force the account over its connect cap so advance_state defers.
    prof = fake_session.linkedin_profile
    prof.daily_caps_json = {"connect": 0}
    prof.save(update_fields=["daily_caps_json"])

    with patch.multiple(
        executor,
        connection_status=lambda *a, **k: "not_connected",
        send_connection_request=lambda *a, **k: None,
    ):
        assert executor.run_due_states(fake_session, campaign=campaign) == 0  # deferral not counted

    state.refresh_from_db()
    assert state.error_count == 2                               # NOT reset by a deferral
    assert state.state == LeadCampaignState.State.ACTIVE
    assert state.next_action_due_at > timezone.now() + timedelta(hours=1)  # deferred to tomorrow


@pytest.mark.django_db
def test_claim_skips_state_that_stopped_before_running(fake_session):
    """A reply (or pause) that flips the row out of ACTIVE after it was selected
    must cause _claim to skip it — the action never runs (no double-contact)."""
    from linkedin.models import LeadCampaignState
    from linkedin.sequences import executor

    State = LeadCampaignState.State
    campaign, state = _one_connect_state(UserFactory(), fake_session)

    calls = []

    def track(*a, **k):
        calls.append(1)
        return "not_connected"

    LeadCampaignState.objects.filter(pk=state.pk).update(state=State.STOPPED_REPLY)

    with patch.multiple(
        executor, connection_status=track, send_connection_request=lambda *a, **k: None,
    ):
        assert executor.run_states(fake_session, [state]) == 0

    assert calls == []  # claim refused → handler never ran
    state.refresh_from_db()
    assert state.state == State.STOPPED_REPLY


@pytest.mark.django_db
def test_claim_skips_not_yet_due_state(fake_session):
    """run_states relies SOLELY on _claim for the due-check (due_states does not
    re-filter its input). A not-yet-due ACTIVE row — e.g. one already leased into
    the future by another worker — must be skipped, never run. This guards the
    core double-send invariant."""
    from linkedin.models import LeadCampaignState
    from linkedin.sequences import executor

    campaign, state = _one_connect_state(UserFactory(), fake_session)
    LeadCampaignState.objects.filter(pk=state.pk).update(
        next_action_due_at=timezone.now() + timedelta(hours=1))

    calls = []

    def track(*a, **k):
        calls.append(1)
        return "not_connected"

    with patch.multiple(
        executor, connection_status=track, send_connection_request=lambda *a, **k: None,
    ):
        assert executor.run_states(fake_session, [state]) == 0

    assert calls == []  # not due → claim refused → handler never ran
    state.refresh_from_db()
    assert state.state == LeadCampaignState.State.ACTIVE
    assert state.next_action_due_at > timezone.now() + timedelta(minutes=50)  # untouched, not leased


@pytest.mark.django_db
def test_claim_leases_due_time_before_acting(fake_session):
    """Claiming pushes next_action_due_at forward up-front, so a crash between the
    browser action and its save can't immediately re-run (double-send) the lead."""
    from linkedin.models import LeadCampaignState
    from linkedin.sequences import executor

    campaign, state = _one_connect_state(UserFactory(), fake_session)
    seen = {}

    def capture(session, st):
        seen["due"] = LeadCampaignState.objects.get(pk=st.pk).next_action_due_at
        raise RuntimeError("crash mid-action")

    with patch.object(executor, "advance_state", capture):
        executor.run_due_states(fake_session, campaign=campaign)

    assert seen["due"] > timezone.now() + timedelta(minutes=20)


@pytest.mark.django_db
def test_connect_log_failure_preserves_decision_wait_and_does_not_resend(fake_session):
    """If _log fails AFTER the connect SEND checkpoint persisted the ~14-day accept
    wait, the retry backoff must NOT shorten that wait (no premature failure-branch
    routing) and the invite must not be re-sent."""
    from linkedin.models import LeadCampaignState
    from linkedin.sequences import executor

    State = LeadCampaignState.State
    campaign, state = _one_connect_state(UserFactory(), fake_session)

    sends = []

    with patch.multiple(
        executor,
        connection_status=lambda *a, **k: "not_connected",
        send_connection_request=lambda *a, **k: sends.append(1),
        _log=_boom,  # bookkeeping fails right after the checkpoint commits
    ):
        executor.run_due_states(fake_session, campaign=campaign)
    state.refresh_from_db()
    assert sends == [1]                                    # invite sent once
    assert state.awaiting_decision is True                 # checkpoint committed before _log
    assert state.state == State.ACTIVE
    assert state.error_count == 1                           # the _log failure was recorded
    # The ~14-day decision wait survived — NOT collapsed to the 15-min backoff.
    assert state.next_action_due_at > timezone.now() + timedelta(days=10)

    # The retry re-enters the idempotent DECISION phase — it does NOT re-send.
    _arm(state)
    with patch.multiple(
        executor,
        send_connection_request=lambda *a, **k: sends.append(1),
        is_connection_accepted=lambda *a, **k: False,
    ):
        executor.run_due_states(fake_session, campaign=campaign)
    assert sends == [1]  # still only one invite


@pytest.mark.django_db
def test_terminal_completion_clears_the_claim_lease(fake_session):
    """A successful advance that COMPLETES the lead nulls next_action_due_at — it
    must not leave the 30-min claim lease as the lead's real schedule. Path:
    already-CONNECTED + include_current_network + no SUCCESS child → _complete."""
    from linkedin.models import Campaign, LeadCampaignState
    from linkedin.sequences import executor
    from linkedin_cli.enums import ProfileState

    campaign, state = _one_connect_state(UserFactory(), fake_session)
    Campaign.objects.filter(pk=campaign.pk).update(include_current_network=True)

    with patch.object(executor, "connection_status", lambda *a, **k: str(ProfileState.CONNECTED)):
        assert executor.run_due_states(fake_session, campaign=campaign) == 1

    state.refresh_from_db()
    assert state.state == LeadCampaignState.State.COMPLETED
    assert state.next_action_due_at is None  # lease overwritten, not lingering


def _auth_boom(*a, **k):
    from linkedin_cli.exceptions import AuthenticationError
    raise AuthenticationError("LinkedIn restriction/checkpoint page: temporarily restricted")


@pytest.mark.django_db
def test_auth_error_propagates_to_pause_account_not_park_lead(fake_session):
    """A 401 / HTTP-200 restriction page mid-step is an ACCOUNT-level failure: it
    must PROPAGATE so the worker auto-pauses the whole account — NOT get swallowed
    into a per-lead STOPPED_ERROR while the worker keeps driving the flagged
    session (the June-9 failure mode). The claimed lead stays ACTIVE to re-run
    after recovery."""
    from linkedin_cli.exceptions import AuthenticationError
    from linkedin.models import LeadCampaignState
    from linkedin.sequences import executor

    State = LeadCampaignState.State
    campaign, state = _one_connect_state(UserFactory(), fake_session)

    with patch.object(executor, "connection_status", _auth_boom):
        with pytest.raises(AuthenticationError):
            executor.run_due_states(fake_session, campaign=campaign)

    state.refresh_from_db()
    assert state.state == State.ACTIVE      # NOT parked as stopped_error
    assert state.error_count == 0           # NOT counted as a transient step failure
    assert state.last_error == ""           # nothing recorded
