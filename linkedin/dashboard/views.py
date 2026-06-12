# linkedin/dashboard/views.py
"""Dashboard JSON API + page (the visual layer, v1).

KPI tiles, a senders table with live cap usage, and a sequence rendered as a
branching flow graph — the HeyReach-style surfaces, served as JSON to a
self-contained page (no SPA build pipeline).
"""
from __future__ import annotations

import csv as _csv
import json
from urllib.parse import quote

from django.contrib.admin.views.decorators import staff_member_required
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST


def welcome_page(request):
    """Public landing/welcome page. Authenticated staff get a 'Go to dashboard'
    CTA; everyone else gets 'Sign in'. This is the commercial front door."""
    from linkedin.models import SiteConfig

    cfg = SiteConfig.load()
    return render(request, "welcome.html", {
        "workspace_name": cfg.workspace_name or "Grantgunner",
        "authed": request.user.is_authenticated and request.user.is_staff,
    })


@ensure_csrf_cookie
@staff_member_required(login_url="/login/")
def dashboard_page(request):
    # ensure_csrf_cookie guarantees the csrftoken cookie is set so the page's
    # fetch helper can send it back as X-CSRFToken on mutating requests — the
    # mutating endpoints enforce CSRF (no more blanket @csrf_exempt).
    from linkedin.models import SiteConfig

    return render(request, "dashboard/dashboard.html", {
        "workspace_name": SiteConfig.load().workspace_name or "Grantgunner",
    })


@staff_member_required
def api_kpi_timeseries(request):
    """Per-bucket counts (connections/messages/inmails/replies) for the chart.
    Granularity: day (<=45d window), week (<=180d), else month."""
    from datetime import timedelta

    from django.db.models import Count
    from django.db.models.functions import TruncDay, TruncMonth, TruncWeek
    from django.utils import timezone

    from linkedin.models import ActionLog, Message

    days = int(request.GET.get("days") or 30)
    campaign = request.GET.get("campaign") or None
    account = request.GET.get("account") or None
    window = days if days else 365
    if window <= 45:
        gran, trunc = "day", TruncDay
    elif window <= 180:
        gran, trunc = "week", TruncWeek
    else:
        gran, trunc = "month", TruncMonth
    start = timezone.now() - timedelta(days=window)

    def series(qs, ts_field):
        qs = qs.filter(**{f"{ts_field}__gte": start})
        rows = qs.annotate(b=trunc(ts_field)).values("b").annotate(c=Count("id"))
        return {r["b"].date().isoformat(): r["c"] for r in rows if r["b"]}

    base = ActionLog.objects.all()
    if campaign:
        base = base.filter(campaign_id=campaign)
    if account:
        base = base.filter(linkedin_profile_id=account)
    connects = series(base.filter(action_type="connect"), "created_at")
    messages = series(base.filter(action_type="message"), "created_at")
    inmails = series(base.filter(action_type="inmail"), "created_at")
    rep_qs = Message.objects.filter(direction="in", thread__contacted_by_tool=True)
    if campaign:
        rep_qs = rep_qs.filter(thread__lead__campaign_states__campaign_id=campaign)
    if account:
        rep_qs = rep_qs.filter(thread__account_id=account)
    replies = series(rep_qs, "sent_at")

    labels = sorted(set(connects) | set(messages) | set(inmails) | set(replies))
    return JsonResponse({
        "granularity": gran,
        "labels": labels,
        "connections": [connects.get(d, 0) for d in labels],
        "messages": [messages.get(d, 0) for d in labels],
        "inmails": [inmails.get(d, 0) for d in labels],
        "replies": [replies.get(d, 0) for d in labels],
    })


@staff_member_required
def api_context(request):
    from linkedin.models import SiteConfig

    return JsonResponse({"ai_context": SiteConfig.load().ai_context})


@staff_member_required
def api_holiday_countries(request):
    """Country codes the bank-holiday calendar supports (from the holidays lib);
    a curated fallback if the package isn't installed."""
    fallback = [
        # UK + the named markets, then EU27 + EEA
        "GB", "IE", "US", "CA", "AU", "NZ",
        "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR",
        "HU", "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI",
        "ES", "SE", "CH", "NO", "IS",
    ]
    try:
        import holidays
        codes = sorted(holidays.list_supported_countries().keys())
    except Exception:
        codes = fallback
    return JsonResponse({"countries": codes})


@staff_member_required
def api_ai_config(request):
    """AI provider settings for the management page. The API key is never echoed
    back in full — only a masked hint and whether one is set."""
    from linkedin.models import SiteConfig

    cfg = SiteConfig.load()
    key = cfg.llm_api_key or ""
    hint = (key[:3] + "…" + key[-4:]) if len(key) >= 10 else ("•••• (set)" if key else "")
    return JsonResponse({
        "provider": cfg.llm_provider,
        "model": cfg.ai_model,
        "api_base": cfg.llm_api_base,
        "key_set": bool(key),
        "key_hint": hint,
        "providers": [{"value": v, "label": l} for v, l in SiteConfig.LLMProvider.choices],
        "slack_webhook_url": cfg.slack_webhook_url,
        "slack_notify_replies": cfg.slack_notify_replies,
    })


@staff_member_required
@require_POST
def api_ai_config_save(request):
    """Save AI provider/model/base, and the API key only when a new one is typed
    (a blank key field leaves the stored key untouched)."""
    from linkedin.models import SiteConfig

    payload = json.loads(request.body or "{}")
    cfg = SiteConfig.load()
    provider = payload.get("provider")
    if provider and provider in SiteConfig.LLMProvider.values:
        cfg.llm_provider = provider
    if "model" in payload:
        cfg.ai_model = (payload.get("model") or "").strip()[:200]
    if "api_base" in payload:
        cfg.llm_api_base = (payload.get("api_base") or "").strip()[:500]
    new_key = (payload.get("api_key") or "").strip()
    if new_key:
        cfg.llm_api_key = new_key[:500]
    if "slack_webhook_url" in payload:
        cfg.slack_webhook_url = (payload.get("slack_webhook_url") or "").strip()[:500]
    if "slack_notify_replies" in payload:
        cfg.slack_notify_replies = bool(payload["slack_notify_replies"])
    cfg.save()
    return JsonResponse({"ok": True})


@staff_member_required
def api_settings(request):
    """Workspace + billing settings for the Settings tab."""
    from linkedin.models import SiteConfig

    cfg = SiteConfig.load()
    connected = bool(cfg.stripe_customer_id or cfg.stripe_portal_url)
    return JsonResponse({
        "workspace_name": cfg.workspace_name or "Grantgunner",
        "billing_email": cfg.billing_email,
        "plan": cfg.plan or "self-hosted",
        "billing_status": cfg.billing_status or "active",
        "billing_connected": connected,
        "portal_url": cfg.stripe_portal_url,
        "login_email": request.user.email or request.user.username,
    })


