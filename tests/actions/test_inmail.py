# tests/actions/test_inmail.py
"""InMail action (M4) — browser composer mocked."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from django.utils import timezone

from tests.factories import UserFactory


def _lead():
    from crm.models import Lead

    return Lead.objects.create(linkedin_url="https://www.linkedin.com/in/x/", public_identifier="x")


def _set_inmail(fake_session, value):
    fake_session.linkedin_profile.has_inmail = value
    fake_session.linkedin_profile.save(update_fields=["has_inmail"])


@pytest.mark.django_db
class TestSendInmail:
    def test_skips_cleanly_without_capability(self, fake_session):
        from linkedin.actions.inmail import send_inmail

        _set_inmail(fake_session, False)
        result = send_inmail(fake_session, _lead(), "subj", "body")
        assert result["skipped"] is True
        assert result["success"] is False
        assert result["error"] == "no_inmail"

    def test_success_when_available(self, fake_session):
        from linkedin.actions import inmail

        _set_inmail(fake_session, True)
        with patch.object(inmail, "_compose_inmail", return_value="msg-1"):
            result = inmail.send_inmail(fake_session, _lead(), "subj", "body")
        assert result["success"] is True
        assert result["linkedin_message_id"] == "msg-1"

    def test_ui_failure_does_not_crash(self, fake_session):
        from linkedin.actions import inmail

        _set_inmail(fake_session, True)
        with patch.object(inmail, "_compose_inmail", side_effect=RuntimeError("boom")):
            result = inmail.send_inmail(fake_session, _lead(), "subj", "body")
        assert result["success"] is False
        assert result["skipped"] is False
        assert "boom" in result["error"]


@pytest.mark.django_db
class TestInmailInSequence:
    def test_inmail_branch_skips_cleanly_without_capability(self, fake_session):
        """not-accepted → InMail branch fires; no capability → skip, sequence still completes."""
        from crm.models import Lead
        from linkedin.models import ActionLog, Campaign, LeadCampaignState, LeadList
        from linkedin.models import SequenceStep as S
        from linkedin.models import Sequence
        from linkedin.sequences import executor

        _set_inmail(fake_session, False)
        owner = UserFactory()
        seq = Sequence.objects.create(name="x", owner=owner)
        connect = S.objects.create(sequence=seq, branch=S.Branch.ROOT, step_type=S.StepType.CONNECT)
        S.objects.create(sequence=seq, parent=connect, branch=S.Branch.FAILURE, step_type=S.StepType.INMAIL, config={"body": "b"})
        ll = LeadList.objects.create(name="L", owner=owner, source_type=LeadList.SourceType.CSV)
        Lead.objects.create(linkedin_url="https://www.linkedin.com/in/p0/", public_identifier="p0", lead_list=ll)
        campaign = Campaign.objects.create(name="C", sequence=seq, lead_list=ll, status=Campaign.Status.ACTIVE)
        campaign.users.add(fake_session.django_user)
        executor.enroll_campaign(campaign)

        with patch.multiple(
            executor,
            connection_status=lambda *a, **k: "not_connected",
            send_connection_request=lambda *a, **k: None,
            is_connection_accepted=lambda *a, **k: False,
        ):
            for _ in range(6):
                active = LeadCampaignState.objects.filter(campaign=campaign, state=LeadCampaignState.State.ACTIVE)
                if not active.exists():
                    break
                active.update(next_action_due_at=timezone.now())
                executor.run_due_states(fake_session, campaign=campaign)

        states = LeadCampaignState.objects.filter(campaign=campaign)
        assert all(s.state == LeadCampaignState.State.COMPLETED for s in states)
        # Skipped, so no InMail action logged — and crucially, no crash.
        assert ActionLog.objects.filter(action_type="inmail").count() == 0
