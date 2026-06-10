from linkedin.browser.registry import get_first_active_profile, get_or_create_session
from linkedin_cli.actions.message import send_raw_message
from linkedin.sequences.executor import render_template
from crm.models import Lead

TARGETS = [
    ("toby-claxton", "https://www.linkedin.com/in/toby-claxton/"),
    ("josh-s-young", "https://www.linkedin.com/in/josh-s-young/"),
]
TEMPLATE = "Hi {first_name}, testing personalisation from the new outreach setup - please ignore!"

profile = get_first_active_profile()
session = get_or_create_session(profile)
print("LOGGED_IN_AS:", profile.linkedin_username)

for pid, url in TARGETS:
    lead, _ = Lead.objects.get_or_create(public_identifier=pid, defaults={"linkedin_url": url})
    prof = lead.get_profile(session)
    fn = (prof or {}).get("first_name", "") or ""
    if fn:
        lead.first_name = fn
        lead.save(update_fields=["first_name"])
    ctx = {"first_name": lead.first_name or "", "last_name": lead.last_name or "", "company": lead.company or "", "public_identifier": pid}
    body = render_template(TEMPLATE, ctx, "Hi there, testing - please ignore!")
    print("RENDERED", pid, "->", body)
    urn = lead.urn or lead.get_urn(session)
    try:
        ok = send_raw_message(session, {"public_identifier": pid, "url": url, "urn": urn}, body)
        print("SEND_RESULT", pid, "->", ok)
    except Exception as e:
        print("SEND_FAIL", pid, repr(e)[:200])
