# linkedin/auth/login.py
"""App-side LinkedIn login that clears 2FA via a stored TOTP secret.

``linkedin_cli.authenticate`` waits for a human at the 2FA challenge, so when an
account has a ``totp_secret`` we drive login ourselves and auto-fill the code
from :func:`linkedin.auth.totp.current_totp`.

The browser steps are real UI automation (the page locators are mocked in tests
via a fake ``page``). The important correctness properties, learned the hard way:

* **Wait for navigation before reading the URL.** Clicking *Sign in* triggers a
  client-side navigation; reading ``page.url`` synchronously right after the
  click sees the *login* page, so the 2FA challenge is never detected. We wait
  for the network to settle first.
* **A TOTP code is only valid for its 30s window.** If we land on the challenge
  near a window boundary the code can expire between generation and submit, so
  we retry with a freshly-minted code.
* **Verify success.** Landing back on ``/login`` (bad credentials) or still on a
  ``/checkpoint`` (wrong/expired code) is a failure — we raise so the caller
  never persists a half-authenticated cookie state as a valid session.
* **Persist the device.** Tick LinkedIn's "remember this device" when present so
  subsequent logins from the saved cookies don't re-challenge.
"""
from __future__ import annotations

import logging
import time

from linkedin.auth.totp import current_totp

logger = logging.getLogger(__name__)

LOGIN_URL = "https://www.linkedin.com/login"
FEED_URL = "https://www.linkedin.com/feed/"

# How long to wait for the challenge / feed to appear after submitting a form.
_NAV_TIMEOUT_MS = 30_000
_CHALLENGE_TIMEOUT_MS = 20_000


class TwoFactorLoginError(RuntimeError):
    """Raised when the native TOTP login fails to reach an authenticated feed."""


def _url(page) -> str:
    return (page.url or "").lower()


def _is_challenge(url: str) -> bool:
    return any(s in url for s in ("checkpoint/challenge", "/checkpoint/", "verification", "two-step"))


def _is_authenticated(url: str) -> bool:
    # The feed (or any in-app page that isn't login/checkpoint) means we're in.
    return "/feed" in url or ("linkedin.com" in url and not _is_challenge(url) and "/login" not in url and "/uas/" not in url)


def _settle(page, timeout_ms: int = _NAV_TIMEOUT_MS) -> None:
    """Wait for navigation to settle. Best-effort: LinkedIn's 'load' event can
    never fire (analytics beacons), so fall back to domcontentloaded + a short
    network-idle, swallowing timeouts."""
    for state in ("domcontentloaded", "networkidle"):
        try:
            page.wait_for_load_state(state, timeout=timeout_ms)
        except Exception:
            pass


def login_with_totp(session, username: str, password: str, totp_secret: str) -> None:
    """Drive the LinkedIn login form and auto-clear a TOTP 2FA challenge.

    Raises :class:`TwoFactorLoginError` if it cannot reach an authenticated page,
    so the caller never saves a pre-auth cookie state as a valid session.
    """
    session.ensure_browser()
    page = session.page

    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    from linkedin_cli.browser.nav import human_type
    human_type(page.locator("#username"), username)
    human_type(page.locator("#password"), password)
    page.get_by_role("button", name="Sign in").click()
    # CRITICAL: wait for the post-submit navigation before reading the URL,
    # otherwise we still see the login page and miss the challenge entirely.
    _settle(page)

    if _wait_for_challenge(page):
        _submit_2fa(page, totp_secret)
        _settle(page)

    _verify_authenticated(page)


def _wait_for_challenge(page) -> bool:
    """Return True once a 2FA challenge page is showing. Polls briefly because
    LinkedIn sometimes interstitially redirects (login → checkpoint)."""
    deadline = time.monotonic() + _CHALLENGE_TIMEOUT_MS / 1000.0
    while time.monotonic() < deadline:
        url = _url(page)
        if _is_challenge(url):
            return True
        if _is_authenticated(url):
            return False  # no 2FA required — already through
        if "/login" in url:
            # Still on login: either an in-flight redirect or rejected creds.
            # A rejected login keeps us here; give the redirect a beat then re-check.
            try:
                page.wait_for_timeout(500)
            except Exception:
                time.sleep(0.5)
            continue
        try:
            page.wait_for_timeout(300)
        except Exception:
            time.sleep(0.3)
    return _is_challenge(_url(page))


def _submit_2fa(page, totp_secret: str, attempts: int = 2) -> None:
    """Fill + submit the current TOTP code, retrying with a fresh code if the
    challenge page persists (the previous code expired at a 30s boundary)."""
    _check_remember_device(page)
    for attempt in range(attempts):
        code = current_totp(totp_secret)
        try:
            from linkedin_cli.browser.nav import human_type
            human_type(page.get_by_role("textbox").last, code)
        except Exception:
            from linkedin_cli.browser.nav import human_type
            human_type(page.locator("input[name='pin'], input#input__phone_verification_pin, input[type='text']"), code)
        page.get_by_role("button", name="Submit").click()
        logger.info("Submitted TOTP 2FA code (attempt %d)", attempt + 1)
        _settle(page, timeout_ms=_CHALLENGE_TIMEOUT_MS)
        if not _is_challenge(_url(page)):
            return
        logger.warning("Still on 2FA challenge after submit — retrying with a fresh code")
    # Leave the final state for _verify_authenticated to judge/raise on.


def _check_remember_device(page) -> None:
    """Tick 'remember this device' if LinkedIn offers it, so saved cookies don't
    re-trigger 2FA next launch. Best-effort — absent on many challenge variants."""
    try:
        box = page.get_by_role("checkbox")
        if box and box.count() > 0 and not box.first.is_checked():
            box.first.check()
    except Exception:
        pass


def _verify_authenticated(page) -> None:
    url = _url(page)
    if _is_authenticated(url):
        logger.info("Native TOTP login reached an authenticated page")
        return
    if _is_challenge(url):
        raise TwoFactorLoginError(
            "2FA challenge not cleared — the TOTP secret may be wrong or the code expired"
        )
    if "/login" in url:
        raise TwoFactorLoginError("Login rejected — check the LinkedIn email/password")
    raise TwoFactorLoginError(f"Login ended on an unexpected page: {page.url!r}")


# Backwards-compatible internal helper retained for the existing unit test, which
# constructs a fake page already parked on the challenge URL and asserts a code
# gets filled. It mirrors the live single-shot submit path.
def _maybe_submit_2fa(session, totp_secret: str) -> bool:
    page = session.page
    if not _is_challenge(_url(page)):
        return False
    code = current_totp(totp_secret)
    try:
        from linkedin_cli.browser.nav import human_type
        human_type(page.get_by_role("textbox").last, code)
    except Exception:
        from linkedin_cli.browser.nav import human_type
        human_type(page.locator("input[type='text']"), code)
    page.get_by_role("button", name="Submit").click()
    logger.info("Submitted TOTP 2FA code natively")
    return True
