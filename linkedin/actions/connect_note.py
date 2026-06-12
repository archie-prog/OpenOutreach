# linkedin/actions/connect_note.py
"""Send a connection request WITH a personalised note.

``linkedin_cli``'s connect verb sends a note-less request, so a "Connection note"
typed in the flow builder was silently dropped. This is the app-side counterpart
(same pattern as ``actions/inmail.py`` / ``actions/like.py``): open the profile,
click Connect, choose "Add a note", fill it, send.

Best-effort UI automation — never raises into the sequence; failures come back in
the result dict so the caller can fall back to the note-less connect verb. The
note is capped at LinkedIn's 300-character limit for free accounts.

NOTE: the selectors here are written defensively with several fallbacks, but like
the other app-side flows they need a live LinkedIn session to fully verify.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# LinkedIn caps free-account connect notes at 300 chars (longer needs Premium).
MAX_NOTE_CHARS = 300


def send_connection_request_with_note(session, profile: dict, note: str) -> dict:
    """Send a connect request to ``profile`` (a ``{public_identifier,url,urn}``
    dict) with ``note``. Returns ``{"success": bool, "error"?: str}``."""
    note = (note or "").strip()[:MAX_NOTE_CHARS]
    if not note:
        return {"success": False, "error": "empty note"}
    try:
        return _connect_with_note(session, profile, note)
    except Exception as exc:  # brittle UI flow — never crash the sequence
        logger.exception("Connect-with-note failed for %s", profile.get("public_identifier"))
        return {"success": False, "error": str(exc)}


def _connect_with_note(session, profile: dict, note: str) -> dict:
    session.ensure_browser()
    page = session.page

    url = profile.get("url") or f"https://www.linkedin.com/in/{profile.get('public_identifier')}/"
    page.goto(url, wait_until="domcontentloaded")
    try:
        page.wait_for_timeout(2000)
    except Exception:
        pass

    # The Connect button is sometimes on the top card, sometimes behind a "More"
    # overflow menu. Try the direct button first, then the overflow.
    if not _click_connect(page):
        return {"success": False, "error": "Connect button not found (already connected or pending?)"}

    # Modal: "Add a note" → note textarea → Send.
    add_note = page.get_by_role("button", name="Add a note")
    try:
        if add_note and add_note.count() > 0:
            add_note.first.click()
    except Exception:
        pass

    box = page.locator("textarea#custom-message, textarea[name='message']").first
    if box.count() == 0:
        box = page.get_by_role("textbox").first
    if box.count() == 0:
        return {"success": False, "error": "note textarea not found"}
    from linkedin.browser.humanize import type_humanly
    type_humanly(box, note)

    send = page.get_by_role("button", name="Send")
    if send.count() == 0:
        send = page.get_by_role("button", name="Send invitation")
    if send.count() == 0:
        return {"success": False, "error": "Send button not found"}
    send.first.click()
    logger.info("Sent connection request WITH note to %s", profile.get("public_identifier"))
    return {"success": True}


def _click_connect(page) -> bool:
    direct = page.get_by_role("button", name="Connect")
    try:
        if direct and direct.count() > 0:
            direct.first.click()
            return True
    except Exception:
        pass
    # Behind the "More actions" overflow menu.
    try:
        more = page.get_by_role("button", name="More actions")
        if more and more.count() > 0:
            more.first.click()
            page.wait_for_timeout(500)
            item = page.get_by_role("menuitem", name="Connect")
            if item and item.count() > 0:
                item.first.click()
                return True
    except Exception:
        pass
    return False
