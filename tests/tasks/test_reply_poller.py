# tests/tasks/test_reply_poller.py
"""Reply detection — conversation-driven ``sync_inbox`` + fail-closed ``has_new_reply``.

The lead-driven ``poll_replies`` + linkedin_cli navigation fallback were removed
(they captured whatever thread was on screen and misattributed one lead's messages
to others). ``sync_inbox`` matches the real conversation participant, so a message
can only ever land in the owning thread.
"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone


def _lead(urn="urn:li:fsd_profile:LEAD1", pid="lead1"):
    from crm.models import Lead

    return Lead.objects.create(
        linkedin_url=f"https://www.linkedin.com/in/{pid}/", public_identifier=pid, urn=urn,
    )


def _state(fake_session, lead, last_action_minutes_ago=60, state=None):
    from linkedin.models import Campaign, LeadCampaignState

    campaign = Campaign.objects.first() or Campaign.objects.create(name="C")
    return LeadCampaignState.objects.create(
        lead=lead, campaign=campaign,
        sending_account=fake_session.linkedin_profile,
        state=state or LeadCampaignState.State.ACTIVE,
        last_action_at=timezone.now() - timedelta(minutes=last_action_minutes_ago),
    )


def _contacted_thread(fake_session, lead, conv_urn=""):
    from linkedin.models import MessageThread

    return MessageThread.objects.create(
        lead=lead, account=fake_session.linkedin_profile,
        contacted_by_tool=True, linkedin_thread_id=conv_urn,
    )


def _msg(text, minutes_ago=0, sender="Someone Else"):
    ts = (timezone.now() - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M")
    return {"sender": sender, "text": text, "timestamp": ts}


def _run_sync(fake_session, lead, conv_urn, messages):
    """Drive ``sync_inbox`` with the linkedin_cli boundary mocked. ``messages`` is
    what ``parse_messages`` returns: ``[{sender, text, timestamp}]``."""
    from linkedin.inbox import poller

    convs = {"data": {"messengerConversationsBySyncToken": {"elements": [
        {"entityUrn": conv_urn, "conversationParticipants": [
            {"hostIdentityUrn": fake_session.self_profile["urn"]},
            {"hostIdentityUrn": lead.urn},
        ]},
    ]}}}
    with patch("linkedin_cli.api.client.PlaywrightLinkedinAPI", return_value=object()), \
         patch("linkedin_cli.api.messaging.fetch_conversations", return_value=convs), \
         patch("linkedin_cli.api.messaging.fetch_messages", return_value={}), \
         patch("linkedin_cli.actions.conversations.parse_messages", return_value=messages), \
         patch("linkedin.notify.slack.notify_reply"):
        return poller.sync_inbox(fake_session)


@pytest.mark.django_db
class TestSyncInboxReplyStop:
    def test_inbound_reply_stops_owner_sequence(self, fake_session):
        from linkedin.models import LeadCampaignState, Message

        lead = _lead()
        state = _state(fake_session, lead)
        _contacted_thread(fake_session, lead, "urn:li:msg:CONV1")
        _run_sync(fake_session, lead, "urn:li:msg:CONV1", [_msg("thanks!", minutes_ago=1)])

        state.refresh_from_db()
        assert state.state == LeadCampaignState.State.STOPPED_REPLY
        assert Message.objects.filter(direction="in").count() == 1

    def test_outbound_only_does_not_stop(self, fake_session):
        from linkedin.models import LeadCampaignState

        lead = _lead()
        state = _state(fake_session, lead)
        _contacted_thread(fake_session, lead, "urn:li:msg:CONV1")
        _run_sync(fake_session, lead, "urn:li:msg:CONV1",
                  [_msg("hi", minutes_ago=1, sender="Diego Ramirez")])

        state.refresh_from_db()
        assert state.state == LeadCampaignState.State.ACTIVE

    def test_reply_older_than_last_action_ignored(self, fake_session):
        from linkedin.models import LeadCampaignState

        lead = _lead()
        state = _state(fake_session, lead, last_action_minutes_ago=0)
        _contacted_thread(fake_session, lead, "urn:li:msg:CONV1")
        _run_sync(fake_session, lead, "urn:li:msg:CONV1", [_msg("earlier", minutes_ago=120)])

        state.refresh_from_db()
        assert state.state == LeadCampaignState.State.ACTIVE

    def test_idempotent_no_duplicate_messages(self, fake_session):
        from linkedin.models import Message

        lead = _lead()
        _state(fake_session, lead)
        _contacted_thread(fake_session, lead, "urn:li:msg:CONV1")
        msgs = [_msg("hi", minutes_ago=1)]
        _run_sync(fake_session, lead, "urn:li:msg:CONV1", msgs)
        _run_sync(fake_session, lead, "urn:li:msg:CONV1", msgs)

        assert Message.objects.filter(direction="in").count() == 1

    def test_no_contacted_thread_skips_no_false_stop(self, fake_session):
        # Connected-but-never-messaged (the Francesco case): participant matches but
        # there is no contacted_by_tool thread → nothing ingested, no false stop.
        from linkedin.models import LeadCampaignState, Message

        lead = _lead()
        state = _state(fake_session, lead)
        updated = _run_sync(fake_session, lead, "urn:li:msg:CONV1", [_msg("thanks!", minutes_ago=1)])

        state.refresh_from_db()
        assert state.state == LeadCampaignState.State.ACTIVE
        assert Message.objects.count() == 0
        assert updated == 0

    def test_manual_send_rekeyed_not_duplicated(self, fake_session):
        from linkedin.models import Message

        lead = _lead()
        _state(fake_session, lead)
        thread = _contacted_thread(fake_session, lead, "urn:li:msg:CONV1")
        Message.objects.create(
            thread=thread, direction="out", body="my reply", sent_via_tool=True,
            linkedin_message_id="manual-abc",
        )
        _run_sync(fake_session, lead, "urn:li:msg:CONV1",
                  [_msg("my reply", minutes_ago=1, sender="Diego Ramirez")])

        assert Message.objects.filter(thread=thread, direction="out").count() == 1
        assert not Message.objects.filter(linkedin_message_id="manual-abc").exists()

    def test_rekey_skipped_when_synth_already_exists(self, fake_session):
        # Legacy data: a thread already holds BOTH a manual- row and its synth twin.
        # Re-keying would hit the (thread, id) unique constraint — it must be skipped,
        # not crash the sync. Exercise the ingest path directly (no wrapper to mask).
        from linkedin.inbox import poller
        from linkedin.models import Message

        lead = _lead()
        _state(fake_session, lead)
        thread = _contacted_thread(fake_session, lead, "urn:li:msg:CONV1")
        msg = _msg("dup reply", minutes_ago=1, sender="Diego Ramirez")
        synth = poller._synth_id("Diego Ramirez", "dup reply", msg["timestamp"])
        Message.objects.create(thread=thread, direction="out", body="dup reply",
                               sent_via_tool=True, linkedin_message_id="manual-xyz")
        Message.objects.create(thread=thread, direction="out", body="dup reply",
                               linkedin_message_id=synth)
        conv = {"entityUrn": "urn:li:msg:CONV1", "conversationParticipants": [
            {"hostIdentityUrn": fake_session.self_profile["urn"]},
            {"hostIdentityUrn": lead.urn}]}
        with patch("linkedin_cli.api.messaging.fetch_messages", return_value={}), \
             patch("linkedin_cli.actions.conversations.parse_messages", return_value=[msg]):
            poller._ingest_conversation(
                fake_session, object(), fake_session.linkedin_profile,
                fake_session.self_profile["urn"], conv)

        assert Message.objects.filter(thread=thread, direction="out").count() == 2


@pytest.mark.django_db
class TestHasNewReplyFailClosed:
    def test_contacted_lead_empty_read_holds(self, fake_session):
        from linkedin.inbox import poller

        lead = _lead()
        state = _state(fake_session, lead)
        _contacted_thread(fake_session, lead)  # contacted_by_tool exists → follow-up
        with patch.object(poller, "fetch_thread_messages", return_value=[]):
            with pytest.raises(poller.ReplyCheckUnverified):
                poller.has_new_reply(fake_session, state)

    def test_never_messaged_empty_read_sends(self, fake_session):
        from linkedin.inbox import poller

        lead = _lead()
        state = _state(fake_session, lead)  # connected (last_action) but no message thread
        with patch.object(poller, "fetch_thread_messages", return_value=[]):
            assert poller.has_new_reply(fake_session, state) is False

    def test_inbound_after_action_returns_true(self, fake_session):
        from linkedin.inbox import poller

        lead = _lead()
        state = _state(fake_session, lead)
        msgs = [{"linkedin_message_id": "m1", "direction": "in", "body": "hi",
                 "sent_at": timezone.now()}]
        with patch.object(poller, "fetch_thread_messages", return_value=msgs):
            assert poller.has_new_reply(fake_session, state) is True


@pytest.mark.django_db
class TestReplyCheckDefer:
    def test_unverified_reply_check_holds_not_parks(self, fake_session):
        # A send-time reply check that can't verify must HOLD + retry, never accrue
        # error_count toward the terminal STOPPED_ERROR park.
        from linkedin.inbox.poller import ReplyCheckUnverified
        from linkedin.models import LeadCampaignState
        from linkedin.sequences import executor

        lead = _lead()
        state = _state(fake_session, lead)
        with patch.object(executor, "advance_state", side_effect=ReplyCheckUnverified(state.lead_id)):
            for _ in range(executor.MAX_STEP_ATTEMPTS + 2):
                LeadCampaignState.objects.filter(pk=state.pk).update(
                    next_action_due_at=timezone.now(), state=LeadCampaignState.State.ACTIVE)
                executor._run_one(fake_session, state)

        state.refresh_from_db()
        assert state.state == LeadCampaignState.State.ACTIVE
        assert (state.error_count or 0) == 0
