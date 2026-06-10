# linkedin/browser/launch.py
"""Persist + orchestrate the daemon's LinkedIn browser session.

Cookie persistence (to the Django DB) and the launch/login orchestration are
OpenOutreach concerns, so they live here. The reusable *mechanics* — launching a
stealthed browser, driving the login form, clearing checkpoints — stay in the
Django-free ``linkedin_cli.browser`` library and are called from here.
"""
from __future__ import annotations

import logging

from termcolor import colored

from linkedin_cli.auth import authenticate
from linkedin_cli.browser.login import dismiss_comply_gate, launch_browser
from linkedin_cli.browser.nav import goto_page

logger = logging.getLogger(__name__)

LINKEDIN_FEED_URL = "https://www.linkedin.com/feed/"


def _save_cookies(session):
    """Persist Playwright storage state (cookies) to the DB."""
    state = session.context.storage_state()
    session.linkedin_profile.cookie_data = state
    session.linkedin_profile.save(update_fields=["cookie_data"])


def _fresh_login(session):
    """Run the login flow from scratch and persist the resulting cookies. Uses
    the native TOTP path when the account has a secret, else the human-in-the-loop
    ``linkedin_cli`` authenticator."""
    lp = session.linkedin_profile
    if lp.totp_secret:
        from linkedin.auth.login import login_with_totp
        login_with_totp(session, lp.linkedin_username, lp.linkedin_password, lp.totp_secret)
    else:
        authenticate(session, username=lp.linkedin_username, password=lp.linkedin_password)
    _save_cookies(session)
    logger.info(colored("Login successful – session saved", "green", attrs=["bold"]))


def start_browser_session(session):
    logger.debug("Configuring browser for %s", session)

    session.linkedin_profile.refresh_from_db(fields=["cookie_data"])
    cookie_data = session.linkedin_profile.cookie_data

    storage_state = cookie_data if cookie_data else None
    if storage_state:
        logger.info("Loading saved session for %s", session)

    session.page, session.context, session.browser, session.playwright = launch_browser(storage_state=storage_state)

    if not storage_state:
        _fresh_login(session)
    else:
        session.page.goto(LINKEDIN_FEED_URL)
        dismiss_comply_gate(session.page)
        try:
            goto_page(
                session,
                action=lambda: None,
                expected_url_pattern="/feed",
                error_message="Saved session invalid",
            )
        except Exception:
            # The saved cookies no longer authenticate (expired/revoked). Drop
            # them and log in fresh (TOTP-aware) rather than dead-ending.
            logger.warning("Saved session invalid for %s — logging in fresh", session)
            session.linkedin_profile.cookie_data = None
            session.linkedin_profile.save(update_fields=["cookie_data"])
            _fresh_login(session)

    # "domcontentloaded" — "load" waits for every subresource (analytics
    # beacons, lazy media) and on LinkedIn that event may never fire,
    # hanging the daemon for the duration of the browser timeout.
    session.page.wait_for_load_state("domcontentloaded")
    logger.info(colored("Browser ready", "green", attrs=["bold"]))
