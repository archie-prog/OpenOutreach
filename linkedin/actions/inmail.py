# linkedin/actions/inmail.py
"""InMail send action (M4).

``linkedin_cli`` has no InMail primitive, so this is an app-side Playwright
action. Gated by the account's ``has_inmail`` capability (Sales Navigator /
Recruiter); when unavailable it **skips cleanly** so a sequence never crashes.
The browser composer lives in ``_compose_inmail`` (mocked in tests).

Robustness: an account whose ``has_inmail`` flag is set but which has no real
InMail credits shows no "Message" button (or only a Premium upsell) on a
2nd-degree profile. We detect that with bounded waits and raise
``InMailUnavailable`` → the send is reported ``skipped`` (not a 30s timeout, and
never a subject-less normal message sent by mistake).
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# How long to wait for the composer affordances before concluding this account
# can't actually InMail this lead. Short on purpose — a missing Message button or
# Subject field means "no InMail here", not "the page is slow".
_AVAILABILITY_TIMEOUT_MS = 6000


class InMailUnavailable(RuntimeError):
    """The InMail composer never appeared — no credits / not InMail-able / UI gate."""


def send_inmail(session, lead, subject: str, body: str) -> dict:
    """Send an InMail to ``lead``.

    Returns ``{success, skipped, error, linkedin_message_id}``. Never raises:
    a missing-capability account yields ``skipped=True``; a UI failure yields
    ``success=False`` with the error captured.
    """
    if not session.linkedin_profile.has_inmail:
        logger.warning(
            "InMail unavailable for %s — skipping (no Sales Navigator/Recruiter)",
            session.linkedin_profile,
        )
        return {"success": False, "skipped": True, "error": "no_inmail", "linkedin_message_id": None}

    try:
        message_id = _compose_inmail(session, lead, subject, body)
    except InMailUnavailable as exc:
        # The flag says this account has InMail but the live page disagrees (no
        # composer / Premium gate). Skip cleanly rather than fail the step.
        logger.info("InMail composer unavailable for %s (%s) — skipping", lead, exc)
        return {"success": False, "skipped": True, "error": "inmail_unavailable", "linkedin_message_id": None}
    except Exception as exc:  # UI flow is brittle; never crash the sequence.
        logger.exception("InMail send failed for %s", lead)
        return {"success": False, "skipped": False, "error": str(exc), "linkedin_message_id": None}

    return {"success": True, "skipped": False, "error": None, "linkedin_message_id": message_id}


def _find_subject_field(page):
    """Return the InMail Subject input if the composer exposes one, else None."""
    for sel in (
        'label:has-text("Subject") >> .. >> input, input[name="subject"]',
        '[aria-label="Subject"]',
        '[placeholder*="Subject" i]',
    ):
        try:
            loc = page.locator(sel).first
            if loc.count():
                return loc
        except Exception:
            continue
    # Fall back to the accessible-name lookup (original selector).
    try:
        loc = page.get_by_label("Subject").first
        if loc.count():
            return loc
    except Exception:
        pass
    return None


def _compose_inmail(session, lead, subject: str, body: str):
    """Drive the LinkedIn InMail composer over Playwright (best-effort UI flow).

    Mocked in tests. Returns a message id when resolvable, else None. Raises
    :class:`InMailUnavailable` when the composer never opens (no InMail credits /
    not InMail-able / Premium gate) so the caller can skip instead of hang.
    """
    from linkedin_cli.browser.nav import goto_page

    session.ensure_browser()
    page = session.page
    goto_page(
        session,
        action=lambda: page.goto(lead.linkedin_url, wait_until="domcontentloaded"),
        expected_url_pattern=f"/in/{lead.public_identifier}",
        error_message="Failed to open profile for InMail",
    )

    # A profile we can InMail shows a "Message" button. No button (within a short
    # wait) == no InMail capability for this lead — skip rather than hang.
    from linkedin.browser.humanize import humanize_page
    humanize_page(page)
    msg_btn = page.get_by_role("button", name="Message").first
    try:
        msg_btn.wait_for(state="visible", timeout=_AVAILABILITY_TIMEOUT_MS)
    except Exception:
        raise InMailUnavailable("no Message button on profile")
    msg_btn.click()

    # The InMail composer has a Subject field; a normal message box / upsell does
    # not. Require it before typing — otherwise we'd risk sending a subject-less
    # plain message as if it were an InMail.
    if subject:
        field = None
        try:
            page.wait_for_timeout(1500)
        except Exception:
            pass
        for _ in range(int(_AVAILABILITY_TIMEOUT_MS / 500)):
            field = _find_subject_field(page)
            if field is not None:
                break
            try:
                page.wait_for_timeout(500)
            except Exception:
                pass
        if field is None:
            raise InMailUnavailable("no Subject field — not an InMail composer (no credits?)")
        from linkedin.browser.humanize import type_humanly
        type_humanly(field, subject)

    from linkedin.browser.humanize import type_humanly
    type_humanly(page.get_by_role("textbox").last, body)
    page.get_by_role("button", name="Send").click()
    return None