@staff_member_required
@require_POST
def api_settings_save(request):
    from linkedin.models import SiteConfig

    payload = json.loads(request.body or "{}")
    cfg = SiteConfig.load()
    if "workspace_name" in payload:
        cfg.workspace_name = (payload.get("workspace_name") or "Grantgunner").strip()[:120]
    if "billing_email" in payload:
        cfg.billing_email = (payload.get("billing_email") or "").strip()[:200]
    # Stripe wiring (optional): paste a customer id / portal URL to enable the
    # "Manage subscription" deep-link. Left blank = self-hosted, no billing.
    if "stripe_customer_id" in payload:
        cfg.stripe_customer_id = (payload.get("stripe_customer_id") or "").strip()[:120]
    if "stripe_portal_url" in payload:
        cfg.stripe_portal_url = (payload.get("stripe_portal_url") or "").strip()[:500]
    cfg.save()
    return JsonResponse({"ok": True})


@staff_member_required
def api_overview(request):
    """At-a-glance counts for the Guide/Settings tabs: accounts, campaigns,
    leads, this-month usage — so the user can see the system is wired up."""
    from datetime import timedelta

    from django.utils import timezone

    from linkedin.models import ActionLog, Campaign, LeadList, LinkedInProfile

    now = timezone.now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    accounts = LinkedInProfile.objects.all()
    return JsonResponse({
        "accounts": accounts.count(),
        "accounts_active": accounts.filter(active=True).count(),
        "accounts_with_2fa": accounts.exclude(totp_secret="").count(),
        "campaigns_active": Campaign.objects.filter(status=Campaign.Status.ACTIVE, sequence__isnull=False).count(),
        "lead_lists": LeadList.objects.filter(archived_at__isnull=True).count(),
        "connects_this_month": ActionLog.objects.filter(action_type="connect", created_at__gte=month_start).count(),
        "messages_this_month": ActionLog.objects.filter(action_type="message", created_at__gte=month_start).count(),
    })


@staff_member_required
@require_POST
def api_slack_test(request):
    """Send a test message to the configured Slack webhook."""
    from linkedin.models import SiteConfig
    from linkedin.notify.slack import post_text

    url = SiteConfig.load().slack_webhook_url
    if not url:
        return JsonResponse({"error": "Save a Slack webhook URL first"}, status=400)
    ok = post_text(":wave: Test from OpenOutreach — LinkedIn reply notifications are wired up.", webhook_url=url)
    return JsonResponse({"ok": ok} if ok else {"error": "Slack didn't accept the message — check the webhook URL"}, status=200 if ok else 400)


@staff_member_required
@require_POST
def api_context_save(request):
    from linkedin.models import SiteConfig

    payload = json.loads(request.body or "{}")
    cfg = SiteConfig.load()
    cfg.ai_context = (payload.get("ai_context") or "")[:10000]
    cfg.save(update_fields=["ai_context"])
    return JsonResponse({"ok": True})


@staff_member_required
@require_POST
def api_update_step(request, step_id):
    """Edit a sequence step: change its config, and/or change what the step
    actually IS (its ``step_type``). Changing type resets the config to that
    type's defaults (unless a config is supplied)."""
    from linkedin.models import SequenceStep

    step = SequenceStep.objects.filter(pk=step_id).first()
    if not step:
        return JsonResponse({"error": "not found"}, status=404)
    try:
        payload = json.loads(request.body or "{}")
    except ValueError:
        return JsonResponse({"error": "bad json"}, status=400)

    new_type = payload.get("step_type")
    new_config = payload.get("config")
    if new_type is None and not isinstance(new_config, dict):
        return JsonResponse({"error": "config must be an object"}, status=400)

    fields = []
    if new_type is not None:
        if new_type not in SequenceStep.StepType.values:
            return JsonResponse({"error": "bad step_type"}, status=400)
        # Branch-arity safety: only connect/message carry a 'no' (failure) branch.
        # End keeps every following step intact (dormant), so it's exempt — any
        # other non-branching type would orphan an existing failure branch.
        branching = {SequenceStep.StepType.CONNECT, SequenceStep.StepType.MESSAGE, SequenceStep.StepType.END}
        if new_type not in branching and step.children.filter(branch=SequenceStep.Branch.FAILURE).exists():
            return JsonResponse(
                {"error": "This step has a 'No reply / Not accepted' branch. Change it to "
                          "Send message or Send connection request, or remove that branch first."},
                status=400,
            )
        step.step_type = new_type
        fields.append("step_type")
        if not isinstance(new_config, dict):
            step.config = dict(_STEP_DEFAULTS.get(new_type, {}))
            fields.append("config")

    if isinstance(new_config, dict):
        step.config = new_config
        fields.append("config")

    step.save(update_fields=fields)
    return JsonResponse({"ok": True, "step_type": step.step_type, "config": step.config})


@staff_member_required
@require_POST
def api_delete_step(request, step_id):
    """Remove a step and splice the branch back together: the steps that followed
    it re-attach to its slot. So removing an End mid-branch makes the rest of the
    branch run again — nothing below it was lost."""
    from linkedin.models import SequenceStep

    step = SequenceStep.objects.filter(pk=step_id).first()
    if not step:
        return JsonResponse({"error": "not found"}, status=404)
    succ = list(step.children.filter(branch=SequenceStep.Branch.SUCCESS).order_by("order_in_branch"))
    fail = list(step.children.filter(branch=SequenceStep.Branch.FAILURE).order_by("order_in_branch"))
    if succ and fail:
        return JsonResponse(
            {"error": "This step splits into two paths — remove one path first."}, status=400,
        )
    spliced = succ or fail
    for child in spliced:
        child.parent = step.parent
        child.branch = step.branch
        child.save(update_fields=["parent", "branch"])
    # Re-point any lead currently parked on this step onto the spliced-in
    # successor so it continues the flow. Without this the FK SET_NULL leaves
    # current_step=None and the executor silently COMPLETEs the lead, skipping
    # the rest of the branch. If the step was a leaf (no successor), completing
    # is the correct outcome, so leave those to SET_NULL.
    from linkedin.models import LeadCampaignState
    if spliced:
        successor = spliced[0]
        LeadCampaignState.objects.filter(current_step=step).update(
            current_step=successor, current_branch=successor.branch,
        )
    step.delete()
    return JsonResponse({"ok": True})


