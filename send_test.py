from linkedin.browser.registry import get_first_active_profile, get_or_create_session
from linkedin_cli.actions.message import send_raw_message
from crm.models import Lead

TARGETS = [
    ("toby-claxton", "https://www.linkedin.com/in/toby-claxton/", "Toby"),
    ("josh-s-young", "https://www.linkedin.com/in/josh-s-young/", "Josh"),
]

profile = get_first_active_profile()
session = get_or_create_session(profile)
print("LOGGED_IN_AS:", profile.linkedin_username)

for pid, url, name in TARGETS:
    lead, _ = Lead.objects.get_or_create(public_identifier=pid, defaults={"linkedin_url": url})
    try:
        urn = lead.get_urn(session)
    except Exception as e:
        print("URN_FAIL", pid, repr(e)[:200])
        continue
    pdict = {"public_identifier": pid, "url": url, "urn": urn}
    msg = "Hi " + name + " - quick automated test from the new outreach setup, please ignore."
    try:
        ok = send_raw_message(session, pdict, msg)
        print("SEND_RESULT", pid, "->", ok)
    except Exception as e:
        print("SEND_FAIL", pid, repr(e)[:200])
