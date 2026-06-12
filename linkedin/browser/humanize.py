# linkedin/browser/humanize.py
"""Human-like browser behaviour so the kit's interactions don't read as automation:

* ``humanize_page`` — Bezier mouse path + scroll + dwell before an action (LinkedIn
  flags straight-line moves and "skips between profiles without reading").
* ``type_humanly`` — per-key **log-normal** delays (not uniform — uniform jitter is
  itself a flagged signal) with the occasional typo+correction.

All best-effort and never raise: texture must never break an action.
"""
from __future__ import annotations

import logging
import math
import random
import time

logger = logging.getLogger(__name__)


def _bezier_mouse(page, x2: float, y2: float) -> None:
    """Move the mouse to (x2,y2) along a cubic-Bezier path with jitter + variable
    velocity — humans don't move in straight, evenly-stepped lines."""
    x1, y1 = random.randint(0, 500), random.randint(0, 350)
    cx1 = x1 + (x2 - x1) * random.uniform(0.2, 0.5) + random.randint(-60, 60)
    cy1 = y1 + (y2 - y1) * random.uniform(0.2, 0.5) + random.randint(-60, 60)
    cx2 = x1 + (x2 - x1) * random.uniform(0.5, 0.8) + random.randint(-60, 60)
    cy2 = y1 + (y2 - y1) * random.uniform(0.5, 0.8) + random.randint(-60, 60)
    steps = random.randint(18, 34)
    for i in range(steps + 1):
        t = i / steps
        u = 1 - t
        bx = u*u*u*x1 + 3*u*u*t*cx1 + 3*u*t*t*cx2 + t*t*t*x2
        by = u*u*u*y1 + 3*u*u*t*cy1 + 3*u*t*t*cy2 + t*t*t*y2
        page.mouse.move(bx + random.uniform(-1, 1), by + random.uniform(-1, 1))
        # ease-in-out velocity: slower at the ends, faster mid-path
        time.sleep(random.uniform(0.004, 0.018) * (1.4 - math.sin(t * math.pi)))


def humanize_page(page, min_dwell: float = 3.0, max_dwell: float = 8.0) -> None:
    """Scroll as if reading, move the mouse on a curved path, then dwell."""
    try:
        for _ in range(random.randint(1, 3)):
            page.mouse.wheel(0, random.randint(250, 700))
            time.sleep(random.uniform(0.4, 1.2))
        if random.random() < 0.4:
            page.mouse.wheel(0, -random.randint(120, 350))
            time.sleep(random.uniform(0.3, 0.9))
        _bezier_mouse(page, random.randint(120, 760), random.randint(160, 520))
        time.sleep(random.uniform(min_dwell, max_dwell))  # read
    except Exception as exc:  # pragma: no cover - texture only
        logger.debug("humanize_page noop: %s", exc)


def _key_delay() -> float:
    """Log-normal inter-key delay: median ~0.12s, with a realistic long tail and
    the occasional think-pause. NOT uniform (FCaptcha-style detectors flag flat
    distributions)."""
    return min(1.4, random.lognormvariate(-2.1, 0.6))


def type_humanly(locator, text: str) -> None:
    """Type ``text`` into ``locator`` key-by-key with log-normal timing and a rare
    typo+backspace. Falls back to a plain fill on any error."""
    if not text:
        return
    try:
        try:
            locator.click()
        except Exception:
            pass
        for ch in text:
            if random.random() < 0.012 and ch.strip():
                try:
                    locator.type(random.choice("asdfghjkleiou"), delay=0)
                    time.sleep(_key_delay())
                    locator.press("Backspace")
                    time.sleep(_key_delay())
                except Exception:
                    pass
            locator.type(ch, delay=0)
            time.sleep(_key_delay())
            if ch == " " and random.random() < 0.05:  # occasional word-boundary pause
                time.sleep(random.uniform(0.15, 0.5))
    except Exception as exc:
        logger.debug("type_humanly fell back to fill: %s", exc)
        try:
            locator.fill(text)
        except Exception:
            pass