def _accepted_count(campaign_id=None):
    """How many leads accepted our connection request — derived from where they
    are in the flow (past the connect step, on its 'accepted' branch), so it's
    correct retroactively (not reliant on the connect_accepted event existing).
    Counts across all sequence campaigns, or one if a campaign id is given."""
    from linkedin.models import Campaign, LeadCampaignState, SequenceStep

    qs = Campaign.objects.filter(sequence__isnull=False).select_related("sequence")
    if campaign_id:
        qs = qs.filter(pk=campaign_id)
    total = 0
    for c in qs:
        root = c.sequence.root_step if c.sequence_id else None
        if not root or root.step_type != SequenceStep.StepType.CONNECT:
            continue
        # All step ids reachable down the connect's SUCCESS (accepted) branch.
        ids, stack = set(), list(root.children.filter(branch=SequenceStep.Branch.SUCCESS))
        while stack:
            s = stack.pop()
            ids.add(s.id)
            stack.extend(list(s.children.all()))
        if ids:
            total += LeadCampaignState.objects.filter(campaign=c, current_step_id__in=ids).count()
    return total


def _lead_name(lead):
    if lead.first_name and lead.last_name:
        return f"{lead.first_name} {lead.last_name}".strip()
    parts = (lead.public_identifier or "").split("-")
    # Drop a trailing LinkedIn hash suffix (pure digits, or mixed letters+digits).
    while parts and (parts[-1].isdigit() or (any(c.isdigit() for c in parts[-1]) and any(c.isalpha() for c in parts[-1]))):
        parts.pop()
    derived = " ".join(p.title() for p in parts)
    return derived or lead.first_name or "Lead"


@staff_member_required
def api_accounts(request):
    from linkedin.accounts.limits import cap_for, daily_count, inmail_sent_this_month
    from linkedin.models import LinkedInProfile

    out = []
    for a in LinkedInProfile.objects.all():
        caps = a.daily_caps_json or {}
        out.append({
            "id": a.pk, "username": a.linkedin_username, "active": a.active,
            "has_inmail": a.has_inmail, "has_totp": bool(a.totp_secret),
            "inmail_monthly_cap": a.inmail_monthly_cap, "inmail_used_month": inmail_sent_this_month(a),
            "connect_cap": caps.get("connect", 25), "message_cap": caps.get("message", 50),
            "connect_used": daily_count(a, "connect"), "message_used": daily_count(a, "message"),
            "send_start_hour": a.send_start_hour, "send_end_hour": a.send_end_hour,
            "send_timezone": a.send_timezone, "send_weekdays": a.send_weekdays or [0, 1, 2, 3, 4],
            "skip_bank_holidays": a.skip_bank_holidays, "holiday_country": a.holiday_country,
            "connect_random_enabled": a.connect_random_enabled,
            "connect_random_min": a.connect_random_min, "connect_random_max": a.connect_random_max,
            "connect_today": cap_for(a, "connect"),  # today's effective cap (random or fixed)
        })
    return JsonResponse({"accounts": out})


@staff_member_required
@require_POST
def api_account_add(request):
    from django.contrib.auth.models import User
    from linkedin.models import LinkedInProfile

    payload = json.loads(request.body or "{}")
    email = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    if not email or not password:
        return JsonResponse({"error": "email + password required"}, status=400)
    handle = (email.split("@")[0].lower().replace(".", "_").replace("+", "_") or "acct")[:140]
    base, i = handle, 1
    while User.objects.filter(username=handle).exists():
        handle = f"{base}{i}"
        i += 1
    user = User.objects.create(username=handle, is_staff=True, is_active=True)
    user.set_unusable_password()
    user.save()
    prof = LinkedInProfile.objects.create(
        user=user, linkedin_username=email, linkedin_password=password,
        has_inmail=bool(payload.get("has_inmail")),
        totp_secret=(payload.get("totp_secret") or "").replace(" ", "").upper(),
        legal_accepted=True,
    )
    return JsonResponse({"ok": True, "id": prof.pk})


@staff_member_required
@require_POST
def api_account_update(request, account_id):
    from linkedin.models import LinkedInProfile

    prof = LinkedInProfile.objects.filter(pk=account_id).first()
    if not prof:
        return JsonResponse({"error": "not found"}, status=404)
    payload = json.loads(request.body or "{}")

    def _int(key):
        """Parse payload[key] as int, or None if absent/blank/invalid — so a
        cleared numeric field in the UI (value "") is ignored instead of 500-ing
        the whole save with int("")."""
        v = payload.get(key)
        if v is None or v == "":
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    if "active" in payload:
        prof.active = bool(payload["active"])
    if "has_inmail" in payload:
        prof.has_inmail = bool(payload["has_inmail"])
    # TOTP secret: presence of the key (even empty) lets the UI clear/rotate it.
    # Spaces are stripped + uppercased so a wrong/blank value can actually be
    # corrected — the old `if payload.get(...)` made the secret write-once.
    if "totp_secret" in payload:
        prof.totp_secret = (payload.get("totp_secret") or "").replace(" ", "").upper()
    # Allow rotating the LinkedIn email/password from the dashboard. Changing
    # either invalidates the saved cookie session, so drop it to force a fresh
    # (TOTP-aware) login on the next worker cycle.
    creds_changed = False
    if payload.get("linkedin_username"):
        prof.linkedin_username = str(payload["linkedin_username"]).strip()[:200]
        creds_changed = True
    if payload.get("linkedin_password"):
        prof.linkedin_password = str(payload["linkedin_password"])[:200]
        creds_changed = True
    if creds_changed:
        prof.cookie_data = None
    if _int("inmail_monthly_cap") is not None:
        prof.inmail_monthly_cap = max(0, _int("inmail_monthly_cap"))
    # Start from the full default set so we never drop the action types the UI
    # doesn't expose (inmail/profile_visit/like_post) — dropping them would zero
    # their cap and stall those sequence steps.
    from linkedin.models import default_daily_caps
    caps = {**default_daily_caps(), **(prof.daily_caps_json or {})}
    for ui_key, cap_key in (("connect_cap", "connect"), ("message_cap", "message"),
                            ("inmail_cap", "inmail"), ("profile_visit_cap", "profile_visit"),
                            ("like_post_cap", "like_post")):
        if _int(ui_key) is not None:
            caps[cap_key] = max(0, _int(ui_key))
    prof.daily_caps_json = caps
    # Per-account send schedule.
    if _int("send_start_hour") is not None:
        prof.send_start_hour = max(0, min(23, _int("send_start_hour")))
    if _int("send_end_hour") is not None:
        prof.send_end_hour = max(1, min(24, _int("send_end_hour")))
    if payload.get("send_timezone"):
        prof.send_timezone = str(payload["send_timezone"])[:64]
    if isinstance(payload.get("send_weekdays"), list):
        prof.send_weekdays = [int(d) for d in payload["send_weekdays"] if str(d).lstrip("-").isdigit() and 0 <= int(d) <= 6]
    if "skip_bank_holidays" in payload:
        prof.skip_bank_holidays = bool(payload["skip_bank_holidays"])
    if payload.get("holiday_country"):
        prof.holiday_country = str(payload["holiday_country"])[:8].upper()
    if "connect_random_enabled" in payload:
        prof.connect_random_enabled = bool(payload["connect_random_enabled"])
    if _int("connect_random_min") is not None:
        prof.connect_random_min = max(0, _int("connect_random_min"))
    if _int("connect_random_max") is not None:
        prof.connect_random_max = max(0, _int("connect_random_max"))
    prof.save()
    return JsonResponse({"ok": True})


