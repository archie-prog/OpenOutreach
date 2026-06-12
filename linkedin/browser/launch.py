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


def _launch_fingerprinted(storage_state, account=None):
    """Launch a stealthed browser with a consistent, region-correct fingerprint:
    locale + timezone matched to the account and a fixed viewport. We deliberately
    do NOT spoof the User-Agent/platform — a UA that disagrees with the real engine
    is itself a detection signal — so we only fix the safe, high-signal bits."""
    from playwright.sync_api import sync_playwright
    try:
        from playwright_stealth import Stealth
    except Exception:
        Stealth = None

    tz = (getattr(account, "send_timezone", None) or "Europe/London")
    playwright = sync_playwright().start()
    # Hide the automation switches: drop --enable-automation (which sets
    # navigator.webdriver + the 'controlled by automated software' infobar) and
    # disable the AutomationControlled blink feature. Stealth covers the JS side.
    browser = playwright.chromium.launch(
        headless=False,
        args=["--disable-blink-features=AutomationControlled"],
        ignore_default_args=["--enable-automation"],
    )
    context = browser.new_context(
        storage_state=storage_state,
        locale="en-GB",
        timezone_id=tz,
        viewport={"width": 1536, "height": 864},
    )
    context.set_default_timeout(30000)
    context.set_default_navigation_timeout(30000)
    if Stealth is not None:
        try:
            Stealth().apply_stealth_sync(context)
        except Exception:
            pass
    page = context.new_page()
    return page, context, browser, playwright


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

    session.page, session.context, session.browser, session.playwright = _launch_fingerprinted(storage_state, session.linkedin_profile)

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


def verify_account(profile):
    """Non-blocking connection test for the onboarding UI. Returns (ok, error).

    Uses the account's SAVED cookies (or a stored TOTP secret) only — it never
    falls back to an interactive login that would block on a human 2FA challenge.
    On a successful TOTP login it persists the fresh cookies."""
    from linkedin_cli.browser.login import launch_browser, dismiss_comply_gate
    from linkedin.auth.login import _is_authenticated, _url

    cookie_data = profile.cookie_data
    if not cookie_data and not profile.totp_secret:
        return False, "No saved session — connect the account (enter the password and approve the login)."

    page = context = browser = playwright = None
    try:
        page, context, browser, playwright = _launch_fingerprinted(cookie_data or None, profile)
        if not cookie_data and profile.totp_secret:
            from linkedin.auth.login import login_with_totp, TwoFactorLoginError

            class _Shim:
                pass
            shim = _Shim()
            shim.page = page
            shim.ensure_browser = lambda: None
            try:
                login_with_totp(shim, profile.linkedin_username, profile.linkedin_password, profile.totp_secret)
            except TwoFactorLoginError as exc:
                return False, "2FA login failed: %s" % (str(exc)[:200])
            profile.cookie_data = context.storage_state()
            profile.save(update_fields=["cookie_data"])
            return True, ""

        page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
        try:
            dismiss_comply_gate(page)
        except Exception:
            pass
        try:
            page.wait_for_timeout(2500)
        except Exception:
            pass
        if _is_authenticated(_url(page)):
            return True, ""
        return False, "Session expired or invalid — reconnect the account."
    except Exception as exc:
        return False, str(exc)[:250]
    finally:
        try:
            if browser:
                browser.close()
            if playwright:
                playwright.stop()
        except Exception:
            pass
