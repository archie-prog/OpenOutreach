# linkedin/leads/importer.py
"""Manual lead import — CSV files and LinkedIn people-search URLs.

The manual counterpart to AI auto-discovery: leads are created and attached
to a ``LeadList``. Dedup is by ``public_identifier`` (derived from the
LinkedIn URL), both within the input and against existing ``Lead`` rows.
"""
from __future__ import annotations

import csv as _csv
import logging
from typing import IO
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from linkedin_cli.url_utils import public_id_to_url, url_to_public_id

logger = logging.getLogger(__name__)

# LinkedIn People search exposes ~100 pages of ~10 results; 1000 is the ceiling.
SEARCH_IMPORT_CAP = 1000

CSV_REQUIRED_COLUMN = "linkedin_url"


def log_event(lead_list, role, text, **meta):
    """Append one entry to a lead list's activity thread (the "AI chat")."""
    from linkedin.models import LeadListEvent

    return LeadListEvent.objects.create(
        lead_list=lead_list, role=role, text=text, meta=meta or {},
    )


def create_lead_list(name, owner, source_type, source_url=None):
    """Create and return a new LeadList."""
    from linkedin.models import LeadList

    return LeadList.objects.create(
        name=name,
        owner=owner,
        source_type=source_type,
        source_url=source_url or None,
    )


def import_csv(lead_list, fileobj: IO[str]) -> dict:
    """Create Leads from CSV rows and attach them to ``lead_list``.

    Required column: ``linkedin_url``. Dedup by ``public_identifier`` against
    both the file itself and existing Leads. Optional name/company/title/notes
    columns are accepted but not persisted (no Lead fields for them yet).
    Returns ``{"created", "skipped", "invalid"}``.
    """
    from crm.models import Lead

    reader = _csv.DictReader(fileobj)
    if reader.fieldnames is None or CSV_REQUIRED_COLUMN not in reader.fieldnames:
        raise ValueError(f"CSV must have a '{CSV_REQUIRED_COLUMN}' column")

    created = skipped = invalid = 0
    seen: set[str] = set()
    for row in reader:
        url = (row.get(CSV_REQUIRED_COLUMN) or "").strip()
        public_id = url_to_public_id(url) if url else None
        if not public_id:
            invalid += 1
            continue
        if public_id in seen:
            skipped += 1
            continue
        seen.add(public_id)
        if Lead.objects.filter(public_identifier=public_id).exists():
            skipped += 1
            continue
        Lead.objects.create(
            linkedin_url=public_id_to_url(public_id),
            public_identifier=public_id,
            lead_list=lead_list,
            first_name=(row.get("first_name") or "").strip(),
            last_name=(row.get("last_name") or "").strip(),
            company=(row.get("company") or "").strip(),
        )
        created += 1

    logger.info(
        "CSV import into %s: created=%d skipped=%d invalid=%d",
        lead_list, created, skipped, invalid,
    )
    return {"created": created, "skipped": skipped, "invalid": invalid}


def _with_page(url: str, page: int) -> str:
    """Return ``url`` with its ``page`` query parameter set to ``page``."""
    parts = urlparse(url)
    query = parse_qs(parts.query, keep_blank_values=True)
    query["page"] = [str(page)]
    return urlunparse(parts._replace(query=urlencode(query, doseq=True)))


def scrape_search_url(session, url: str, cap: int = SEARCH_IMPORT_CAP) -> list[str]:
    """Return up to ``cap`` unique profile URLs from a people-search URL.

    Paginates via the ``page`` query parameter, reusing ``linkedin_cli``'s
    page navigation and ``/in/`` extraction. Stops when a page yields no new
    URLs or the cap is reached.
    """
    from linkedin_cli.browser.nav import extract_in_urls, goto_page

    session.ensure_browser()
    expected = urlparse(url).path.rstrip("/") or "/"
    collected: list[str] = []
    seen: set[str] = set()
    page_num = 1
    while len(collected) < cap:
        page_url = _with_page(url, page_num)
        goto_page(
            session,
            action=lambda u=page_url: session.page.goto(u),
            expected_url_pattern=expected,
            error_message="Failed to reach search results",
        )
        fresh = [u for u in extract_in_urls(session.page) if u not in seen]
        if not fresh:
            break
        for u in fresh:
            seen.add(u)
            collected.append(u)
            if len(collected) >= cap:
                break
        page_num += 1

    return collected[:cap]


def import_search_url(session, lead_list, url: str, cap: int = SEARCH_IMPORT_CAP) -> dict:
    """Scrape a people-search URL and enrich up to ``cap`` new leads into
    ``lead_list``. Idempotent: re-running the same URL skips existing leads.
    Returns ``{"created", "scraped"}``.
    """
    from linkedin.db.leads import create_enriched_lead, lead_exists
    from linkedin_cli.api.client import PlaywrightLinkedinAPI

    scraped = scrape_search_url(session, url, cap=cap)
    new_urls = [u for u in scraped if not lead_exists(u)][:cap]

    session.ensure_browser()
    api = PlaywrightLinkedinAPI(session=session)
    created = 0
    for profile_url in new_urls:
        try:
            profile, _raw = api.get_profile(profile_url=profile_url)
        except Exception:
            logger.warning("Voyager failed for %s — skipping", profile_url)
            continue
        if not profile:
            continue
        if create_enriched_lead(session, profile_url, profile, lead_list=lead_list) is not None:
            created += 1

    logger.info(
        "Search import into %s: created=%d (scraped=%d)", lead_list, created, len(scraped),
    )
    log_event(
        lead_list, "system",
        f"Scraped the saved LinkedIn search ({len(scraped)} profiles) and added "
        f"{created} new lead{'s' if created != 1 else ''} "
        f"({lead_list.leads.count()} in the list so far).",
        created=created, scraped=len(scraped),
    )
    return {"created": created, "scraped": len(scraped)}


