# tests/tasks/test_overhaul_fixes.py
"""Regression tests for the hardening pass — these lock in fixes for bugs the
old suite never exercised (it mocked exactly the failing boundary)."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from django.utils import timezone

from tests.factories import UserFactory


# ── accounts/limits: cap_for must never silently return 0 for a known action ──


@pytest.mark.django_db
def test_cap_for_falls_back_to_default_when_key_missing(fake_session):
    """A daily_caps_json saved before it carried inmail/profile_visit/like_post
    must not zero those caps (which stalls the step forever)."""
    from linkedin.accounts.limits import cap_for

    acct = fake_session.linkedin_profile
    # Simulate a truncated dict written by the old save path.
    acct.daily_caps_json = {"connect": 25, "message": 50}
    acct.save(update_fields=["daily_caps_json"])

    assert cap_for(acct, "connect") == 25
    assert cap_for(acct, "message") == 50
    # These were dropped by the old UI save — must fall back to defaults, not 0.
    assert cap_for(acct, "inmail") > 0
    assert cap_for(acct, "profile_visit") > 0
    assert cap_for(acct, "like_post") > 0


# ── accounts/limits: send_end_hour=24 must not crash next_send_time ──


@pytest.mark.django_db
def test_send_end_hour_24_does_not_crash(fake_session):
    from linkedin.accounts.limits import is_send_time, next_send_time

    acct = fake_session.linkedin_profile
    acct.send_start_hour = 9
    acct.send_end_hour = 24  # "until midnight" — the UI allows this
    acct.send_weekdays = [0, 1, 2, 3, 4, 5, 6]
    acct.save(update_fields=["send_start_hour", "send_end_hour", "send_weekdays"])

    # Previously raised ValueError (replace(hour=24)); now returns a datetime.
    nxt = next_send_time(acct)
    assert nxt is not None
    # is_send_time tolerates the 24 bound too.
    assert isinstance(is_send_time(acct), bool)


# ── executor.render_template: both brace styles, unknown tags left literal ──


def test_render_template_double_braces_and_unknown_tags():
    from linkedin.sequences.executor import render_template

    ctx = {"first_name": "Jane", "company": "Acme"}
    # HeyReach-style single brace.
    assert render_template("Hi {first_name}", ctx) == "Hi Jane"
    # Jinja double brace must resolve (not render the literal "{Jane}").
    assert render_template("Hi {{ first_name }}", ctx) == "Hi Jane"
    # Unknown single-brace tag is left verbatim, not silently blanked.
    assert render_template("Hi {first_name} at {title}", ctx) == "Hi Jane at {title}"
    # Empty render falls back.
    assert render_template("{unknown_only_removed}", {}, fallback="Hey") == "Hey" or \
        render_template("", {}, fallback="Hey") == "Hey"


# ── executor: awaiting_decision is persisted False after the connect decision ──


def _connect_then_message(owner):
    from linkedin.models import Sequence, SequenceStep as S

    seq = Sequence.objects.create(name="C", owner=owner)
    connect = S.objects.create(
        sequence=seq, branch=S.Branch.ROOT, step_type=S.StepType.CONNECT,
        config={"wait_days_before_branch_decision": 14},
    )
    S.objects.create(sequence=seq, parent=connect, branch=S.Branch.SUCCESS,
                     step_type=S.StepType.MESSAGE, config={"template": "hi", "fallback": "hi"})
    return seq


@pytest.mark.django_db
def test_awaiting_decision_persisted_false_after_decision(fake_session):
    from crm.models import Lead
    from linkedin.models import Campaign, LeadCampaignState, LeadList
    from linkedin.sequences import executor

    owner = fake_session.django_user
    seq = _connect_then_message(owner)
    ll = LeadList.objects.create(name="L", owner=owner, source_type=LeadList.SourceType.CSV)
    Lead.objects.create(linkedin_url="https://www.linkedin.com/in/x/", public_identifier="x", lead_list=ll)
    campaign = Campaign.objects.create(name="Seq", sequence=seq, lead_list=ll, status=Campaign.Status.ACTIVE)
    campaign.users.add(owner)
    executor.enroll_campaign(campaign)

    with patch.multiple(
        executor,
        send_connection_request=lambda *a, **k: None,
        is_connection_accepted=lambda *a, **k: True,
        send_message=lambda *a, **k: None,
    ):
        # Phase 1: sends connect, sets awaiting_decision True.
        executor.run_due_states(fake_session, campaign=campaign)
        st = LeadCampaignState.objects.get(campaign=campaign)
        assert st.awaiting_decision is True

        # Phase 2: decision — must persist awaiting_decision False to the DB.
        st.next_action_due_at = timezone.now()
        st.save(update_fields=["next_action_due_at"])
        executor.run_due_states(fake_session, campaign=campaign)
        st.refresh_from_db()
        assert st.awaiting_decision is False  # the bug left this True forever


# ── executor: due_states only runs ACTIVE campaigns ──


@pytest.mark.django_db
def test_due_states_skips_non_active_campaign(fake_session):
    from crm.models import Lead
    from linkedin.models import Campaign, LeadCampaignState, LeadList
    from linkedin.sequences import executor

    owner = fake_session.django_user
    seq = _connect_then_message(owner)
    ll = LeadList.objects.create(name="L", owner=owner, source_type=LeadList.SourceType.CSV)
    Lead.objects.create(linkedin_url="https://www.linkedin.com/in/y/", public_identifier="y", lead_list=ll)
    campaign = Campaign.objects.create(name="Paused", sequence=seq, lead_list=ll, status=Campaign.Status.ACTIVE)
    campaign.users.add(owner)
    executor.enroll_campaign(campaign)
    # Pause it but leave an ACTIVE, due lead state (as add-leads-after-pause would).
    Campaign.objects.filter(pk=campaign.pk).update(status=Campaign.Status.PAUSED)
    LeadCampaignState.objects.filter(campaign=campaign).update(
        state=LeadCampaignState.State.ACTIVE, next_action_due_at=timezone.now(),
    )

    sent = {"n": 0}
    def _count(*a, **k):
        sent["n"] += 1
    with patch.multiple(executor, send_connection_request=_count, is_connection_accepted=lambda *a, **k: False):
        executor.run_due_states(fake_session, campaign=campaign)
    assert sent["n"] == 0  # paused campaign must not send
