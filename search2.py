from linkedin.browser.registry import get_first_active_profile, get_or_create_session
from linkedin.leads.importer import scrape_search_url, import_search_url
from linkedin.models import LeadList
profile = get_first_active_profile()
session = get_or_create_session(profile)
ll = LeadList.objects.get(pk=2)
print("URL", ll.source_url, flush=True)
urls = scrape_search_url(session, ll.source_url, cap=5)
print("SCRAPED_URLS", len(urls), flush=True)
for u in urls[:5]:
    print("   FOUND", u, flush=True)
r = import_search_url(session, ll, ll.source_url, cap=5)
ll.pending_search = False; ll.save()
print("IMPORT_RESULT", r, flush=True)
print("LIST_LEADS", ll.leads.count(), flush=True)
