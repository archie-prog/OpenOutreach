# linkedin/actions/like.py
"""Like a profile's most recent post.

No ``linkedin_cli`` primitive exists, so this is an app-side Playwright flow:
resolve the lead's most-recent post permalink, open it, and click the reaction
button. The click is **verified** — we re-read the reaction-button label until it
shows we've reacted, and only then report success; a click that doesn't register
returns ``success=False`` (so the daemon never logs a like that didn't happen).
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

# Current LinkedIn markup: a post's reaction control is
# ``<button aria-label="Reaction button state: ...">`` whose label encodes the
# CURRENT reaction — "no reaction" (not liked) vs "Like"/"Liked"/another reaction
# (we've reacted). There is NO aria-pressed attribute. The legacy
# ``button[aria-label*="React Like"]`` markup now survives only on *comment* like
# buttons, so matching it likes a comment (or nothing), never the post — that was
# the false-success bug where the action reported success without liking.
_REACTION_BTN = 'button[aria-label^="Reaction button state:"]'


def _reaction_label(page) -> str | None:
    btn = page.locator(_REACTION_BTN).first
    if btn.count() == 0:
        return None
    return btn.get_attribute("aria-label") or ""


def _label_is_liked(label: str | None) -> bool:
    """True when the reaction-button label shows we've reacted to the post."""
    label = (label or "").strip().lower()
    return bool(label) and "no reaction" not in label


# LinkedIn serves TWO reaction-button markups (varies by account/A-B/page):
#   (a) new: <button aria-label="Reaction button state: no reaction|Like|...">  (state in label)
#   (b) old: <button aria-label="React Like to <name>'s post" aria-pressed=...>  (state in aria-pressed)
# The like action must handle both, or it silently no-ops on the variant it
# doesn't recognise.
_REACTION_SEL = 'button[aria-label^="Reaction button state:"], button[aria-label*="React Like"]'


def _post_reaction_button(page):
    """The POST's reaction button (not a comment's), in whichever markup is live.
    Returns a locator or None."""
    b = page.locator('button[aria-label^="Reaction button state:"]').first
    if b.count():
        return b
    btns = page.locator('button[aria-label*="React Like"]')
    for i in range(btns.count()):
        bi = btns.nth(i)
        if "comment" in (bi.get_attribute("aria-label") or "").lower():
            continue  # skip comment reaction buttons
        return bi
    return None


def _btn_is_liked(btn) -> bool:
    """Whether the located reaction button shows we've reacted — both markups."""
    try:
        lab = (btn.get_attribute("aria-label") or "").strip().lower()
    except Exception:
        lab = ""
    if lab.startswith("reaction button state:"):
        return "no reaction" not in lab
    return (btn.get_attribute("aria-pressed") or "").lower() == "true"


def _latest_post_url(page, fallback: str) -> str:
    """Permalink of the member's most recent activity (top container's data-urn).
    Falls back to the recent-activity page when no activity urn is present."""
    try:
        post = page.locator('div[data-urn*="urn:li:activity"]').first
        if post.count():
            m = _ACTIVITY_RE.search(post.get_attribute("data-urn") or "")
            if m:
                return f"https://www.linkedin.com/feed/update/urn:li:activity:{m.group(1)}/"
    except Exception:
        pass
    return fallback


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
    activity_url = f"https://www.linkedin.com/in/{lead.public_identifier}/recent-activity/all/"
    page.goto(activity_url, wait_until="domcontentloaded")
    # The recent-activity feed hydrates lazily — wait for the first activity
    # container to render rather than racing a fixed sleep (else we fall back to
    # the activity page, which has no post reaction button).
    try:
        page.wait_for_selector('div[data-urn*="urn:li:activity"]', timeout=10000)
    except Exception:
        pass

    # Act on the single-post PERMALINK, where the reaction control is unambiguous
    # (the recent-activity feed interleaves posts with comment activity, so a bare
    # match there can land on the wrong card). The permalink is also the real
    # "View liked post" target for the dashboard.
    post_url = _latest_post_url(page, fallback=activity_url)
    if "/feed/update/" in post_url:
        page.goto(post_url, wait_until="domcontentloaded")
        try:
            page.wait_for_selector(_REACTION_SEL, timeout=10000)
        except Exception:
            pass

    from linkedin.browser.humanize import humanize_page
    humanize_page(page)
    btn = _post_reaction_button(page)
    if btn is None:
        return {"success": False, "error": "no reaction button found (no recent post?)", "post_url": post_url}

    if _btn_is_liked(btn):
        logger.info("Most recent post already liked for %s", lead.public_identifier)
        return {"success": True, "already_liked": True, "post_url": post_url}

    try:
        btn.scroll_into_view_if_needed()
    except Exception:
        pass
    btn.click()

    # VERIFY the like registered — re-locate the button and confirm the liked
    # state flipped (handles both markups). Never report success on a no-op click.
    for _ in range(10):
        try:
            page.wait_for_timeout(500)
        except Exception:
            pass
        b2 = _post_reaction_button(page)
        if b2 is not None and _btn_is_liked(b2):
            logger.info("Liked most recent post for %s", lead.public_identifier)
            return {"success": True, "already_liked": False, "post_url": post_url}

    return {
        "success": False,
        "error": "clicked Like but the reaction did not register",
        "post_url": post_url,
    }
