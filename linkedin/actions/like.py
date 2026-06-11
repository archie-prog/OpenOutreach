# linkedin/actions/like.py
"""Like a profile's most recent post.

No ``linkedin_cli`` primitive exists, so this is an app-side Playwright flow:
open the lead's recent activity, find the latest post's Like button, click it.
Idempotent — if it's already liked, it reports success without re-clicking.
Never raises: failures are captured in the result.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# urn:li:activity:7298…  (also pulls the activity id out of a composite urn such
# as urn:li:fsd_socialDetail:(urn:li:activity:7298…,…) ).
_ACTIVITY_RE = re.compile(r"urn:li:activity:(\d+)")


def _capture_post_url(page, like, fallback: str) -> str:
    """Resolve the permalink of the post the Like button belongs to.

    Tries, in order: an ancestor's data-urn carrying urn:li:activity:<id>; the
    card's timestamp/permalink anchor (/feed/update/ or /posts/). Falls back to
    the lead's recent-activity page. Never raises.
    """
    # 1) Post-container ancestor whose data-urn holds an activity id. Scan the
    #    data-urn ancestors outermost-first — the closest is often a
    #    socialDetail/reactions sub-node; the activity-bearing one is higher up.
    try:
        urns = like.locator("xpath=ancestor::*[@data-urn]").all()
        for node in reversed(urns):  # outermost (post root) first
            m = _ACTIVITY_RE.search(node.get_attribute("data-urn") or "")
            if m:
                return f"https://www.linkedin.com/feed/update/urn:li:activity:{m.group(1)}/"
    except Exception:
        pass

    # 2) The card's own permalink (timestamp/menu anchor links to the post).
    try:
        card = like.locator(
            "xpath=ancestor::*[contains(@class,'feed-shared-update-v2') or @data-urn][1]"
        ).first
        anchor = card.locator("a[href*='/feed/update/'], a[href*='/posts/']").first
        if anchor.count():
            href = anchor.get_attribute("href") or ""
            if href.startswith("/"):
                href = "https://www.linkedin.com" + href
            if href.startswith("http"):
                return href.split("?")[0]
    except Exception:
        pass

    # 3) Couldn't resolve the post — link to where the like happened.
    return fallback


def like_most_recent_post(session, lead) -> dict:
    try:
        return _like(session, lead)
    except Exception as exc:  # brittle UI flow — never crash the sequence
        logger.exception("Like most recent post failed for %s", lead)
        return {"success": False, "error": str(exc)}


def _like(session, lead) -> dict:
    session.ensure_browser()
    page = session.page
    url = f"https://www.linkedin.com/in/{lead.public_identifier}/recent-activity/all/"
    page.goto(url, wait_until="domcontentloaded")
    try:
        page.wait_for_timeout(2500)
    except Exception:
        pass

    like = page.locator('button[aria-label*="React Like"], button[aria-label="Like"]').first
    if like.count() == 0:
        like = page.get_by_role("button", name="Like").first
    if like.count() == 0:
        return {"success": False, "error": "no Like button found (no recent posts?)"}

    # Capture the real permalink of THIS post so the dashboard's "View liked post"
    # opens the post itself, not the profile's activity page.
    post_url = _capture_post_url(page, like, fallback=url)

    if (like.get_attribute("aria-pressed") or "").lower() == "true":
        logger.info("Most recent post already liked for %s", lead.public_identifier)
        return {"success": True, "already_liked": True, "post_url": post_url}

    like.click()
    logger.info("Liked most recent post for %s", lead.public_identifier)
    return {"success": True, "already_liked": False, "post_url": post_url}