@staff_member_required
def api_campaigns(request):
    from linkedin.models import Campaign, LinkedInProfile

    out = []
    qs = Campaign.objects.filter(sequence__isnull=False).exclude(
        status=Campaign.Status.ARCHIVED
    ).select_related("sequence", "lead_list").prefetch_related("sending_accounts").order_by("-id")
    for c in qs:
        out.append({
            "id": c.pk,
            "name": c.name,
            "status": c.status,
            "sequence": c.sequence.name if c.sequence else "",
            "sequence_id": c.sequence_id,
            "lead_list": c.lead_list.name if c.lead_list else "",
            "lead_list_id": c.lead_list_id,
            "leads": c.lead_states.count(),
            "include_current_network": c.include_current_network,
            "sending_account_ids": [a.pk for a in c.sending_accounts.all()],
            "sending_accounts": [a.linkedin_username for a in c.sending_accounts.all()],
        })
    accounts = [
        {"id": a.pk, "name": a.linkedin_username}
        for a in LinkedInProfile.objects.filter(active=True).order_by("id")
    ]
    return JsonResponse({"campaigns": out, "accounts": accounts})


@staff_member_required
@require_POST
def api_campaign_create(request):
    """Create a sequence-driven campaign natively (no Django admin): name +
    sequence + optional lead list. Launches active by default and enrolls the
    list's leads immediately."""
    from django.db import IntegrityError

    from linkedin.models import Campaign
    from linkedin.sequences.executor import enroll_campaign

    payload = json.loads(request.body or "{}")
    name = (payload.get("name") or "").strip()
    sequence_id = payload.get("sequence_id")
    lead_list_id = payload.get("lead_list_id") or None
    status = payload.get("status") or Campaign.Status.ACTIVE
    if not name:
        return JsonResponse({"error": "name required"}, status=400)
    if not sequence_id:
        return JsonResponse({"error": "pick a sequence"}, status=400)
    try:
        campaign = Campaign.objects.create(
            name=name, sequence_id=sequence_id, lead_list_id=lead_list_id, status=status,
            include_current_network=bool(payload.get("include_current_network")),
        )
        if payload.get("sending_account_ids"):
            campaign.sending_accounts.set(payload["sending_account_ids"])
    except IntegrityError:
        return JsonResponse({"error": "a campaign with that name already exists"}, status=400)
    enrolled = 0
    if status == Campaign.Status.ACTIVE and lead_list_id:
        enrolled = enroll_campaign(campaign).get("enrolled", 0)
    return JsonResponse({"ok": True, "id": campaign.pk, "enrolled": enrolled})


def _set_states(campaign, from_states, to_state, *, due_now=False):
    """Bulk-move a campaign's lead states between lifecycle states (pause/resume/
    archive). Never deletes — terminal/paused moves only."""
    from django.utils import timezone

    from linkedin.models import LeadCampaignState

    qs = LeadCampaignState.objects.filter(campaign=campaign, state__in=from_states)
    fields = {"state": to_state}
    if due_now:
        fields["next_action_due_at"] = timezone.now()
    return qs.update(**fields)


@staff_member_required
@require_POST
def api_campaign_update(request, campaign_id):
    """Update a campaign: rename, swap sequence/list, or change status. Status
    changes drive the lead states — pause→paused_manual, activate→active (due
    now) + enroll, archive→archived (terminal). No hard deletes."""
    from linkedin.models import Campaign, LeadCampaignState
    from linkedin.sequences.executor import enroll_campaign

    campaign = Campaign.objects.filter(pk=campaign_id).first()
    if not campaign:
        return JsonResponse({"error": "not found"}, status=404)
    payload = json.loads(request.body or "{}")
    S, LS = Campaign.Status, LeadCampaignState.State

    updates = []
    if "name" in payload and payload["name"].strip():
        campaign.name = payload["name"].strip(); updates.append("name")
    if "sequence_id" in payload:
        campaign.sequence_id = payload["sequence_id"] or None; updates.append("sequence")
    if "lead_list_id" in payload:
        campaign.lead_list_id = payload["lead_list_id"] or None; updates.append("lead_list")
    if "include_current_network" in payload:
        campaign.include_current_network = bool(payload["include_current_network"]); updates.append("include_current_network")
    if "sending_account_ids" in payload:
        campaign.sending_accounts.set(payload["sending_account_ids"] or [])

    status = payload.get("status")
    enrolled = 0
    if status and status != campaign.status:
        campaign.status = status; updates.append("status")
        if status == S.ACTIVE:
            _set_states(campaign, [LS.PAUSED_MANUAL], LS.ACTIVE, due_now=True)
            enrolled = enroll_campaign(campaign).get("enrolled", 0)
        elif status == S.PAUSED:
            _set_states(campaign, [LS.ACTIVE], LS.PAUSED_MANUAL)
        elif status == S.ARCHIVED:
            _set_states(campaign, [LS.ACTIVE, LS.PAUSED_MANUAL], LS.ARCHIVED)

    if updates:
        campaign.save()
    return JsonResponse({"ok": True, "status": campaign.status, "enrolled": enrolled})


@staff_member_required
@require_POST
def api_campaign_add_leads(request, campaign_id):
    """Add a lead list's leads into this campaign (a campaign can accumulate
    leads from several lists). If the campaign has no primary list yet, adopt
    this one as it."""
    from linkedin.models import Campaign, LeadList
    from linkedin.sequences.executor import enroll_lead_list

    campaign = Campaign.objects.filter(pk=campaign_id).first()
    if not campaign:
        return JsonResponse({"error": "not found"}, status=404)
    payload = json.loads(request.body or "{}")
    ll = LeadList.objects.filter(pk=payload.get("list_id")).first()
    if not ll:
        return JsonResponse({"error": "lead list not found"}, status=404)
    if not campaign.lead_list_id:
        campaign.lead_list = ll
        campaign.save(update_fields=["lead_list"])
    result = enroll_lead_list(campaign, ll)
    return JsonResponse({"ok": True, **result})


