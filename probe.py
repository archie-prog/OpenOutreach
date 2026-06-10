from linkedin.browser.registry import get_first_active_profile, get_or_create_session
from linkedin_cli.api.client import PlaywrightLinkedinAPI
s = get_first_active_profile()
sess = get_or_create_session(s)
sess.ensure_browser()
api = PlaywrightLinkedinAPI(session=sess)
profile, _ = api.get_profile(public_identifier="toby-claxton")
print("KEYS", sorted((profile or {}).keys()), flush=True)
print("headline=", (profile or {}).get("headline"), flush=True)
print("location_name=", (profile or {}).get("location_name"), flush=True)
print("positions_type=", type((profile or {}).get("positions")), str((profile or {}).get("positions"))[:200], flush=True)
