# linkedin/browser/humanize.py
"""Human-like page behaviour before actions — scroll, dwell, small mouse moves —
so the kit doesn't jump straight from opening a profile to acting on it (a
navigation-pattern signal LinkedIn flags as "skips between profiles without
reading"). Best-effort and never raises: humanising must never break an action.
"""
from __future__ import annotations

import logging
import random
import time

logger = logging.getLogger(__name__)


def humanize_page(page, min_dwell: float = 3.0, max_dwell: float = 8.0) -> None:
    """Scroll a little as if reading, nudge the mouse, then dwell. No-op on error."""
    try:
        for _ in range(random.randint(1, 3)):
            page.mouse.wheel(0, random.randint(250, 700))
            time.sleep(random.uniform(0.4, 1.2))
        if random.random() < 0.4:  # sometimes scroll back up a touch
            page.mouse.wheel(0, -random.randint(120, 350))
            time.sleep(random.uniform(0.3, 0.9))
        page.mouse.move(
            random.randint(80, 700), random.randint(120, 500),
            steps=random.randint(3, 12),
        )
        time.sleep(random.uniform(min_dwell, max_dwell))  # read
    except Exception as exc:  # pragma: no cover - texture only
        logger.debug("humanize_page noop: %s", exc)
