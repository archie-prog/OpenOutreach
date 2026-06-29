# linkedin/inbox/poller.py
"""Reply detection (M3).

Polls each active sequence lead's LinkedIn conversation; persists messages as
``Message`` rows (idempotent by ``linkedin_message_id``); and when an inbound
reply has arrived since the lead's last action, sets ``LeadCampaignState`` to
``stopped_reply`` so the sequence never messages a lead who answered.

``fetch_thread_messages`` is the single mockable boundary over ``linkedin_cli``.
NOTE: ``get_conversation`` returns only ``{sender, text, timestamp}`` — it drops
the Voyager entityUrn — so direction is inferred by sender name and the message
id is synthesised from a content hash. Refining this needs a ``linkedin_cli``
change (tracked for the M4-era cli fork).
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
    conversation. Mocked in tests; thin wrapper over ``linkedin_cli`` in prod.
    """
    from linkedin_cli.actions.conversations import get_conversation

    target_urn = getattr(lead, "urn", None)
    mailbox_urn = (session.self_profile or {}).get("urn")
    if not target_urn or not mailbox_urn:
        return []

    convo = get_conversation(session, target_urn, mailbox_urn) or []
    me = _self_name(session)
    out = []
    for m in convo:
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


def process_pending_sends(session) -> int:
    """Send any manual replies queued from the Unibox, from the thread's owning
    account. Manual sends are user-initiated, so they're sent immediately (not
    window-gated) but are *recorded* (ActionLog + daily counter) so they show up
    in the activity feed/KPIs and count toward the account's limits. Retries are
    bounded; a permanently-failing send is marked failed, not retried forever.
    Returns the number sent this cycle.
    """
    from django.utils import timezone

    from linkedin.accounts.limits import record_action
    from linkedin.models import ActionLog, Message
    from linkedin_cli.actions.message import send_raw_message

    sent = 0
    pending = Message.objects.filter(pending_send=True).select_related("thread__lead", "thread__account")
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
        # We've already messaged this lead, so the conversation must contain our
        # outbound. An empty result means we couldn't read it — hold, don't send.
        _safety_hold_warning(state, "reply-check returned no messages for a contacted lead")
        raise ReplyCheckUnverified(state.lead_id)
    for m in messages:
        if m["direction"] == "in" and m["sent_at"] and m["sent_at"] > state.last_action_at:
            return True
    return False


def poll_replies(session, campaign=None, limit=None) -> int:
    """Poll active sequence leads; persist messages; stop any that replied.
    Returns the number of states transitioned to ``stopped_reply``.

    ``limit`` bounds how many leads are scanned per call (each scan is a live
    Voyager conversation fetch) — the worker passes a small bound and orders by
    most-recent activity so each cycle stays fast and coverage rotates.
    """
    from linkedin.models import LeadCampaignState, Message, MessageThread

    State = LeadCampaignState.State
    # Poll active leads (to stop on reply) AND finished/paused ones (so the
    # unibox keeps syncing replies that arrive after a sequence completes).
    pollable = [State.ACTIVE, State.COMPLETED, State.STOPPED_REPLY, State.PAUSED_MANUAL]
    qs = LeadCampaignState.objects.filter(state__in=pollable)
    if campaign is not None:
        qs = qs.filter(campaign=campaign)
    qs = qs.select_related("lead", "campaign")
    if limit:
        # Rotate coverage by least-recently-polled so EVERY pollable lead is
        # eventually scanned — not just the top-N by recent activity (which would
        # never re-poll completed/stopped leads, whose last_action_at is frozen,
        # so a reply arriving after a sequence finished would never sync). We sort
        # by the lead's thread last_polled_at, nulls (never polled) first.
        from django.db.models import F, Min

        qs = (
            qs.annotate(_polled=Min("lead__threads__last_polled_at"))
            .order_by(F("_polled").asc(nulls_first=True), "last_action_at")[:limit]
        )

    stopped = 0
    for state in qs:
        messages = fetch_thread_messages(session, state.lead)
        thread, _ = MessageThread.objects.get_or_create(
            lead=state.lead, account=session.linkedin_profile,
        )

        inbound_reply = False
        latest_ts = thread.last_message_at
        # Unibox gate: an outbound message dated at/after we enrolled this lead is
        # us contacting them — as opposed to the account's pre-existing LinkedIn
        # conversations (from other tools), which pre-date enrollment.
        our_outbound = thread.contacted_by_tool
        new_reply = None  # newest genuinely-new inbound reply, for Slack notify
        for m in messages:
            if (
                m["direction"] == "out"
                and m["sent_at"]
                and state.created_at
                and m["sent_at"] >= state.created_at
            ):
                our_outbound = True
            # Reconcile a manual Unibox send: when we re-fetch the conversation,
            # our own outbound reply comes back with a synthesised id that differs
            # from the "manual-…" id we stored at queue time. Re-key the existing
            # manual row to the real id so the get_or_create below matches it
            # instead of inserting a duplicate.
            if m["direction"] == "out":
                manual = Message.objects.filter(
                    thread=thread, direction="out", sent_via_tool=True, body=m["body"],
                    linkedin_message_id__startswith="manual-",
                ).first()
                if manual is not None and manual.linkedin_message_id != m["linkedin_message_id"]:
                    manual.linkedin_message_id = m["linkedin_message_id"]
                    if m["sent_at"]:
                        manual.sent_at = m["sent_at"]
                    manual.save(update_fields=["linkedin_message_id", "sent_at"])
            _obj, created = Message.objects.get_or_create(
                thread=thread,
                linkedin_message_id=m["linkedin_message_id"],
                defaults={
                    "direction": m["direction"],
                    "body": m["body"],
                    "sent_at": m["sent_at"],
                    "sent_via_tool": False,
                    "sender_account": session.linkedin_profile if m["direction"] == "out" else None,
                },
            )
            if m["sent_at"] and (latest_ts is None or m["sent_at"] > latest_ts):
                latest_ts = m["sent_at"]
            # A reply only counts once we've actually acted (sent something) and
            # the inbound message post-dates that action — otherwise historical
            # messages would stop a sequence before it even starts.
            if (
                m["direction"] == "in"
                and state.last_action_at
                and m["sent_at"]
                and m["sent_at"] > state.last_action_at
            ):
                inbound_reply = True
                if created:  # only ping Slack for messages we haven't seen before
                    new_reply = m

        thread.last_polled_at = timezone.now()
        thread.last_message_at = latest_ts
        if inbound_reply:
            thread.has_inbound_reply = True
        if our_outbound:
            thread.contacted_by_tool = True
        thread.save()

        # Slack: notify on a brand-new reply to a conversation we started.
        if new_reply and (our_outbound or thread.contacted_by_tool):
            _notify_reply(state.lead, new_reply["body"], session)

        if inbound_reply and state.state == State.ACTIVE:
            state.state = State.STOPPED_REPLY
            state.save(update_fields=["state"])
            stopped += 1
            logger.info("Lead %s replied — sequence stopped", state.lead_id)

    return stopped


