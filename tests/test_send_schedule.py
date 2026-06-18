"""Per-account send window + paced drip + Blank resume."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth.models import User

import linkedin.conf as conf
from linkedin.accounts import limits
from linkedin.models import LinkedInProfile


def _account(**kw):
    u = User.objects.create(username=kw.pop("u", "sched_user"))
    defaults = dict(
        user=u, linkedin_username="x@y.com", linkedin_password="p",
        send_start_hour=9, send_end_hour=17, send_timezone="Europe/London",
        send_weekdays=[0, 1, 2, 3, 4],
    )
    defaults.update(kw)
    return LinkedInProfile.objects.create(**defaults)


@pytest.mark.django_db
def test_is_send_time_inside_and_outside_window():
    a = _account()
    tz = ZoneInfo("Europe/London")
    assert limits.is_send_time(a, datetime(2026, 6, 8, 10, 0, tzinfo=tz))   # Mon 10:00
    assert not limits.is_send_time(a, datetime(2026, 6, 8, 8, 0, tzinfo=tz))   # before 09
    assert not limits.is_send_time(a, datetime(2026, 6, 8, 18, 0, tzinfo=tz))  # after 17
    assert not limits.is_send_time(a, datetime(2026, 6, 6, 10, 0, tzinfo=tz))  # Sat


@pytest.mark.django_db
def test_next_send_time_jumps_to_window_open():
    a = _account()
    tz = ZoneInfo("Europe/London")
    # Saturday afternoon → next slot is Monday 09:00 local.
    nxt = limits.next_send_time(a, datetime(2026, 6, 6, 14, 0, tzinfo=tz)).astimezone(tz)
    assert (nxt.weekday(), nxt.hour) == (0, 9)


@pytest.mark.django_db
def test_pacing_ahead_defers_to_slot(monkeypatch):
    """Already sent more than the schedule expects by now → wait for the slot."""
    monkeypatch.setattr(conf, "ENABLE_ACTION_PACING", True)
    from django.utils import timezone

    a = _account(daily_caps_json={"connect": 8})  # 8h window / 8 = 1h spacing
    monkeypatch.setattr(limits, "is_send_time", lambda acc, dt=None: True)
    # Zero the (now heavy-tailed) jitter so this test pins the schedule-ANCHOR
    # behaviour deterministically; jitter spread is covered separately.
    monkeypatch.setattr(limits, "_heavy_tailed_jitter", lambda spacing: 0.0)
    # Window opened ~30 min ago; we've already sent 3 → next slot is far ahead.
    monkeypatch.setattr(limits, "_window_open_today", lambda acc, now: now - timedelta(minutes=30))
    monkeypatch.setattr(limits, "daily_count", lambda acc, at, date=None: 3)
    slot = limits.next_action_at(a, "connect")
    assert (slot - timezone.now()).total_seconds() > 60 * 60  # ~2h out (3rd slot), in the future


@pytest.mark.django_db
def test_pacing_behind_catches_up(monkeypatch):
    """Behind the schedule (e.g. after a stall) → eligible now to catch up."""
    monkeypatch.setattr(conf, "ENABLE_ACTION_PACING", True)
    from django.utils import timezone

    a = _account(daily_caps_json={"connect": 25})
    monkeypatch.setattr(limits, "is_send_time", lambda acc, dt=None: True)
    # Window opened 5h ago; only sent 2 → way behind → fire now.
    monkeypatch.setattr(limits, "_window_open_today", lambda acc, now: now - timedelta(hours=5))
    monkeypatch.setattr(limits, "daily_count", lambda acc, at, date=None: 2)
    monkeypatch.setattr(limits, "_last_action_at", lambda acc, at: None)
    slot = limits.next_action_at(a, "connect")
    assert (slot - timezone.now()).total_seconds() <= 0  # go now


@pytest.mark.django_db
def test_blank_step_resumes_to_next(fake_session):
    from django.utils import timezone

    from linkedin.models import LeadCampaignState, Sequence, SequenceStep
    from crm.models import Lead
    from linkedin.sequences import executor

    seq = Sequence.objects.create(name="s-blank", owner=fake_session.django_user)
    blank = SequenceStep.objects.create(sequence=seq, step_type="blank", branch="root", config={})
    nxt = SequenceStep.objects.create(sequence=seq, parent=blank, branch="success", step_type="wait", config={"days": 3})
    lead = Lead.objects.create(public_identifier="bl", linkedin_url="https://x/in/bl/")
    camp = fake_session.campaign
    st = LeadCampaignState.objects.create(
        lead=lead, campaign=camp, current_step=blank,
        state=LeadCampaignState.State.ACTIVE, next_action_due_at=timezone.now(),
    )
    executor.advance_state(fake_session, st)
    st.refresh_from_db()
    # Blank does nothing but advances the cursor to the next step (resumes).
    assert st.current_step_id == nxt.pk


def test_heavy_tailed_jitter_within_envelope_and_spread():
    """Jitter stays inside +/-3*spacing (clamp), is heavy-tailed (some draws exceed
    1*spacing — impossible under the old clamp), spread, and ~zero-mean (symmetric,
    so it doesn't shift the schedule anchor / under-send)."""
    spacing = 1000.0
    vals = [limits._heavy_tailed_jitter(spacing) for _ in range(4000)]
    assert all(-3 * spacing <= v <= 3 * spacing for v in vals)  # within the clamp
    assert any(abs(v) > spacing for v in vals)                  # heavy tail (beyond old +/-1)
    assert len({round(v) for v in vals}) > 50                   # varied, not constant
    assert abs(sum(vals) / len(vals)) < spacing * 0.15          # ~zero-mean (symmetric)
