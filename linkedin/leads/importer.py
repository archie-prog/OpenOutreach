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
# LinkedIn stops serving results past ~100 pages — never page beyond this.
SEARCH_MAX_PAGES = 100

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


def scrape_search_url(session, url: str, cap: int = SEARCH_IMPORT_CAP,
                      skip=None, start_page: int = 1, abort_check=None) -> list[str]:
    """Return up to ``cap`` profile URLs from a people-search URL, paging through
    EVERY result page — the point is to categorically collect everyone, not just
    the first page-worth.

    Paginates via the ``page`` query parameter, reusing ``linkedin_cli``'s page
    navigation and ``/in/`` extraction.

    - ``skip``: optional predicate; URLs it returns truthy for are paged PAST but
      not collected (pass ``lead_exists`` so a resumed scrape skips already-imported
      people instead of re-collecting them).
    - ``start_page``: begin here (a resumed scrape jumps near where it left off
      rather than re-walking from page 1 every pass).
    - ``abort_check``: optional predicate polled between pages; truthy ends the
      pass early with what's collected so far (the worker passes the manual-send
      flag so a human's Unibox reply never waits behind a multi-minute scrape —
      the list stays pending and the scrape resumes next cycle).

    Stops only at ``cap`` collected, a page with no URLs we haven't already seen
    (the true end / LinkedIn's last-page clamp), the ~100-page ceiling, or an
    ``abort_check`` — NEVER on a page that merely contained no *new-to-us* people.
    """
    from linkedin_cli.browser.nav import extract_in_urls, goto_page

    skip = skip or (lambda u: False)
    session.ensure_browser()
    expected = urlparse(url).path.rstrip("/") or "/"
    collected: list[str] = []
    seen: set[str] = set()
    for page_num in range(max(1, start_page), SEARCH_MAX_PAGES + 1):
        if len(collected) >= cap:
            break
        if abort_check is not None and abort_check():
            break  # yield the browser — resume from here next pass
        page_url = _with_page(url, page_num)
        if page_num > max(1, start_page):  # human pause between pages — never a burst
            from linkedin.browser.session import random_sleep
            random_sleep(6, 12)
        goto_page(
            session,
            action=lambda u=page_url: session.page.goto(u),
            expected_url_pattern=expected,
            error_message="Failed to reach search results",
        )
        new_on_page = [u for u in extract_in_urls(session.page) if u not in seen]
        if not new_on_page:
            break  # nothing we haven't already seen on this page = past the last result
        for u in new_on_page:
            seen.add(u)
            if skip(u):
                continue  # already imported — page past it, keep looking for new people
            collected.append(u)
            if len(collected) >= cap:
                break

    return collected[:cap]


def import_search_url(session, lead_list, url: str, cap: int = SEARCH_IMPORT_CAP,
                      abort_check=None) -> dict:
    """Scrape a people-search URL and save EVERY person on it into ``lead_list``.

    Leads are created as skeleton rows (LinkedIn URL + public id) with NO
    per-profile Voyager fetch during the scrape — so a large search is captured
    quickly and with a light footprint (listing result pages, not opening every
    profile). Name/title/company are filled in afterwards by the background
    enrichment pass (``backfill_lead_profiles``), which paces the expensive
    per-profile calls and keeps the account safe.

    Idempotent + resumable: already-imported people are skipped and the scrape
    resumes near where the last pass stopped, so repeated paced passes march
    through the entire search rather than re-collecting the first page every time.
    Returns ``{"created", "scraped"}``.
    """
    from crm.models import Lead
    from linkedin.db.leads import lead_exists

    # Resume ~1 page behind where we left off (10 results/page) so no one is missed
    # if a prior pass stopped mid-page; scrape_search_url's skip=lead_exists dedupes
    # the small overlap.
    start_page = max(1, (lead_list.leads.count() // 10) - 1)
    scraped = scrape_search_url(session, url, cap=cap, skip=lead_exists,
                                start_page=start_page, abort_check=abort_check)

    created = 0
    for profile_url in scraped:
        public_id = url_to_public_id(profile_url)
        if not public_id or Lead.objects.filter(public_identifier=public_id).exists():
            continue
        Lead.objects.create(
            linkedin_url=public_id_to_url(public_id),
            public_identifier=public_id,
            lead_list=lead_list,
        )
        created += 1

    logger.info(
        "Search import into %s: created=%d skeleton leads (scraped=%d)",
        lead_list, created, len(scraped),
    )
    log_event(
        lead_list, "system",
        f"Scraped the saved LinkedIn search and added {created} new "
        f"lead{'s' if created != 1 else ''} ({lead_list.leads.count()} in the list so "
        f"far). Profile details fill in automatically as enrichment runs.",
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
    # Connection-degree filter (per-list, default "S" = 2nd-degree only). The AI
    # finder must never pull in 1st-degree ("F") — people you're already connected
    # to; they'd only be skipped by the provenance guard at send time and clutter
    # the list. 2nd-degree is the outreach sweet spot; 3rd ("O") is opt-in.
    network = [c for c in (getattr(lead_list, "search_network", "S") or "S").split(",") if c in ("F", "S", "O")] or ["S"]
    if "F" in network:
        network = [c for c in network if c != "F"]  # never source 1st-degree via AI
    for kw in keywords:
        if created >= cap:
            break
        for p in search_people(session, kw, network=network).get("profiles", []):
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


def process_pending_searches(session, cap: int = SEARCH_IMPORT_CAP, abort_check=None) -> list:
    """Scrape/AI-find any lead lists queued via the dashboard. Clears the
    ``pending_search`` flag whether or not it succeeds. Returns
    ``[(list_id, created), ...]``.

    ``abort_check``: truthy ends the work early (lists stay pending and resume
    next cycle) — the worker passes the manual-send flag so a human's reply
    never waits behind a long scrape.
    """
    from linkedin.models import LeadList

    results = []
    for ll in LeadList.objects.filter(pending_search=True, archived_at__isnull=True):
        if abort_check is not None and abort_check():
            break  # list stays pending_search=True — resumes next cycle
        is_ai = ll.source_type == LeadList.SourceType.AI
        if is_ai:
            # The AI finder samples and is expensive — keep it target-bounded.
            target = ll.target_count or 30
            remaining = max(0, target - ll.leads.count())
            this_pass = min(cap, remaining) or cap
        else:
            # A real search URL is imported EXHAUSTIVELY: grab up to `cap` new people
            # this pass and stay pending until a pass adds nothing new. target_count
            # is NOT a cap here — that was why URL lists stopped at ~30.
            this_pass = cap
        created = 0
        try:
            if is_ai:
                created = import_ai_search(session, ll, ll.source_url or "", cap=this_pass).get("created", 0)
            else:
                created = import_search_url(session, ll, ll.source_url or "", cap=this_pass,
                                            abort_check=abort_check).get("created", 0)
            results.append((ll.pk, created))
        except Exception:
            logger.exception("Pending search failed for list %s", ll.pk)
            results.append((ll.pk, 0))
        # Keep going across cycles; stop when a pass adds nothing new (exhausted),
        # or — for the AI finder — when its target_count is reached. An ABORTED
        # pass proves nothing about exhaustion: stay pending and resume.
        aborted = abort_check is not None and abort_check()
        ll.refresh_from_db(fields=["target_count"])
        if is_ai:
            ll.pending_search = bool(ll.leads.count() < (ll.target_count or 0) and created > 0)
        else:
            ll.pending_search = created > 0 or aborted
        ll.save(update_fields=["pending_search"])
    return results