def import_ai_search(session, lead_list, prompt: str, cap: int = 30, max_keywords: int = 4) -> dict:
    """AI lead finder (the original OpenOutreach discovery): the LLM turns an ICP
    description into LinkedIn search keywords, each is searched, and matching
    profiles are enriched into ``lead_list``. Returns ``{created, keywords}``.
    """
    from linkedin.db.leads import create_enriched_lead, lead_exists
    from linkedin.pipeline.search_keywords import generate_search_keywords
    from linkedin_cli.actions.search import search_people
    from linkedin_cli.api.client import PlaywrightLinkedinAPI

    keywords = (generate_search_keywords(product_docs=prompt, campaign_objective="") or [])[:max_keywords]
    session.ensure_browser()
    api = PlaywrightLinkedinAPI(session=session)
    created = 0
    # Hygiene: search 2nd/3rd-degree only ("S","O") — never 1st ("F"). The AI
    # finder must not pull in people you're already connected to (they'd only be
    # skipped by the connection-provenance guard at send time anyway, and they
    # clutter the list). Deliberately contacting existing connections is done via
    # an explicit lead list + the campaign's "include current network" toggle.
    for kw in keywords:
        if created >= cap:
            break
        for p in search_people(session, kw, network=["S", "O"]).get("profiles", []):
            if created >= cap:
                break
            url = p.get("url")
            if not url or lead_exists(url):
                continue
            try:
                profile, _raw = api.get_profile(profile_url=url)
            except Exception:
                continue
            if profile and create_enriched_lead(session, url, profile, lead_list=lead_list) is not None:
                created += 1

    logger.info("AI search into %s: created=%d keywords=%s", lead_list, created, keywords)
    if keywords:
        kw = ", ".join(keywords)
        summary = (
            f"Searched LinkedIn for: {kw}.\nAdded {created} new lead{'s' if created != 1 else ''}"
            f" ({lead_list.leads.count()} in the list so far)."
            if created else
            f"Searched LinkedIn for: {kw}.\nNo new leads this pass — this search looks exhausted."
        )
        log_event(lead_list, "ai", summary, keywords=keywords, created=created)
    return {"created": created, "keywords": keywords}


def backfill_lead_profiles(session, limit: int = 8) -> int:
    """Re-scrape leads missing a title and populate name/title/company/location,
    then reset their ai_score so they get re-ranked with real data. Returns count.
    """
    from crm.models import Lead

    done = 0
    for lead in Lead.objects.filter(title="", disqualified=False)[:limit]:
        try:
            profile = lead.get_profile(session)
        except Exception:
            continue
        if not profile:
            continue
        positions = profile.get("positions") or []
        company = positions[0].get("company_name", "") if positions and isinstance(positions[0], dict) else ""
        lead.title = profile.get("headline", "") or ""
        lead.company = company or lead.company
        lead.location = profile.get("location_name", "") or ""
        if not lead.first_name:
            lead.first_name = profile.get("first_name", "") or ""
        if not lead.last_name:
            lead.last_name = profile.get("last_name", "") or ""
        lead.ai_score = None  # re-rank with the new data
        lead.ai_reason = ""
        lead.save()
        done += 1
    return done


def process_pending_searches(session, cap: int = SEARCH_IMPORT_CAP) -> list:
    """Scrape/AI-find any lead lists queued via the dashboard. Clears the
    ``pending_search`` flag whether or not it succeeds. Returns
    ``[(list_id, created), ...]``.
    """
    from linkedin.models import LeadList

    results = []
    for ll in LeadList.objects.filter(pending_search=True, archived_at__isnull=True):
        target = ll.target_count or 30
        remaining = max(0, target - ll.leads.count())
        this_pass = min(cap, remaining) or cap
        created = 0
        try:
            if ll.source_type == LeadList.SourceType.AI:
                created = import_ai_search(session, ll, ll.source_url or "", cap=this_pass).get("created", 0)
            else:
                created = import_search_url(session, ll, ll.source_url or "", cap=this_pass).get("created", 0)
            results.append((ll.pk, created))
        except Exception:
            logger.exception("Pending search failed for list %s", ll.pk)
            results.append((ll.pk, 0))
        # Keep filling toward the target across cycles; stop when reached or when
        # a pass adds nothing new (the search is exhausted).
        ll.refresh_from_db(fields=["target_count"])
        ll.pending_search = bool(ll.leads.count() < (ll.target_count or 0) and created > 0)
        ll.save(update_fields=["pending_search"])
    return results
