# linkedin/browser/session.py
from __future__ import annotations

import logging
import random
import time
from functools import cached_property

from linkedin.conf import MIN_DELAY, MAX_DELAY

logger = logging.getLogger(__name__)

# The main LinkedIn auth cookie
_AUTH_COOKIE_NAME = "li_at"


def random_sleep(min_val, max_val):
    delay = random.uniform(min_val, max_val)
    logger.debug(f"Pause: {delay:.2f}s")
    time.sleep(delay)


# Phrases LinkedIn shows on its "account restricted" / automation-detected
# checkpoint page. That page returns HTTP 200 (NOT a 401), so it slips past
# auth-error handling — we detect it by wording and stop, instead of hammering
# a flagged account (the aawilding incident: days of connects on a restricted
# session). Kept deliberately narrow so a normal 2FA challenge never matches.
RESTRICTION_MARKERS = (
    "temporarily restricted",
    "we've restricted your account",
    "we have restricted your account",
    "your account has been restricted",
    "detected the use of software that automates",
    "restricted your account until",
)


def detect_restriction(page):
    """Return a short reason string if ``page`` is LinkedIn's restriction /
    automation-checkpoint page, else None. Never raises."""
    if page is None:
        return None
    try:
        body = (page.inner_text("body") or "").lower()
    except Exception:
        return None
    for m in RESTRICTION_MARKERS:
        if m in body:
            i = body.find(m)
            return body[max(0, i - 40):i + 130].strip().replace("\n", " ")
    return None


class AccountSession:
    def __init__(self, linkedin_profile):
        self.linkedin_profile = linkedin_profile
        self.django_user = linkedin_profile.user

        # Active campaign — set by the daemon before each lane execution
        self.campaign = None

        # Playwright objects – created on first access or after crash
        self.page = None
        self.context = None
        self.browser = None
        self.playwright = None
        # The real x-li-track / x-li-page-instance LinkedIn's own JS attaches to
        # voyager/api requests — captured live so our fetch()es match the web
        # client (a real clientVersion that rotates ~monthly, and a page-instance
        # matching the page we navigated). See attach_voyager_capture().
        self._li_track = None
        self._li_page_instance = None

    @cached_property
    def campaigns(self):
        """All campaigns this user belongs to (cached)."""
        from linkedin.models import Campaign
        return list(Campaign.objects.filter(users=self.django_user))

    def attach_voyager_capture(self):
        """Listen for LinkedIn's own voyager/api requests and remember the
        x-li-track / x-li-page-instance headers it sends, so our API client can
        replicate them. Best-effort; never raises."""
        page = self.page
        if page is None:
            return

        def _cap(req):
            try:
                if "/voyager/" not in req.url:
                    return
                h = req.headers
                t = h.get("x-li-track")
                pi = h.get("x-li-page-instance")
                if t:
                    self._li_track = t
                if pi:
                    self._li_page_instance = pi
            except Exception:
                pass

        try:
            page.on("request", _cap)
        except Exception:
            pass

    def close_browser(self):
        """Tear down the browser/Playwright so no session is held open outside the
        account's working hours. ensure_browser() relaunches it on next use."""
        for obj, meth in ((self.browser, "close"), (self.playwright, "stop")):
            try:
                if obj:
                    getattr(obj, meth)()
            except Exception:
                pass
        self.page = self.context = self.browser = self.playwright = None

    def ensure_browser(self):
        """Launch or recover browser + login if needed. Call before using .page"""
        from linkedin.browser.launch import start_browser_session

        if not self.page or self.page.is_closed():
            logger.debug("Launching/recovering browser for %s", self)
            start_browser_session(session=self)
        else:
            self._maybe_refresh_cookies()

    def assert_not_restricted(self):
        """Raise AuthenticationError if the current page is LinkedIn's restriction
        checkpoint (HTTP 200 — not a 401, so nothing else catches it). Lets the
        worker auto-pause a flagged account instead of continuing to act on it."""
        reason = detect_restriction(self.page)
        if not reason:
            return
        try:
            from linkedin_cli.exceptions import AuthenticationError
        except Exception:
            class AuthenticationError(Exception):
                pass
        raise AuthenticationError("LinkedIn restriction/checkpoint page: %s" % reason)

    @cached_property
    def self_profile(self) -> dict:
        """Authenticated user's profile dict, fetched once per session.

        The dict isn't persisted to DB (we dropped ``Lead.profile_data``),
        so the first access per session triggers a Voyager call via the
        ``linkedin_cli`` self-discovery primitive; the ``cached_property``
        keeps it warm for the rest of the session. CRM-side persistence
        (the disqualified ``self_lead``) is layered on in ``register_self_lead``.
        """
        from linkedin_cli.setup.self_profile import discover_self_profile
        from linkedin.db.leads import register_self_lead

        profile = discover_self_profile(self)
        register_self_lead(self, profile)
        return profile

    def wait(self, min_delay=MIN_DELAY, max_delay=MAX_DELAY):
        random_sleep(min_delay, max_delay)
        self.page.wait_for_load_state("domcontentloaded")

    def reauthenticate(self):
        """Force a fresh login: close browser, clear saved cookies, re-launch."""
        from linkedin.browser.launch import start_browser_session

        logger.warning("Re-authenticating %s — clearing saved session", self)
        self.close()
        self.linkedin_profile.cookie_data = None
        self.linkedin_profile.save(update_fields=["cookie_data"])
        start_browser_session(session=self)

    def _maybe_refresh_cookies(self):
        """Re-login if the li_at auth cookie in the saved DB state is expired."""
        from linkedin.browser.launch import start_browser_session

        self.linkedin_profile.refresh_from_db(fields=["cookie_data"])
        cookie_data = self.linkedin_profile.cookie_data
        if not cookie_data:
            return
        for cookie in cookie_data.get("cookies", []):
            if cookie.get("name") == _AUTH_COOKIE_NAME:
                expires = cookie.get("expires", -1)
                if expires > 0 and expires < time.time():
                    logger.warning("Auth cookie expired for %s — re-authenticating", self)
                    # Clear the stale cookies so the relaunch takes the fresh-login
                    # branch (which runs the TOTP flow) instead of restoring the
                    # expired session and failing the saved-session validation.
                    self.close()
                    self.linkedin_profile.cookie_data = None
                    self.linkedin_profile.save(update_fields=["cookie_data"])
                    start_browser_session(session=self)
                return

    def close(self):
        if self.context:
            try:
                self.context.close()
                if self.browser:
                    self.browser.close()
                if self.playwright:
                    self.playwright.stop()
                logger.info("Browser closed gracefully (%s)", self)
            except Exception as e:
                logger.debug("Error closing browser: %s", e)
            finally:
                self.page = self.context = self.browser = self.playwright = None

        logger.info("Account session closed → %s", self)

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def __repr__(self) -> str:
        return self.linkedin_profile.linkedin_username
