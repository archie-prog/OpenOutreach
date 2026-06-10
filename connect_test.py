import traceback
from linkedin.browser.registry import get_first_active_profile, get_or_create_session
from linkedin_cli.actions.connect import send_connection_request
from linkedin_cli.actions.status import get_connection_status
from crm.models import Lead
profile = get_first_active_profile()
session = get_or_create_session(profile)
session.ensure_browser()
lead = Lead.objects.get(public_identifier="jess-mcallister-0b2330143")
pdict = {"public_identifier": lead.public_identifier, "url": lead.linkedin_url, "urn": lead.urn or ""}
try:
    status = get_connection_status(session, lead.public_identifier)
    print("STATUS", status, flush=True)
    result = send_connection_request(session, pdict)
    print("CONNECT_RESULT", result, flush=True)
except Exception:
    print("CONNECT_ERROR", flush=True)
    traceback.print_exc()
