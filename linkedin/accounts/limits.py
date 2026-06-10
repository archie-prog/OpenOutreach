# linkedin/accounts/limits.py
"""Per-account daily caps + least-recently-used round-robin selection (M6).

Every outgoing action is tallied in ``AccountDailyCounter`` (append/increment,
never deleted). Before scheduling an action the caller checks ``has_capacity``;
``select_account`` picks the least-recently-used account in a campaign's pool
that still has headroom, so volume spreads across accounts and no single account
trips LinkedIn's limits.
"""
from __future__ import annotations

import logging

from django.db.models import F
from django.utils import timezone

logger = logging.getLogger(__name__)


def cap_for(account, action_type, date=None) -> int:
    from linkedin.models import default_daily_caps

    # Connection requests can use a randomised daily cap within [min, max], picked
    # deterministically per (account, day) so it's stable across the day / restarts.
    if action_type == "connect" and getattr(account, "connect_random_enabled", False):
        import hashlib
        import random as _random

        lo = int(account.connect_random_min or 0)
        hi = int(account.connect_random_max or 0)
        if hi < lo:
            lo, hi = hi, lo
        if hi > 0:
            d = date or timezone.now().date()
            # Hash the (account, day) so consecutive days decorrelate — gives a
            # genuinely varied daily number (odds included), not clustered on
            # round/boundary values. Still deterministic per day (restart-safe).
            digest = hashlib.sha256(f"{account.pk}:{d.toordinal()}".encode()).digest()
            seed = int.from_bytes(digest[:8], "big")
            return _random.Random(seed).randint(lo, hi)

    # Merge stored caps over the full defaults so an action type missing from a
    # truncated daily_caps_json (e.g. a row saved before it carried inmail/
    # profile_visit/like_post) falls back to its sensible default instead of 0 —
    # a 0 would silently stall every sequence step of that type forever.
    defaults = default_daily_caps()
    caps = {**defaults, **(account.daily_caps_json or {})}
    return int(caps.get(action_type, defaults.get(action_type, 0)))


def daily_count(account, action_type, date=None) -> int:
    from linkedin.models import AccountDailyCounter

    date = date or timezone.now().date()
    row = AccountDailyCounter.objects.filter(account=account, date=date, action_type=action_type).first()
    return row.count if row else 0


def has_capacity(account, action_type, date=None) -> bool:
    return daily_count(account, action_type, date) < cap_for(account, action_type)


def inmail_sent_this_month(account) -> int:
    from linkedin.models import AccountDailyCounter

    now = timezone.now()
    rows = AccountDailyCounter.objects.filter(
        account=account, action_type="inmail", date__year=now.year, date__month=now.month,
    )
    return sum(r.count for r in rows)


def has_inmail_monthly_capacity(account) -> bool:
    return inmail_sent_this_month(account) < (account.inmail_monthly_cap or 0)


def record_action(account, action_type, date=None) -> int:
    """Increment the day's counter for (account, action_type); stamp last-used.
    Returns the new count. Idempotent row creation by (account, date, type).
    """
    from linkedin.models import AccountDailyCounter

    date = date or timezone.now().date()
    counter, _ = AccountDailyCounter.objects.get_or_create(
        account=account, date=date, action_type=action_type,
    )
    AccountDailyCounter.objects.filter(pk=counter.pk).update(count=F("count") + 1)
    account.last_used_at = timezone.now()
    account.save(update_fields=["last_used_at"])
    counter.refresh_from_db()
    return counter.count


# ── Per-account send schedule + paced "drip" ──────────────────────────


def _account_tz(account):
    from zoneinfo import ZoneInfo

    try:
        return ZoneInfo(account.send_timezone or "UTC")
    except Exception:
        return ZoneInfo("UTC")


def _is_bank_holiday(account, local_date) -> bool:
    if not account.skip_bank_holidays:
        return False
    try:
        import holidays  # optional dep; degrades to "no holidays" if absent
    except ImportError:
        logger.warning("skip_bank_holidays set but 'holidays' package not installed — ignoring")
        return False
    try:
        cal = holidays.country_holidays(account.holiday_country or "GB", years=local_date.year)
        return local_date in cal
    except Exception:
        return False


def is_send_time(account, dt=None) -> bool:
    """True if ``dt`` (default now) is inside the account's send window: right
    weekday, not a skipped bank holiday, and within [start, end) local hours."""
    dt = dt or timezone.now()
    local = dt.astimezone(_account_tz(account))
    weekdays = account.send_weekdays or [0, 1, 2, 3, 4]
    if local.weekday() not in weekdays:
        return False
    if _is_bank_holiday(account, local.date()):
        return False
    return account.send_start_hour <= local.hour < account.send_end_hour


def _at_hour(local, hour):
    """``local`` set to ``hour:00`` — but ``hour==24`` means midnight at the start
    of the next day (datetime can't hold hour 24). The UI allows an end hour of 24
    ('until midnight'); ``replace(hour=24)`` would raise ValueError and brick the
    account's scheduling."""
    from datetime import timedelta

    if hour >= 24:
        nxt = local + timedelta(days=1)
        return nxt.replace(hour=0, minute=0, second=0, microsecond=0)
    return local.replace(hour=hour, minute=0, second=0, microsecond=0)