@staff_member_required
def api_campaign_leads(request, campaign_id):
    from linkedin.models import LeadCampaignState

    states = (
        LeadCampaignState.objects.filter(campaign_id=campaign_id)
        .select_related("lead", "current_step")
        .order_by("-lead__ai_score")
    )
    leads = []
    for s in states:
        stage = s.current_step.step_type if s.current_step else "—"
        if s.current_step and s.current_step.step_type == "connect" and s.awaiting_decision:
            stage = "connect (awaiting accept)"
        leads.append({
            "lead_name": _lead_name(s.lead),
            "lead_url": s.lead.linkedin_url,
            "title": s.lead.title,
            "company": s.lead.company,
            "ai_score": s.lead.ai_score,
            "stage": stage,
            "state": s.state,
        })
    return JsonResponse({"leads": leads})


@staff_member_required
def api_campaign_detail(request, campaign_id):
    """Everything about one campaign: meta, stats, recent activity, and the
    replies that have come in."""
    from collections import Counter

    from django.utils import timezone as _tz

    from linkedin.models import ActionLog, Campaign, LeadCampaignState, Message

    c = Campaign.objects.select_related("sequence", "lead_list").filter(pk=campaign_id).first()
    if not c:
        return JsonResponse({"error": "not found"}, status=404)

    def when(dt):
        return _tz.localtime(dt).strftime("%-d %b %H:%M") if dt else ""

    state_counts = Counter(
        LeadCampaignState.objects.filter(campaign=c).values_list("state", flat=True)
    )
    action_counts = Counter(
        ActionLog.objects.filter(campaign=c).values_list("action_type", flat=True)
    )
    # Recent activity — what the tool did, newest first (with who + post link).
    activity = [
        {"action": a.action_type, "at": when(a.created_at),
         "lead": _lead_name(a.lead) if a.lead_id else "—",
         "lead_url": a.lead.linkedin_url if a.lead_id else "",
         "post_url": a.target_url,
         "account": a.linkedin_profile.linkedin_username if a.linkedin_profile_id else ""}
        for a in ActionLog.objects.filter(campaign=c).select_related("linkedin_profile", "lead").order_by("-created_at")[:40]
    ]
    # Replies from this campaign's leads, newest first — ONLY on threads we
    # actually messaged (contacted_by_tool), so the account's pre-existing
    # conversations don't get counted as replies to our outreach.
    reply_qs = (
        Message.objects.filter(
            direction="in", thread__contacted_by_tool=True,
            thread__lead__campaign_states__campaign=c,
        )
        .select_related("thread__lead").order_by("-sent_at").distinct()[:40]
    )
    responses = [
        {"lead_name": _lead_name(m.thread.lead), "lead_url": m.thread.lead.linkedin_url,
         "body": m.body, "at": when(m.sent_at)}
        for m in reply_qs
    ]

    return JsonResponse({
        "id": c.pk, "name": c.name, "status": c.status,
        "sequence": c.sequence.name if c.sequence else "",
        "lead_list": c.lead_list.name if c.lead_list else "",
        "include_current_network": c.include_current_network,
        "sending_accounts": [a.linkedin_username for a in c.sending_accounts.all()],
        "sending_account_ids": [a.pk for a in c.sending_accounts.all()],
        "stats": {
            "enrolled": sum(state_counts.values()),
            "active": state_counts.get("active", 0),
            "completed": state_counts.get("completed", 0),
            "skipped_existing": state_counts.get("skipped_existing", 0),
            "connections": action_counts.get("connect", 0),
            "accepted": _accepted_count(c.pk),
            "messages": action_counts.get("message", 0),
            "posts_liked": action_counts.get("like_post", 0),
            "inmails": action_counts.get("inmail", 0),
            "replies": len(responses),
        },
        "activity": activity,
        "responses": responses,
    })


@staff_member_required
def api_inbox_accounts(request):
    from linkedin.models import LinkedInProfile

    return JsonResponse({"accounts": [
        {"id": a.pk, "name": a.linkedin_username} for a in LinkedInProfile.objects.all()
    ]})


@staff_member_required
def api_inbox_threads(request):
    from linkedin.models import MessageThread

    account = request.GET.get("account")
    # Conversations with at least one MESSAGE (excludes connection-request-only
    # threads). filter: all | replied (has inbound) | sent (outbound, no reply yet).
    f = request.GET.get("filter", "replied")
    # Only threads THIS tool actually started — never the account's pre-existing
    # LinkedIn conversations from other tools.
    base = MessageThread.objects.filter(contacted_by_tool=True, messages__isnull=False)
    # Archived threads are hidden everywhere except the dedicated "archived" view.
    if f == "archived":
        base = base.filter(archived_at__isnull=False)
    else:
        base = base.filter(archived_at__isnull=True)
    if f == "replied":
        base = base.filter(messages__direction="in")
    elif f == "sent":
        base = base.filter(messages__direction="out").exclude(messages__direction="in")
    if account:
        base = base.filter(account_id=account)
    campaign = request.GET.get("campaign")
    if campaign:
        base = base.filter(lead__campaign_states__campaign_id=campaign)
    lead_list = request.GET.get("lead_list")
    if lead_list:
        base = base.filter(lead__lead_list_id=lead_list)
    qs = (
        base.distinct().select_related("lead", "account").order_by("-last_message_at", "-created_at")
    )

    threads = []
    for t in qs[:300]:
        last = t.messages.order_by("-sent_at", "-fetched_at").first()
        threads.append({
            "id": t.pk,
            "lead_name": _lead_name(t.lead),
            "lead_url": t.lead.linkedin_url,
            "account_id": t.account_id,
            "account_name": t.account.linkedin_username if t.account else "",
            "last_message": (last.body[:90] if last and last.body else ""),
            "last_direction": last.direction if last else "",
            "unread": t.read_at is None,
            "has_reply": t.has_inbound_reply,
            "archived": t.archived_at is not None,
        })
    return JsonResponse({"threads": threads})


