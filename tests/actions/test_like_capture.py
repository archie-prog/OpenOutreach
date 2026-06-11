"""_capture_post_url resolves the real liked-post permalink (not the profile)."""
from unittest.mock import MagicMock

from linkedin.actions.like import _capture_post_url

FALLBACK = "https://www.linkedin.com/in/someone/recent-activity/all/"


def _node(urn):
    n = MagicMock()
    n.get_attribute.return_value = urn
    return n


def _make_like(data_urns, anchor_href=None):
    """Mock the Playwright Locator chain used by _capture_post_url."""
    step1 = MagicMock()
    step1.all.return_value = [_node(u) for u in data_urns]

    anchor = MagicMock()
    anchor.count.return_value = 1 if anchor_href else 0
    anchor.get_attribute.return_value = anchor_href
    card = MagicMock()
    card.locator.return_value.first = anchor
    step2 = MagicMock()
    step2.first = card

    like = MagicMock()
    # step 1 selector has no "[1]"; the card (step 2) selector ends with "[1]".
    like.locator.side_effect = lambda sel: step2 if "[1]" in sel else step1
    return like


def test_activity_urn_yields_canonical_permalink():
    like = _make_like(["urn:li:activity:7777"])
    assert _capture_post_url(None, like, FALLBACK) == \
        "https://www.linkedin.com/feed/update/urn:li:activity:7777/"


def test_composite_urn_extracts_the_activity_id():
    like = _make_like(["urn:li:fsd_socialDetail:(urn:li:activity:8888,FEED,)"])
    assert _capture_post_url(None, like, FALLBACK) == \
        "https://www.linkedin.com/feed/update/urn:li:activity:8888/"


def test_outermost_activity_preferred_over_inner_socialdetail():
    # .all() is nearest-first; reversed() checks the post-root (outermost) first.
    like = _make_like([
        "urn:li:fsd_socialDetail:(urn:li:activity:111,FEED,)",
        "urn:li:activity:222",
    ])
    assert _capture_post_url(None, like, FALLBACK) == \
        "https://www.linkedin.com/feed/update/urn:li:activity:222/"


def test_falls_back_to_permalink_anchor_when_no_data_urn():
    like = _make_like([], anchor_href="/feed/update/urn:li:activity:999/?utm=x")
    assert _capture_post_url(None, like, FALLBACK) == \
        "https://www.linkedin.com/feed/update/urn:li:activity:999/"


def test_degrades_to_recent_activity_when_nothing_resolvable():
    like = _make_like([])  # no data-urn ancestors, no anchor
    assert _capture_post_url(None, like, FALLBACK) == FALLBACK