def sync_inbox(session, limit=40) -> int:
    """Refresh THIS account's KIT-CONTACTED threads — smallest detection surface.

    ONE ``fetch_conversations`` lists the recent inbox (a reply bumps a conversation
    to the top, so a new reply is always in this list); we then ``fetch_messages``
    ONLY for recent conversations that match a thread THIS kit contacted. It never
    touches organic / other-tool (HeyReach) conversations and makes no per-thread
    call it doesn't need — 1 list call + a few message reads per account. Returns
    the number of threads that gained new messages.
    """
    from crm.models import Lead
    from linkedin.models import Message, MessageThread
    from linkedin_cli.actions.conversations import parse_messages
    from linkedin_cli.api.client import PlaywrightLinkedinAPI
    from linkedin_cli.api.messaging import fetch_conversations, fetch_messages

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
    me = _self_name(session)
    account = session.linkedin_profile
    updated = 0

    for conv in elements[:limit]:
        conv_urn = conv.get("entityUrn")
        if not conv_urn:
            continue
        part_urn = ""
        for p in conv.get("conversationParticipants", []):
            u = p.get("hostIdentityUrn", "")
            if u and u != mailbox_urn:
                part_urn = u
                break
        if not part_urn:
            continue
        lead = Lead.objects.filter(urn=part_urn).first()
        if not lead:
            continue
        # ONLY threads THIS kit contacted — never organic / HeyReach.
        thread = MessageThread.objects.filter(
            account=account, lead=lead, contacted_by_tool=True,
        ).first()
        if not thread:
            continue
        if not thread.linkedin_thread_id:
            thread.linkedin_thread_id = conv_urn

        latest = thread.last_message_at
        gained = False
        for m in parse_messages(fetch_messages(api, conv_urn) or {}):
            sender = m.get("sender", "")
            ts = _parse_ts(m.get("timestamp", ""))
            direction = "out" if sender == me else "in"
            _obj, created = Message.objects.get_or_create(
                thread=thread,
                linkedin_message_id=_synth_id(sender, m.get("text", ""), m.get("timestamp", "")),
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

        thread.last_message_at = latest
        thread.last_polled_at = timezone.now()
        thread.save()
        if gained:
            updated += 1

    return updated


def _notify_reply(lead, body, session):
    """Fire a Slack reply notification (never raises into the poll loop)."""
    try:
        from linkedin.notify.slack import notify_reply

        name = f"{lead.first_name or ''} {lead.last_name or ''}".strip() or (lead.public_identifier or "lead")
        account = getattr(session.linkedin_profile, "linkedin_username", "") or ""
        notify_reply(name, body, lead.linkedin_url, account)
    except Exception as exc:
        logger.warning("Slack reply notify failed: %r", exc)
