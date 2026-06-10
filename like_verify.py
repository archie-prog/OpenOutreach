from linkedin.browser.registry import get_first_active_profile, get_or_create_session
from linkedin.actions.like import like_most_recent_post
from crm.models import Lead
profile = get_first_active_profile()
session = get_or_create_session(profile)
lead = Lead.objects.get(public_identifier="toby-claxton")
print("LIKE_VERIFY", like_most_recent_post(session, lead), flush=True)
