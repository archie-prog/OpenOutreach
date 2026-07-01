# linkedin/inbox/poller.py
"""Reply detection (M3).

Polls each active sequence lead's LinkedIn conversation; persists messages as
``Message`` rows (idempotent by ``linkedin_message_id``); and when an inbound
reply has arrived since the lead's last action, sets ``LeadCampaignState`` to
``stopped_reply`` so the sequence never messages a lead who answered.

Reply detection is CONVERSATION-DRIVEN: ``sync_inbox`` lists the real inbox and
matches each conversation to a lead by participant URN, so a message can only ever
land in the owning thread. The send-time guard ``has_new_reply`` reads a single
lead's conversation via ``fetch_thread_messages`` (the mockable boundary), matched
by participant URN only — never LinkedIn's messaging-page navigation fallback,
which captured whatever thread was on screen and caused cross-lead misattribution.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone as _tz

from django.utils import timezone

logger = logging.getLogger(__name__)


def _parse_ts(ts: str):
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%d %H:%M").replace(tzinfo=_tz.utc)
    except (ValueError, TypeError):
        return None


def _synth_id(sender: str, text: str, ts: str) -> str:
    return hashlib.sha1(f"{sender}|{ts}|{text}".encode("utf-8")).hexdigest()


def _self_name(session) -> str:
    p = session.self_profile or {}
    return f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()


def fetch_thread_messages(session, lead) -> list[dict]:
    """Return ``[{linkedin_message_id, direction, body, sent_at}]`` for the lead's
    conversation, matched by PARTICIPANT URN only. Mockable test boundary.

    Never uses linkedin_cli's messaging-page navigation fallback (which captured
    whatever thread the page rendered — the account's most-recent chat — and was
    the cause of cross-lead misattribution). We match the real conversation by
    participant, or fall back to the owner thread's already participant-verified
    ``linkedin_thread_id`` (written only by ``sync_inbox``) — never "what's on screen".
    """
    from linkedin_cli.actions.conversations import find_conversation_urn, parse_messages
    from linkedin_cli.api.client import PlaywrightLinkedinAPI
    from linkedin_cli.api.messaging import fetch_messages

    from linkedin.models import MessageThread

    target_urn = getattr(lead, "urn", None)
    mailbox_urn = (session.self_profile or {}).get("urn")
    if not target_urn or not mailbox_urn or target_urn == mailbox_urn:
        return []

    session.ensure_browser()
    api = PlaywrightLinkedinAPI(session=session)
    conv_urn = find_conversation_urn(api, target_urn, mailbox_urn)
    if not conv_urn:
        # Off the recent-conversations page: use the owner thread's stored,
        # participant-verified conversation id — never a page navigation.
        conv_urn = (
            MessageThread.objects.filter(
                lead=lead, account=session.linkedin_profile, contacted_by_tool=True,
            ).values_list("linkedin_thread_id", flat=True).first()
        ) or ""
    if not conv_urn:
        return []

    me = _self_name(session)
    out = []
    for m in parse_messages(fetch_messages(api, conv_urn) or {}):
        sender = m.get("sender", "")
        ts = m.get("timestamp", "")
        out.append({
            "linkedin_message_id": _synth_id(sender, m.get("text", ""), ts),
            "direction": "out" if sender == me else "in",
            "body": m.get("text", ""),
            "sent_at": _parse_ts(ts),
        })
    return out


# A queued manual reply that keeps failing must not re-drive a live browser send
# every worker cycle forever — give up after this many attempts and surface it.
MAX_MANUAL_SEND_ATTEMPTS = 3


def process_pending_sends(session, account=None) -> int:
    """Send any manual replies queued from the Unibox, from the thread's owning
    account. Manual sends are user-initiated, so they're sent immediately (not
    window-gated) but are *recorded* (ActionLog + daily counter) so they show up
    in the activity feed/KPIs and count toward the account's limits. Retries are
    bounded; a permanently-failing send is marked failed, not retried forever.

    ``account`` scopes the drain to that account's OWN pending messages, so a
    given browser session only ever types into its own logged-in LinkedIn — a
    reply on Josh's thread is sent from Josh's session, never the default account's
    (wrong-identity / cross-account correlation bug). ``None`` = drain all (backstop).
    Returns the number sent this cycle.
    """
    from django.db.models import Q
    from django.utils import timezone

    from linkedin.accounts.limits import record_action
    from linkedin.models import ActionLog, Message
    from linkedin_cli.actions.message import send_raw_message

    sent = 0
    pending = Message.objects.filter(pending_send=True).select_related("thread__lead", "thread__account")
    if account is not None:
        pending = pending.filter(Q(sender_account=account) | Q(sender_account__isnull=True, thread__account=account))
    for m in pending:
        lead = m.thread.lead
        account = m.sender_account or m.thread.account
        urn = lead.urn
        if not urn:
            try:
                urn = lead.get_urn(session)
            except Exception:
                urn = ""
        m.send_attempts = (m.send_attempts or 0) + 1
        ok = False
        err = ""
        try:
            ok = bool(send_raw_message(
                session,
                {"public_identifier": lead.public_identifier, "url": lead.linkedin_url, "urn": urn or ""},
                m.body,
            ))
        except Exception as exc:
            err = repr(exc)[:300]
            logger.exception("Manual send failed for message %s", m.pk)

        if ok:
            m.pending_send = False
            m.sent_at = timezone.now()
            m.send_error = ""
            m.save(update_fields=["pending_send", "sent_at", "send_attempts", "send_error"])
            # Record so the send appears in the activity feed/KPIs and counts
            # toward the daily message cap (the executor's automated sends do the
            # same via _log). A campaign is needed for ActionLog; use the lead's
            # most recent campaign state if any.
            campaign = _lead_campaign(lead)
            if campaign is not None:
                ActionLog.objects.create(
                    linkedin_profile=account, campaign=campaign, lead=lead,
                    action_type=ActionLog.ActionType.MESSAGE,
                )
            record_action(account, "message")
            sent += 1
        else:
            m.send_error = err or "send returned no confirmation"
            if m.send_attempts >= MAX_MANUAL_SEND_ATTEMPTS:
                m.pending_send = False  # give up — UI shows it as failed
                logger.warning("Manual send %s gave up after %d attempts", m.pk, m.send_attempts)
            m.save(update_fields=["pending_send", "send_attempts", "send_error"])
    return sent


def _lead_campaign(lead):
    """The campaign to attribute a manual send to — the lead's most recent
    campaign state, if any (manual replies aren't tied to a single campaign)."""
    state = lead.campaign_states.order_by("-created_at").select_related("campaign").first()
    return state.campaign if state else None


class ReplyCheckUnverified(Exception):
    """The send-time reply check could not confirm the lead hasn't replied, so the
    caller must NOT send. Raised (instead of failing open) so the executor defers +
    retries the step; a warning is surfaced so a held follow-up is never silent."""


def _safety_hold_warning(state, reason):
    """A follow-up is being HELD because we couldn't verify no-reply — log it and
    fire a Slack warning so it's visible, never silently stuck."""
    lead = state.lead
    name = ((getattr(lead, "first_name", "") or "") + " " + (getattr(lead, "last_name", "") or "")).strip() or f"lead {state.lead_id}"
    logger.warning("SAFETY HOLD: not messaging %s (lead %s) — %s", name, state.lead_id, reason)
    try:
        from linkedin.notify.slack import post_text
        post_text(f":warning: Follow-up HELD for *{name}* — couldn't confirm they haven't replied ({reason}). Holding + retrying; check the conversation.")
    except Exception:
        pass


def has_new_reply(session, state) -> bool:
    """Live send-time guard: True if the lead replied since our last action.

    FAIL-CLOSED (the "only send if we know FOR SURE they haven't replied" rule):
    if we cannot actually read the conversation — a fetch error, or an empty result
    for a lead we've already messaged (missing/stale URN) — we do NOT know they
    haven't replied, so we raise ``ReplyCheckUnverified`` to make the caller hold +
    retry rather than risk messaging someone who already replied (and surface a
    warning). A clean read with only outbound messages returns False (safe)."""
    if not state.last_action_at:
        return False
    try:
        messages = fetch_thread_messages(session, state.lead)
    except Exception as exc:
        _safety_hold_warning(state, f"reply-check fetch error: {exc!r}")
        raise ReplyCheckUnverified(state.lead_id) from exc
    if not messages:
        from linkedin.models import MessageThread
        # "Have we messaged them?" keys on the contacted_by_tool thread written at
        # send time by _mark_contacted — NOT a re-read message row the sender never
        # persists — so a follow-up whose conversation we can't read now fails CLOSED
        # (holds). At the FIRST message step no contacted thread exists yet (only an
        # invite was sent), so an empty read is safe → send.
        sent_before = MessageThread.objects.filter(
            lead=state.lead, contacted_by_tool=True,
        ).exists()
        if not sent_before:
            return False
        _safety_hold_warning(state, "reply-check returned no messages for a contacted lead")
        raise ReplyCheckUnverified(state.lead_id)
    for m in messages:
        if m["direction"] == "in" and m["sent_at"] and m["sent_at"] > state.last_action_at:
            return True
    return False


def sync_inbox(session, limit=40) -> int:
    """Refresh THIS account's KIT-CONTACTED threads — smallest detection surface.

    ONE ``fetch_conversations`` lists the recent inbox (a reply bumps a conversation
    to the top, so a new reply is always in this list); each is ingested into its
    OWNING (participant-matched) thread, so a message can only ever land in the right
    lead's thread. One bad/legacy conversation is skipped, not fatal. Returns the
    number of threads that gained new messages.
    """
    from linkedin_cli.api.client import PlaywrightLinkedinAPI
    from linkedin_cli.api.messaging import fetch_conversations

    try:
        from linkedin_cli.exceptions import AuthenticationError
    except Exception:  # pragma: no cover
        class AuthenticationError(Exception):
            pass

    mailbox_urn = (session.self_profile or {}).get("urn")
    if not mailbox_urn:
        logger.warning("sync_inbox: no mailbox urn on session; skipping")
        return 0

    session.ensure_browser()
    api = PlaywrightLinkedinAPI(session=session)
    raw = fetch_conversations(api, mailbox_urn) or {}
    elements = (
        raw.get("data", {})
        .get("messengerConversationsBySyncToken", {})
        .get("elements", [])
    )
    account = session.linkedin_profile
    updated = 0
    for conv in elements[:limit]:
        try:
            if _ingest_conversation(session, api, account, mailbox_urn, conv):
                updated += 1
        except AuthenticationError:
            raise  # account-level (401/checkpoint) → let the worker auto-pause
        except Exception:
            # Legacy/malformed data on one conversation must not abort the whole
            # sync (the live DB carries pre-fix duplicates). Log it and move on.
            logger.exception("sync_inbox: skipping a conversation after error")
    return updated


def _ingest_conversation(session, api, account, mailbox_urn, conv) -> bool:
    """Ingest ONE recent conversation into its owning, participant-matched thread;
    stop the owner's ACTIVE sequence + Slack-notify on a genuine inbound reply.
    Returns True if the thread gained a new message."""
    from crm.models import Lead
    from linkedin.models import LeadCampaignState, Message, MessageThread
    from linkedin_cli.actions.conversations import parse_messages
    from linkedin_cli.api.messaging import fetch_messages

    conv_urn = conv.get("entityUrn")
    if not conv_urn:
        return False
    part_urn = ""
    for p in conv.get("conversationParticipants", []):
        u = p.get("hostIdentityUrn", "")
        if u and u != mailbox_urn:
            part_urn = u
            break
    if not part_urn:
        return False
    lead = Lead.objects.filter(urn=part_urn).first()
    if not lead:
        return False
    # ONLY threads THIS kit contacted — never organic / HeyReach.
    thread = MessageThread.objects.filter(
        account=account, lead=lead, contacted_by_tool=True,
    ).first()
    if not thread:
        return False
    if not thread.linkedin_thread_id:
        thread.linkedin_thread_id = conv_urn

    me = _self_name(session)
    # A lead is enrolled in at most one live sequence, so its states share one last
    # action; a reply after it stops the lead's ACTIVE state(s).
    states = list(LeadCampaignState.objects.filter(lead=lead))
    last_action = max((s.last_action_at for s in states if s.last_action_at), default=None)
    newest_reply = None  # (body, created) of the newest inbound after our last action

    latest = thread.last_message_at
    gained = False
    for m in parse_messages(fetch_messages(api, conv_urn) or {}):
        sender = m.get("sender", "")
        raw_ts = m.get("timestamp", "")
        ts = _parse_ts(raw_ts)
        direction = "out" if sender == me else "in"
        synth = _synth_id(sender, m.get("text", ""), raw_ts)
        # Reconcile a manual Unibox send: our own reply comes back with a synth id
        # differing from the "manual-…" id stored at queue time — re-key it so the
        # get_or_create matches. Skip if the real id is already stored (legacy
        # duplicate), else the re-key trips the (thread, id) unique constraint.
        if direction == "out":
            manual = Message.objects.filter(
                thread=thread, direction="out", sent_via_tool=True, body=m.get("text", ""),
                linkedin_message_id__startswith="manual-",
            ).first()
            if (manual is not None and manual.linkedin_message_id != synth
                    and not Message.objects.filter(thread=thread, linkedin_message_id=synth).exists()):
                manual.linkedin_message_id = synth
                if ts:
                    manual.sent_at = ts
                manual.save(update_fields=["linkedin_message_id", "sent_at"])
        _obj, created = Message.objects.get_or_create(
            thread=thread,
            linkedin_message_id=synth,
            defaults={
                "direction": direction,
                "body": m.get("text", ""),
                "sent_at": ts,
                "sender_account": account if direction == "out" else None,
            },
        )
        if created:
            gained = True
        if ts and (latest is None or ts > latest):
            latest = ts
        if direction == "in":
            thread.has_inbound_reply = True
            if last_action and ts and ts > last_action:
                newest_reply = (m.get("text", ""), created)

    thread.last_message_at = latest
    thread.last_polled_at = timezone.now()
    thread.save()

    # A genuine inbound after our last action stops the sequence (misattribution-
    # proof: it reached the owner thread) and pings Slack once for a new reply.
    if newest_reply is not None:
        for s in states:
            if s.state == LeadCampaignState.State.ACTIVE:
                s.state = LeadCampaignState.State.STOPPED_REPLY
                s.save(update_fields=["state"])
                logger.info("Lead %s replied — sequence stopped", lead.id)
        if newest_reply[1]:  # created → not seen before
            _notify_reply(lead, newest_reply[0], session)
    return gained


def _notify_reply(lead, body, session):
    """Fire a Slack reply notification (never raises into the poll loop)."""
    try:
        from linkedin.notify.slack import notify_reply

        name = f"{lead.first_name or ''} {lead.last_name or ''}".strip() or (lead.public_identifier or "lead")
        account = getattr(session.linkedin_profile, "linkedin_username", "") or ""
        notify_reply(name, body, lead.linkedin_url, account)
    except Exception as exc:
        logger.warning("Slack reply notify failed: %r", exc)
