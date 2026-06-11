# tests/tasks/test_sequence_executor.py
"""Sequence executor walkthrough (M2) — browser actions mocked."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from django.utils import timezone

from tests.factories import UserFactory


def _build_sequence(owner):
    """Connect → (accepted: Wait → Message → Wait → Message) / (not-accepted: InMail)."""
    from linkedin.models import Sequence, SequenceStep as S

    seq = Sequence.objects.create(name="Default", owner=owner)
    connect = S.objects.create(
        sequence=seq, branch=S.Branch.ROOT, step_type=S.StepType.CONNECT,
        config={"wait_days_before_branch_decision": 14, "personalised_note": "Hi", "fallback_note": "Hi"},
    )
    wait1 = S.objects.create(sequence=seq, parent=connect, branch=S.Branch.SUCCESS, step_type=S.StepType.WAIT, config={"days": 2})
    msg1 = S.objects.create(sequence=seq, parent=wait1, branch=S.Branch.SUCCESS, step_type=S.StepType.MESSAGE, config={"template": "hi", "fallback": "hi"})
    wait2 = S.objects.create(sequence=seq, parent=msg1, branch=S.Branch.FAILURE, step_type=S.StepType.WAIT, config={"days": 2})
    S.objects.create(sequence=seq, parent=wait2, branch=S.Branch.SUCCESS, step_type=S.StepType.MESSAGE, config={"template": "f", "fallback": "f"})
    S.objects.create(sequence=seq, parent=connect, branch=S.Branch.FAILURE, step_type=S.StepType.INMAIL, config={"subject": "s", "body": "b"})
    return seq


def _campaign_with_leads(owner, fake_session, seq, n=3):
    from crm.models import Lead
    from linkedin.models import Campaign, LeadList

    ll = LeadList.objects.create(name="L", owner=owner, source_type=LeadList.SourceType.CSV)
    for i in range(n):
        Lead.objects.create(linkedin_url=f"https://www.linkedin.com/in/p{i}/", public_identifier=f"p{i}", lead_list=ll)
    campaign = Campaign.objects.create(name="Seq Campaign", sequence=seq, lead_list=ll, status=Campaign.Status.ACTIVE)
    campaign.users.add(fake_session.django_user)
    return campaign


def _drive(executor, fake_session, campaign, rounds=15):
    """Force every active state due each round and run, until all settle."""
    from linkedin.models import LeadCampaignState

    for _ in range(rounds):
        active = LeadCampaignState.objects.filter(campaign=campaign, state=LeadCampaignState.State.ACTIVE)
        if not active.exists():
            break
        active.update(next_action_due_at=timezone.now())
        executor.run_due_states(fake_session, campaign=campaign)


@pytest.mark.django_db
class TestSequenceExecutor:
    def test_enroll_creates_one_state_per_lead(self, fake_session):
        from linkedin.models import LeadCampaignState
        from linkedin.sequences import executor

        owner = UserFactory()
        seq = _build_sequence(owner)
        campaign = _campaign_with_leads(owner, fake_session, seq, n=3)
        assert executor.enroll_campaign(campaign)["enrolled"] == 3
        # Idempotent.
        assert executor.enroll_campaign(campaign)["enrolled"] == 0
        assert LeadCampaignState.objects.filter(campaign=campaign).count() == 3

    def test_accepted_branch_walkthrough(self, fake_session):
        from linkedin.models import ActionLog, LeadCampaignState
        from linkedin.sequences import executor

        owner = UserFactory()
        campaign = _campaign_with_leads(owner, fake_session, _build_sequence(owner), n=3)
        executor.enroll_campaign(campaign)

        with patch.multiple(
            executor,
            connection_status=lambda *a, **k: "not_connected",
            send_connection_request=lambda *a, **k: None,
            is_connection_accepted=lambda *a, **k: True,
            send_message=lambda *a, **k: None,
            send_inmail=lambda *a, **k: {"success": True},
        ):
            _drive(executor, fake_session, campaign)

        states = LeadCampaignState.objects.filter(campaign=campaign)
        assert all(s.state == LeadCampaignState.State.COMPLETED for s in states)
        assert ActionLog.objects.filter(action_type="connect").count() == 3
        assert ActionLog.objects.filter(action_type="message").count() == 6
        assert ActionLog.objects.filter(action_type="inmail").count() == 0

    def test_not_accepted_routes_to_inmail(self, fake_session):
        from linkedin.models import ActionLog, LeadCampaignState
        from linkedin.sequences import executor

        owner = UserFactory()
        campaign = _campaign_with_leads(owner, fake_session, _build_sequence(owner), n=3)
        executor.enroll_campaign(campaign)

        with patch.multiple(
            executor,
            connection_status=lambda *a, **k: "not_connected",
            send_connection_request=lambda *a, **k: None,
            is_connection_accepted=lambda *a, **k: False,
            send_message=lambda *a, **k: None,
            send_inmail=lambda *a, **k: {"success": True},
        ):
            _drive(executor, fake_session, campaign)

        states = LeadCampaignState.objects.filter(campaign=campaign)
        assert all(s.state == LeadCampaignState.State.COMPLETED for s in states)
        assert ActionLog.objects.filter(action_type="inmail").count() == 3
        assert ActionLog.objects.filter(action_type="message").count() == 0
