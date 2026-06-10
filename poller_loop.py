import time
from linkedin.browser.registry import get_first_active_profile, get_or_create_session
from linkedin.inbox.poller import poll_replies
profile = get_first_active_profile()
session = get_or_create_session(profile)
print("POLLER_STARTED for", profile.linkedin_username, flush=True)
while True:
    try:
        n = poll_replies(session)
        print("polled ok, stopped=", n, flush=True)
    except Exception as e:
        print("poll error:", repr(e)[:150], flush=True)
    time.sleep(120)