def next_send_time(account, dt=None):
    """Earliest send-eligible moment at or after ``dt`` for this account."""
    from datetime import timedelta

    dt = dt or timezone.now()
    tz = _account_tz(account)
    weekdays = account.send_weekdays or [0, 1, 2, 3, 4]
    local = dt.astimezone(tz)
    for _ in range(367):  # at most a year ahead
        ok_day = local.weekday() in weekdays and not _is_bank_holiday(account, local.date())
        if ok_day:
            win_start = _at_hour(local, account.send_start_hour)
            win_end = _at_hour(local, account.send_end_hour)
            if local < win_start:
                return win_start  # window opens later today (tz-aware)
            if local < win_end:
                return local  # already inside the window (== dt on the first pass)
        # advance to the start of the next calendar day, local time
        local = (local + timedelta(days=1)).replace(
            hour=account.send_start_hour, minute=0, second=0, microsecond=0,
        )
    return dt


def _window_seconds(account) -> float:
    hours = max(1, (account.send_end_hour - account.send_start_hour))
    return hours * 3600.0


def random_slot_in_working_days(account, days, base=None):
    """A datetime ``days`` *working* days ahead (skipping non-working days and
    bank holidays per the account's schedule), at a RANDOM time within that day's
    send window. So "wait 1 day" = next working day, spread out — not exactly 24h.
    ``days`` < 1 is treated as 1 (a wait always crosses into a future working day)."""
    import random
    from datetime import datetime as _dt, timedelta

    base = base or timezone.now()
    tz = _account_tz(account)
    local = base.astimezone(tz)
    weekdays = account.send_weekdays or [0, 1, 2, 3, 4]

    def working(d):
        return d.weekday() in weekdays and not _is_bank_holiday(account, d)

    day = local.date()
    needed = max(1, int(days))
    while needed > 0:
        day = day + timedelta(days=1)
        if working(day):
            needed -= 1

    sh, eh = account.send_start_hour, account.send_end_hour
    offset = random.randint(0, max(1, (eh - sh)) * 3600 - 1)
    slot = _dt(day.year, day.month, day.day, sh, 0, 0) + timedelta(seconds=offset)
    return slot.replace(tzinfo=tz)


def _last_action_at(account, action_type):
    from linkedin.models import ActionLog

    return (
        ActionLog.objects.filter(linkedin_profile=account, action_type=action_type)
        .order_by("-created_at").values_list("created_at", flat=True).first()
    )


def _window_open_today(account, now):
    """The send window's opening instant for ``now``'s local day (tz-aware)."""
    local = now.astimezone(_account_tz(account))
    return local.replace(hour=account.send_start_hour, minute=0, second=0, microsecond=0)


def next_action_at(account, action_type):
    """When this account may next perform ``action_type``.

    Schedule-anchored: send #N of the day is pegged to ``window_open + N*spacing``
    (spacing = window / cap), with jitter. So it spreads the cap evenly across the
    window AND self-corrects — if it falls behind (slow cycle, restart, stall) it
    catches up toward the cap rather than drifting, while a ``min_gap`` floor keeps
    catch-up a steady recovery, never an instant burst. Never sends outside the
    window. ``<= now`` means 'go now'."""
    import random
    from datetime import timedelta

    from linkedin.conf import ENABLE_ACTION_PACING

    now = timezone.now()
    if not ENABLE_ACTION_PACING:
        return now
    cap = cap_for(account, action_type)
    if cap <= 0:
        return next_send_time(account, now)
    if not is_send_time(account):
        return next_send_time(account, now)

    spacing = _window_seconds(account) / cap
    sent = daily_count(account, action_type)
    # Anchor the next send to its slot in today's window (not to the last send),
    # so lost time is recoverable.
    slot = _window_open_today(account, now) + timedelta(seconds=sent * spacing)
    slot += timedelta(seconds=random.uniform(-0.2, 0.2) * spacing)  # jitter

    if slot <= now:  # on-schedule or behind → eligible, but floor the gap
        last = _last_action_at(account, action_type)
        min_gap = spacing * 0.4
        if last is not None and (now - last).total_seconds() < min_gap:
            return last + timedelta(seconds=min_gap)
        return now
    return slot  # ahead of schedule → wait for the slot


def account_pool(campaign):
    """Active LinkedIn accounts attached to the campaign (via its users)."""
    from linkedin.models import LinkedInProfile

    return list(LinkedInProfile.objects.filter(user__campaigns=campaign, active=True).distinct())


def select_account(campaign, action_type):
    """The least-recently-used account in the pool with remaining capacity, or None
    if all accounts are at their cap for this action today.
    """
    candidates = [a for a in account_pool(campaign) if has_capacity(a, action_type)]
    if not candidates:
        return None
    # Never-used first, then oldest last_used_at — round-robin.
    candidates.sort(key=lambda a: (a.last_used_at is not None, a.last_used_at or timezone.now()))
    return candidates[0]