@staff_member_required
def api_inbox_thread(request, thread_id):
    from django.utils import timezone

    from linkedin.models import MessageThread

    t = MessageThread.objects.select_related("lead", "account").filter(pk=thread_id).first()
    if not t:
        return JsonResponse({"error": "not found"}, status=404)
    if t.read_at is None:
        t.read_at = timezone.now()
        t.save(update_fields=["read_at"])
    from linkedin.inbox.poller import MAX_MANUAL_SEND_ATTEMPTS
    msgs = [
        {
            "direction": m.direction,
            "body": m.body,
            "sent_at": m.sent_at.isoformat() if m.sent_at else "",
            "pending": m.pending_send,
            "failed": (not m.pending_send and bool(m.send_error) and m.send_attempts >= MAX_MANUAL_SEND_ATTEMPTS),
            "send_error": m.send_error,
        }
        for m in t.messages.order_by("sent_at", "fetched_at")
    ]
    return JsonResponse({
        "id": t.pk,
        "lead_name": _lead_name(t.lead),
        "lead_url": t.lead.linkedin_url,
        "account_name": t.account.linkedin_username if t.account else "",
        "archived": t.archived_at is not None,
        "messages": msgs,
    })


@staff_member_required
@require_POST
def api_inbox_archive(request, thread_id):
    """Archive (hide) or unarchive a Unibox conversation. Soft — the thread and
    its messages are kept; archived threads just drop out of the default list.
    Body: {"archived": true|false}; defaults to archiving."""
    from django.utils import timezone

    from linkedin.models import MessageThread

    t = MessageThread.objects.filter(pk=thread_id).first()
    if not t:
        return JsonResponse({"error": "not found"}, status=404)
    payload = json.loads(request.body or "{}")
    archive = payload.get("archived", True)
    t.archived_at = timezone.now() if archive else None
    t.save(update_fields=["archived_at"])
    return JsonResponse({"ok": True, "archived": t.archived_at is not None})


@staff_member_required
def api_leads(request):
    from linkedin.models import LeadList

    lists = []
    for ll in LeadList.objects.filter(archived_at__isnull=True).order_by("-created_at"):
        lists.append({
            "id": ll.pk, "name": ll.name, "source": ll.source_type,
            "count": ll.leads.count(), "target": ll.target_count, "pending": ll.pending_search,
        })
    return JsonResponse({"lists": lists})


@staff_member_required
@require_POST
def api_leads_csv(request):
    from linkedin.leads import importer
    from linkedin.models import LeadList

    payload = json.loads(request.body or "{}")
    name = (payload.get("name") or "CSV import").strip()
    csv_text = payload.get("csv_text") or ""
    ll = importer.create_lead_list(name=name, owner=request.user, source_type=LeadList.SourceType.CSV)
    try:
        result = importer.import_csv(ll, iter(csv_text.splitlines()))
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse({"ok": True, "list_id": ll.pk, **result})


def _build_search_url(filters: dict) -> str:
    """Build a LinkedIn people-search URL from free-text filters. Entity filters
    (specific company/school/location/industry URNs) aren't resolvable without
    LinkedIn's typeahead — use a pasted URL for those.
    """
    from urllib.parse import urlencode

    params = {}
    for key in ("keywords", "firstName", "lastName", "title", "company", "school"):
        val = (filters.get(key) or "").strip()
        if val:
            params[key] = val
    network = [n for n in (filters.get("network") or []) if n in ("F", "S", "O")]
    if network:
        params["network"] = json.dumps(network, separators=(",", ":"))
    lang = (filters.get("language") or "").strip()
    if lang:
        params["profileLanguage"] = lang
    return "https://www.linkedin.com/search/results/people/?" + urlencode(params)


@staff_member_required
@require_POST
def api_leads_search(request):
    """Queue a LinkedIn people search (filters, URL, or keyword). The browser
    worker scrapes it on its next cycle (one process owns the LinkedIn session).
    """
    from linkedin.leads import importer
    from linkedin.models import LeadList

    payload = json.loads(request.body or "{}")
    name = (payload.get("name") or "Search import").strip()
    filters = payload.get("filters")
    query = (payload.get("query") or "").strip()
    if filters:
        url = _build_search_url(filters)
    elif query:
        url = query if query.startswith("http") else (
            "https://www.linkedin.com/search/results/people/?keywords=" + quote(query)
        )
    else:
        return JsonResponse({"error": "filters or query required"}, status=400)
    ll = importer.create_lead_list(
        name=name, owner=request.user, source_type=LeadList.SourceType.SEARCH_URL, source_url=url,
    )
    ll.target_count = int(payload.get("target_count") or 30)
    ll.pending_search = True
    ll.save(update_fields=["pending_search", "target_count"])
    return JsonResponse({"ok": True, "queued": True, "list_id": ll.pk})


@staff_member_required
@require_POST
def api_leads_ai(request):
    """AI lead finder — describe your ICP; the worker generates search keywords
    via the LLM, searches, and enriches matches into a new list."""
    from linkedin.leads import importer
    from linkedin.models import LeadList

    payload = json.loads(request.body or "{}")
    name = (payload.get("name") or "AI leads").strip()
    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        return JsonResponse({"error": "describe who you're looking for"}, status=400)
    ll = importer.create_lead_list(
        name=name, owner=request.user, source_type=LeadList.SourceType.AI, source_url=prompt[:2000],
    )
    ll.target_count = int(payload.get("target_count") or 30)
    # Connection-degree filter: "S" (2nd only, default), "S,O" (2nd+3rd). "F" is
    # always stripped — the AI finder never sources 1st-degree connections.
    net = [c for c in (payload.get("network") or "S").upper().split(",") if c in ("S", "O")] or ["S"]
    ll.search_network = ",".join(net)
    ll.pending_search = True
    ll.save(update_fields=["pending_search", "target_count", "search_network"])
    importer.log_event(ll, "user", prompt)
    importer.log_event(ll, "system", f"Target set to {ll.target_count} leads. The worker will start finding them shortly.")
    return JsonResponse({"ok": True, "queued": True, "list_id": ll.pk})


@staff_member_required
@require_POST
def api_leads_continue(request, list_id):
    """Keep building a lead list: optionally refine the prompt ("these aren't
    right, here's why…") and/or raise the target (e.g. 1000 not 30)."""
    from linkedin.leads import importer
    from linkedin.models import LeadList

    ll = LeadList.objects.filter(pk=list_id).first()
    if not ll:
        return JsonResponse({"error": "not found"}, status=404)
    payload = json.loads(request.body or "{}")
    new_prompt = (payload.get("prompt") or "").strip()
    target = payload.get("target_count")
    if new_prompt:
        ll.source_url = ((ll.source_url or "") + " | " + new_prompt)[:2000]
    if target is not None:
        ll.target_count = int(target)
    else:
        ll.target_count = max(ll.target_count, ll.leads.count() + 30)
    ll.pending_search = True
    ll.save()
    if new_prompt:
        importer.log_event(ll, "user", new_prompt)
    importer.log_event(ll, "system", f"Continuing — target raised to {ll.target_count} leads.")
    return JsonResponse({"ok": True, "target": ll.target_count})


