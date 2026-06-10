from linkedin.browser.registry import get_first_active_profile, get_or_create_session
from linkedin.leads.importer import process_pending_searches, scrape_search_url
from linkedin.models import LeadList
print("PENDING_BEFORE", list(LeadList.objects.filter(pending_search=True).values_list("pk", flat=True)), flush=True)
profile = get_first_active_profile()
session = get_or_create_session(profile)
ll = LeadList.objects.get(pk=2)
urls = scrape_search_url(session, ll.source_url, cap=25)
print("SCRAPED_URLS", len(urls), urls[:3], flush=True)
r = process_pending_searches(session)
print("RESULT", r, flush=True)
print("LEADS_AFTER", LeadList.objects.get(pk=2).leads.count(), flush=True)
