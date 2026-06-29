# tests/db/test_account_distribution.py
"""Capacity-aware shared-pool distribution of pre-connect leads.

Pre-connect leads are an unowned shared pool spread across every eligible
account; the owner is appointed only when the connect is sent and is sticky
afterwards (anti-ban Rule #6 — no mid-sequence identity switch)."""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from tests.factories import UserFactory


def _account(username, **kw):
    from linkedin.models import LinkedInProfile

    defaults = dict(
        linkedin_username=username, linkedin_password="x", active=True,
        # 24/7 window so in_session() is deterministic regardless of wall-clock.
        send_start_hour=0, send_end_hour=24, send_weekdays=[0, 1, 2, 3, 4, 5, 6],
    )
    defaults.update(kw)
    return LinkedInProfile.objects.create(user=UserFactory(), **defaults)


def _active_campaign(*accounts):
    from linkedin.models import Campaign

    c = Campaign.objects.create(name="C", status=Campaign.Status.ACTIVE)
    if accounts:
        c.sending_accounts.set(accounts)
    return c


def _due_lead(campaign, i, sending_account=None):
    from crm.models import Lead
    from linkedin.models import LeadCampaignState

    lead = Lead.objects.create(
        linkedin_url=f"https://www.linkedin.com/in/x{i}/", public_identifier=f"x{i}",
    )
    return LeadCampaignState.objects.create(
        lead=lead, campaign=campaign, sending_account=sending_account,
        next_action_due_at=timezone.now() - timedelta(seconds=1),
    )


@pytest.mark.django_db
class TestAccountDistribution:
    def test_pre_connect_leads_spread_across_all_accounts(self):
        from linkedin.sequences.executor import due_states_by_account

        a1 = _account("a1@x.c")
        a2 = _account("a2@x.c")
        c = _active_campaign(a1, a2)
        for i in range(6):
            _due_lead(c, i)

        groups = {p.pk: states for p, states in due_states_by_account(None)}
        assert set(groups) == {a1.pk, a2.pk}, "every eligible account must send, not just one"
        assert sum(len(s) for s in groups.values()) == 6
        # max-remaining-budget round-robin → balanced within one.
        assert abs(len(groups[a1.pk]) - len(groups[a2.pk])) <= 1

    def test_owned_lead_stays_sticky(self):
        from linkedin.sequences.executor import due_states_by_account

        a1 = _account("a1@x.c")
        a2 = _account("a2@x.c")
        c = _active_campaign(a1, a2)
        owned = _due_lead(c, 0, sending_account=a1)

        groups = {p.pk: [s.pk for s in states] for p, states in due_states_by_account(None)}
        assert owned.pk in groups.get(a1.pk, [])
        assert owned.pk not in groups.get(a2.pk, []), "must never migrate identity (Rule #6)"

    def test_account_at_cap_is_excluded(self):
        from linkedin.sequences.executor import due_states_by_account

        full = _account("full@x.c", daily_caps_json={"connect": 0})
        ok = _account("ok@x.c")
        c = _active_campaign(full, ok)
        for i in range(4):
            _due_lead(c, i)

        groups = {p.pk: states for p, states in due_states_by_account(None)}
        assert full.pk not in groups, "an account with no connect headroom must not be assigned"
        assert len(groups.get(ok.pk, [])) == 4

    def test_share_is_independent_of_history(self):
        """Each account operates independently: its share of the pool does NOT depend
        on how many the OTHERS have already sent — leads are dealt round-robin,
        bounded only by each account's own cap."""
        from linkedin.models import AccountDailyCounter
        from linkedin.sequences.executor import due_states_by_account

        busy = _account("busy@x.c")
        fresh = _account("fresh@x.c")
        # busy already sent 5 connects today; fresh sent 0 — history must NOT skew shares.
        AccountDailyCounter.objects.create(
            account=busy, date=timezone.localdate(), action_type="connect", count=5,
        )
        c = _active_campaign(busy, fresh)
        for i in range(4):
            _due_lead(c, i)

        groups = {p.pk: states for p, states in due_states_by_account(None)}
        # round-robin, history ignored → even 2/2 (NOT favouring the behind account).
        assert len(groups.get(busy.pk, [])) == 2
        assert len(groups.get(fresh.pk, [])) == 2

    def test_no_eligible_account_assigns_nothing(self):
        from linkedin.sequences.executor import due_states_by_account

        # Pool present but every account is at cap → nothing scheduled, no crash.
        full = _account("full@x.c", daily_caps_json={"connect": 0})
        c = _active_campaign(full)
        _due_lead(c, 0)

        groups = {p.pk: states for p, states in due_states_by_account(None)}
        assert groups == {}
