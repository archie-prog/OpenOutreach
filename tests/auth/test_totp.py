# tests/auth/test_totp.py
"""Native TOTP 2FA (RFC 6238) + auto-fill login."""
from __future__ import annotations

import pytest

# RFC 6238 SHA-1 test vector: secret = base32("12345678901234567890").
RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"


class TestCurrentTotp:
    def test_rfc6238_vector(self):
        from linkedin.auth.totp import current_totp

        # At t=59s the RFC's 8-digit TOTP is 94287082 → 6-digit tail 287082.
        assert current_totp(RFC_SECRET, t=59) == "287082"

    def test_spaces_and_lowercase_and_padding(self):
        from linkedin.auth.totp import current_totp

        spaced = "gezd gnbv gy3t qojq gezd gnbv gy3t qojq"
        assert current_totp(spaced, t=59) == "287082"

    def test_code_is_six_digits(self):
        from linkedin.auth.totp import current_totp

        code = current_totp(RFC_SECRET, t=1234567890)
        assert code.isdigit() and len(code) == 6


# ── Fake Playwright page/session that simulates navigation ──────────────
#
# The whole point of the live bug was that the previous test froze the page on
# the challenge URL, so it never exercised the "click → navigate → read URL"
# sequence. This fake page advances its URL in response to clicks, the way the
# real page does, so the test catches a regression to synchronous URL reads.


class _FakeLocator:
    def __init__(self, page, kind):
        self.page = page
        self.kind = kind
        self._checked = False

    @property
    def last(self):
        return self

    @property
    def first(self):
        return self

    def count(self):
        return 1 if self.kind == "checkbox" else 0

    def is_checked(self):
        return self._checked

    def check(self):
        self._checked = True
        self.page.remembered = True

    def fill(self, value):
        self.page.filled.append((self.kind, value))

    def type(self, text, delay=None):
        # The login path types via linkedin_cli.human_type, which calls
        # locator.type(text, delay=...). Record it keyed by the locator's selector.
        self.page.filled.append((self.kind, text))

    def click(self):
        self.page.clicked.append(self.kind)
        self.page.on_click(self.kind)


class _FakePage:
    """A page that transitions URL on Sign in / Submit, scripted per scenario."""

    def __init__(self, *, after_signin, after_submit):
        self.url = "about:blank"
        self.filled = []
        self.clicked = []
        self.remembered = False
        self._after_signin = after_signin            # url after clicking Sign in
        self._after_submit = list(after_submit)       # url(s) after each Submit
        self._submit_idx = 0

    # — navigation —
    def goto(self, url, **kw):
        self.url = url

    def wait_for_load_state(self, state, timeout=None):
        pass

    def wait_for_timeout(self, ms):
        pass

    # — interaction —
    def fill(self, selector, value):
        self.filled.append((selector, value))

    def locator(self, selector):
        # login.py now types into locators (human_type(page.locator("#username"), …))
        # rather than calling page.fill(selector, …). The fake locator records the
        # typed text under its selector so the credential assertions still hold.
        return _FakeLocator(self, selector)

    def get_by_role(self, role, name=None):
        if role == "button":
            return _FakeLocator(self, name or "button")
        if role == "checkbox":
            return _FakeLocator(self, "checkbox")
        return _FakeLocator(self, "textbox")

    def on_click(self, kind):
        if kind == "Sign in":
            self.url = self._after_signin
        elif kind == "Submit":
            nxt = self._after_submit[min(self._submit_idx, len(self._after_submit) - 1)]
            self._submit_idx += 1
            self.url = nxt


class _FakeSession:
    def __init__(self, page):
        self.page = page

    def ensure_browser(self):
        pass


CHALLENGE = "https://www.linkedin.com/checkpoint/challenge/AbC"
FEED = "https://www.linkedin.com/feed/"
LOGIN = "https://www.linkedin.com/login"


class TestLoginWithTotp:
    def test_fills_credentials_and_clears_challenge(self):
        from linkedin.auth.login import login_with_totp

        page = _FakePage(after_signin=CHALLENGE, after_submit=[FEED])
        login_with_totp(_FakeSession(page), "user@example.com", "pw", RFC_SECRET)

        assert ("#username", "user@example.com") in page.filled
        assert ("#password", "pw") in page.filled
        # A 6-digit code was filled on the challenge page, and we ended on the feed.
        assert any(isinstance(v, str) and v.isdigit() and len(v) == 6 for _, v in page.filled)
        assert "/feed" in page.url
        assert page.remembered is True  # ticked "remember this device"

    def test_no_code_when_no_2fa(self):
        from linkedin.auth.login import login_with_totp

        page = _FakePage(after_signin=FEED, after_submit=[FEED])
        login_with_totp(_FakeSession(page), "user@example.com", "pw", RFC_SECRET)
        assert not any(isinstance(v, str) and v.isdigit() and len(v) == 6 for _, v in page.filled)
        assert "/feed" in page.url

    def test_retries_with_fresh_code_then_succeeds(self):
        from linkedin.auth.login import login_with_totp

        # First submit stays on the challenge (expired code), second clears it.
        page = _FakePage(after_signin=CHALLENGE, after_submit=[CHALLENGE, FEED])
        login_with_totp(_FakeSession(page), "user@example.com", "pw", RFC_SECRET)
        assert page.clicked.count("Submit") == 2
        assert "/feed" in page.url

    def test_raises_when_challenge_never_clears(self):
        from linkedin.auth.login import login_with_totp, TwoFactorLoginError

        page = _FakePage(after_signin=CHALLENGE, after_submit=[CHALLENGE])
        with pytest.raises(TwoFactorLoginError):
            login_with_totp(_FakeSession(page), "user@example.com", "pw", RFC_SECRET)

    def test_raises_on_rejected_credentials(self):
        from linkedin.auth.login import login_with_totp, TwoFactorLoginError

        # Bad password: never leaves /login.
        page = _FakePage(after_signin=LOGIN, after_submit=[LOGIN])
        with pytest.raises(TwoFactorLoginError):
            login_with_totp(_FakeSession(page), "user@example.com", "bad", RFC_SECRET)
