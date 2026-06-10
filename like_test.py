from linkedin.browser.registry import get_first_active_profile, get_or_create_session
from linkedin.actions.like import like_most_recent_post
from crm.models import Lead
profile = get_first_active_profile()
session = get_or_create_session(profile)
print("LOGGED_IN_AS:", profile.linkedin_username)
lead, _ = Lead.objects.get_or_create(public_identifier="toby-claxton", defaults={"linkedin_url": "https://www.linkedin.com/in/toby-claxton/"})
print("LIKE_RESULT:", like_most_recent_post(session, lead))