@staff_member_required
def api_leadlist_events(request, list_id):
    """The lead list's activity thread (the "AI chat"): your prompts and what
    the finder did each run, newest last."""
    from django.utils import timezone as _tz

    from linkedin.models import LeadList

    ll = LeadList.objects.filter(pk=list_id).first()
    if not ll:
        return JsonResponse({"error": "not found"}, status=404)

    def when(dt):
        d = _tz.localtime(dt)
        return d.strftime("%-d %b %H:%M")

    events = [
        {"role": e.role, "text": e.text, "meta": e.meta, "at": when(e.created_at)}
        for e in ll.events.all()
    ]
    # Legacy lists created before the activity log existed have no events — show
    # the original prompt(s) reconstructed from what's stored (source_url holds
    # the AI prompt, joined by " | " as it was refined).
    if not events:
        from linkedin.models import LeadList

        seeded = []
        if ll.source_type == LeadList.SourceType.AI and ll.source_url:
            for part in ll.source_url.split(" | "):
                if part.strip():
                    seeded.append({"role": "user", "text": part.strip(), "meta": {}, "at": when(ll.created_at)})
        elif ll.source_type == LeadList.SourceType.SEARCH_URL and ll.source_url:
            seeded.append({"role": "system", "text": f"Saved LinkedIn search: {ll.source_url}", "meta": {}, "at": when(ll.created_at)})
        seeded.append({
            "role": "system",
            "text": f"{ll.leads.count()} leads gathered so far (target {ll.target_count})."
                    + ("" if ll.pending_search else " Finder is idle — hit Continue / refine to gather more."),
            "meta": {}, "at": when(ll.created_at),
        })
        events = seeded
    return JsonResponse({"name": ll.name, "source": ll.source_type, "events": events})


@staff_member_required
@require_POST
def api_inbox_send(request, thread_id):
    """Queue a manual reply typed in the Unibox; the worker sends it from the
    thread's owning account on its next cycle."""
    import hashlib

    from django.utils import timezone

    from linkedin.models import Message, MessageThread

    t = MessageThread.objects.filter(pk=thread_id).first()
    if not t:
        return JsonResponse({"error": "not found"}, status=404)
    payload = json.loads(request.body or "{}")
    body = (payload.get("body") or "").strip()
    if not body:
        return JsonResponse({"error": "empty message"}, status=400)
    mid = "manual-" + hashlib.sha1((body + str(timezone.now())).encode()).hexdigest()[:16]
    # sent_at stays NULL until the worker actually sends it — the Unibox renders a
    # "queued" pill while pending_send is True, so the UI never claims a message
    # was sent before it left the browser.
    Message.objects.create(
        thread=t, direction="out", body=body, sent_via_tool=True, pending_send=True,
        sender_account=t.account, linkedin_message_id=mid, sent_at=None,
    )
    t.contacted_by_tool = True  # a manual reply is us contacting them
    t.save(update_fields=["contacted_by_tool"])
    return JsonResponse({"ok": True, "queued": True})


@staff_member_required
def api_leadlist_export(request, list_id):
    from linkedin.models import LeadList

    ll = LeadList.objects.filter(pk=list_id).first()
    if not ll:
        return JsonResponse({"error": "not found"}, status=404)
    resp = HttpResponse(content_type="text/csv")
    resp["Content-Disposition"] = f'attachment; filename="leads-{ll.pk}.csv"'
    w = _csv.writer(resp)
    w.writerow(["first_name", "last_name", "title", "company", "location", "ai_score", "ai_reason", "linkedin_url", "public_identifier"])
    for lead in ll.leads.all().order_by("-ai_score"):
        w.writerow([
            lead.first_name, lead.last_name, lead.title, lead.company, lead.location,
            lead.ai_score if lead.ai_score is not None else "", lead.ai_reason,
            lead.linkedin_url, lead.public_identifier,
        ])
    return resp


@staff_member_required
@require_POST
def api_create_sequence(request):
    from linkedin.models import Sequence

    payload = json.loads(request.body or "{}")
    name = (payload.get("name") or "").strip() or "New sequence"
    seq = Sequence.objects.create(name=name, owner=request.user)
    return JsonResponse({"id": seq.pk, "name": seq.name})


_STEP_DEFAULTS = {
    "connect": {"wait_days_before_branch_decision": 14, "personalised_note": ""},
    "message": {"template": "Hi {first_name},"},
    "inmail": {"subject": "", "body": ""},
    "wait": {"days": 2},
    "profile_visit": {},
    "like_post": {},
    "end": {},
    "blank": {},
}


@staff_member_required
@require_POST
def api_sequence_archive(request, sequence_id):
    from linkedin.models import Sequence

    seq = Sequence.objects.filter(pk=sequence_id).first()
    if not seq:
        return JsonResponse({"error": "not found"}, status=404)
    seq.archive()  # soft-delete (recoverable); hidden from the picker
    return JsonResponse({"ok": True})


@staff_member_required
@require_POST
def api_sequence_duplicate(request, sequence_id):
    """Deep-copy a sequence (name + full step tree) into a new one."""
    from linkedin.models import Sequence, SequenceStep

    src = Sequence.objects.filter(pk=sequence_id).first()
    if not src:
        return JsonResponse({"error": "not found"}, status=404)
    new = Sequence.objects.create(name=f"{src.name} (copy)"[:200], owner=src.owner)

    def copy_subtree(src_step, new_parent):
        clone = SequenceStep.objects.create(
            sequence=new, parent=new_parent, branch=src_step.branch,
            step_type=src_step.step_type, config=dict(src_step.config or {}),
            order_in_branch=src_step.order_in_branch,
        )
        for child in src_step.children.all().order_by("branch", "order_in_branch"):
            copy_subtree(child, clone)

    root = src.root_step
    if root:
        copy_subtree(root, None)
    return JsonResponse({"ok": True, "id": new.pk})


@staff_member_required
@require_POST
def api_sequence_rename(request, sequence_id):
    from linkedin.models import Sequence

    seq = Sequence.objects.filter(pk=sequence_id).first()
    if not seq:
        return JsonResponse({"error": "not found"}, status=404)
    name = (json.loads(request.body or "{}").get("name") or "").strip()
    if not name:
        return JsonResponse({"error": "name required"}, status=400)
    seq.name = name[:200]
    seq.save(update_fields=["name"])
    return JsonResponse({"ok": True})


