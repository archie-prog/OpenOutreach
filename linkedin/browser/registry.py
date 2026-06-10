# linkedin/browser/registry.py
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_sessions: dict[int, "AccountSession"] = {}


def get_or_create_session(linkedin_profile) -> "AccountSession":
    from linkedin.browser.session import AccountSession

    pk = linkedin_profile.pk
    if pk not in _sessions:
        _sessions[pk] = AccountSession(linkedin_profile)
        logger.debug("Created new account session for %s", linkedin_profile)
    return _sessions[pk]


def get_first_active_profile():
    """Return the first active LinkedInProfile, or None."""
    from linkedin.models import LinkedInProfile

    return LinkedInProfile.objects.filter(active=True).select_related("user").first()


def resolve_profile(username: str | None = None):
    """Resolve a LinkedInProfile from an optional username, falling back to first active."""
    if username:
        from linkedin.models import LinkedInProfile

        return LinkedInProfile.objects.select_related("user").filter(
            user__username=username,
        ).first()
    return get_first_active_profile()
