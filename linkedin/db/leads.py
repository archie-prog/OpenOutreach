import logging
from typing import Dict, Any, Optional

from django.db import transaction

from linkedin_cli.url_utils import url_to_public_id, public_id_to_url

logger = logging.getLogger(__name__)


def lead_exists(url: str) -> bool:
    """Check if Lead already exists for this LinkedIn URL."""
    from crm.models import Lead

    pid = url_to_public_id(url)
    if not pid:
        return False
    return Lead.objects.filter(public_identifier=pid).exists()


def create_enriched_lead(session, url: str, profile: Dict[str, Any], lead_list=None) -> Optional[int]:
    """Create Lead with full profile data and embedding.

    Returns lead PK or None if exists.
    Does NOT create Deal — that comes at qualification.
    ``lead_list`` optionally attaches the Lead to a manual-import LeadList.
    """
    from crm.models import Lead

    # Use canonical public_identifier from Voyager response when available.
    canonical_pid = profile.get("public_identifier")
    public_id = canonical_pid or url_to_public_id(url)
    clean_url = public_id_to_url(public_id)

    urn = profile.get("urn") or None

    with transaction.atomic():
        if Lead.objects.filter(public_identifier=public_id).exists():
            return None
        if urn and Lead.objects.filter(urn=urn).exists():
            logger.info(
                "Lead with URN %s already exists — skipping duplicate %s",
                urn, public_id,
            )
            return None
        positions = profile.get("positions") or []
        company = ""
        if positions and isinstance(positions[0], dict):
            company = positions[0].get("company_name", "") or ""
        lead = Lead.objects.create(
            linkedin_url=clean_url, public_identifier=public_id, lead_list=lead_list,
            first_name=profile.get("first_name", "") or "",
            last_name=profile.get("last_name", "") or "",
            company=company,
            title=profile.get("headline", "") or "",
            location=profile.get("location_name", "") or "",
        )
        _cache_urn_from_profile(lead, profile)

    lead.embed_from_profile(profile)

    logger.debug("Created enriched lead for %s (pk=%d)", public_id, lead.pk)
    return lead.pk


def _cache_urn_from_profile(lead, profile: Dict[str, Any]):
    """Promote ``profile['urn']`` onto the Lead row if not already cached.

    The only durable field we extract from a fresh scrape — everything
    else lives in memory for the lifetime of the caller's dict.
    """
    urn = profile.get("urn") or None
    if urn and lead.urn != urn:
        lead.urn = urn
        lead.save(update_fields=["urn"])


def register_self_lead(session, profile: Dict[str, Any]):
    """Persist the logged-in member's own profile as a disqualified Lead.

    The CRM-side layer over ``linkedin_cli``'s self-discovery primitive: marks
    the real profile disqualified (so auto-discovery never targets it) and links
    it as ``linkedin_profile.self_lead``. Idempotent per profile.
    """
    from crm.models import Lead

    public_id = profile["public_identifier"]
    lead, _ = Lead.objects.update_or_create(
        public_identifier=public_id,
        defaults={"linkedin_url": public_id_to_url(public_id), "disqualified": True},
    )
    _cache_urn_from_profile(lead, profile)

    session.linkedin_profile.self_lead = lead
    session.linkedin_profile.save(update_fields=["self_lead"])
    logger.info("Registered self-profile as disqualified Lead: %s", public_id)