@staff_member_required
@require_POST
def api_create_step(request, sequence_id):
    from linkedin.models import Sequence, SequenceStep

    seq = Sequence.objects.filter(pk=sequence_id).first()
    if not seq:
        return JsonResponse({"error": "not found"}, status=404)
    payload = json.loads(request.body or "{}")
    step_type = payload.get("step_type")
    if step_type not in SequenceStep.StepType.values:
        return JsonResponse({"error": "bad step_type"}, status=400)
    branch = payload.get("branch", SequenceStep.Branch.ROOT)
    if branch not in SequenceStep.Branch.values:
        return JsonResponse({"error": "bad branch"}, status=400)
    parent = None
    if payload.get("parent_id"):
        parent = SequenceStep.objects.filter(pk=payload["parent_id"], sequence=seq).first()
    # If this (parent, branch) slot already holds a step, INSERT the new step
    # before it: the new step takes the slot and the old child re-parents onto
    # the new step's success branch (linear continuation).
    existing = SequenceStep.objects.filter(sequence=seq, parent=parent, branch=branch).first()
    step = SequenceStep.objects.create(
        sequence=seq, parent=parent, branch=branch, step_type=step_type,
        config=_STEP_DEFAULTS.get(step_type, {}), order_in_branch=0,
    )
    if existing:
        existing.parent = step
        existing.branch = SequenceStep.Branch.SUCCESS
        existing.save(update_fields=["parent", "branch"])
    return JsonResponse({"id": step.pk, "inserted_before": existing.pk if existing else None})


@staff_member_required
def api_kpis(request):
    from datetime import timedelta

    from django.utils import timezone

    from linkedin.models import ActionLog, LeadCampaignState, MessageThread

    State = LeadCampaignState.State
    days = int(request.GET.get("days") or 0)
    campaign = request.GET.get("campaign") or None
    account = request.GET.get("account") or None
    since = timezone.now() - timedelta(days=days) if days else None

    def actions(t):
        q = ActionLog.objects.filter(action_type=t)
        if since:
            q = q.filter(created_at__gte=since)
        if campaign:
            q = q.filter(campaign_id=campaign)
        if account:
            q = q.filter(linkedin_profile_id=account)
        return q.count()

    def states(s):
        q = LeadCampaignState.objects.filter(state=s)
        if campaign:
            q = q.filter(campaign_id=campaign)
        return q.count()

    # Replies to OUR outreach only — threads this tool messaged.
    replies_q = MessageThread.objects.filter(contacted_by_tool=True, messages__direction="in")
    if since:
        replies_q = replies_q.filter(messages__direction="in", messages__sent_at__gte=since)
    if campaign:
        replies_q = replies_q.filter(lead__campaign_states__campaign_id=campaign)
    if account:
        replies_q = replies_q.filter(account_id=account)

    return JsonResponse({
        # Accepted connections are recorded as connect_accepted ActionLog events
        # (by the executor when it detects acceptance; backfill_acceptances seeds
        # historical ones). Counting them here — rather than walking the flow tree
        # — makes the tile honour the same period/campaign/account filters as every
        # other KPI, which the tree-walk silently ignored.
        "connection_requests": actions("connect"),
        "connections_accepted": actions("connect_accepted"),
        "messages_sent": actions("message"),
        "inmails_sent": actions("inmail"),
        "posts_liked": actions("like_post"),
        "replies": replies_q.distinct().count(),
        "active": states(State.ACTIVE),
        "completed": states(State.COMPLETED),
    })


@staff_member_required
def api_activity(request):
    """Recent actions/events this tool performed (filterable by period / campaign /
    account / type) — connection requests, acceptances, likes, messages, InMails.
    Only ever reflects the tool's own outreach (ActionLog is written by it alone)."""
    from datetime import timedelta

    from django.utils import timezone

    from linkedin.models import ActionLog

    days = int(request.GET.get("days") or 0)
    campaign = request.GET.get("campaign") or None
    account = request.GET.get("account") or None
    atype = request.GET.get("type") or None

    qs = ActionLog.objects.select_related("lead", "linkedin_profile").order_by("-created_at")
    if days:
        qs = qs.filter(created_at__gte=timezone.now() - timedelta(days=days))
    if campaign:
        qs = qs.filter(campaign_id=campaign)
    if account:
        qs = qs.filter(linkedin_profile_id=account)
    if atype:
        qs = qs.filter(action_type=atype)

    out = [
        {
            "action": a.action_type,
            "lead": _lead_name(a.lead) if a.lead_id else "—",
            "lead_url": a.lead.linkedin_url if a.lead_id else "",
            "post_url": a.target_url,
            "at": timezone.localtime(a.created_at).strftime("%-d %b %H:%M"),
            "account": a.linkedin_profile.linkedin_username if a.linkedin_profile_id else "",
        }
        for a in qs[:120]
    ]
    return JsonResponse({"activity": out})


@staff_member_required
def api_senders(request):
    from linkedin.accounts.limits import cap_for, daily_count
    from linkedin.models import LinkedInProfile

    senders = []
    for a in LinkedInProfile.objects.all():
        caps = a.daily_caps_json or {}
        senders.append({
            "id": a.pk,
            "username": a.linkedin_username,
            "active": a.active,
            "has_inmail": a.has_inmail,
            "usage": {
                k: {
                    "used": daily_count(a, k),
                    "cap": cap_for(a, "connect") if k == "connect" else v,
                }
                for k, v in caps.items()
            },
        })
    return JsonResponse({"senders": senders})


@staff_member_required
def api_sequences(request):
    from linkedin.models import Sequence

    seqs = Sequence.objects.filter(archived_at__isnull=True).order_by("name")
    return JsonResponse({"sequences": [{"id": s.pk, "name": s.name} for s in seqs]})


@staff_member_required
def api_sequence(request, sequence_id):
    from linkedin.models import Sequence, SequenceStep

    seq = Sequence.objects.filter(pk=sequence_id).first()
    if not seq:
        return JsonResponse({"error": "not found"}, status=404)

    def node(step):
        return {
            "id": step.pk,
            "step_type": step.step_type,
            "branch": step.branch,
            "config": step.config,
            "success": [node(c) for c in step.children.filter(branch=SequenceStep.Branch.SUCCESS).order_by("order_in_branch")],
            "failure": [node(c) for c in step.children.filter(branch=SequenceStep.Branch.FAILURE).order_by("order_in_branch")],
        }

    root = seq.root_step
    return JsonResponse({"id": seq.pk, "name": seq.name, "root": node(root) if root else None})
