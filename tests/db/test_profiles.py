# tests/db/test_profiles.py
"""URL helpers + live lead import/enrichment (db.leads). The legacy Deal/
qualification-pipeline tests were removed with that dead code."""
import pytest

from linkedin.db.leads import create_enriched_lead, lead_exists
from linkedin_cli.url_utils import url_to_public_id, public_id_to_url


# ── url_to_public_id (pure function) ──

class TestUrlToPublicId:
    def test_standard_url(self):
        assert url_to_public_id("https://www.linkedin.com/in/johndoe/") == "johndoe"

    def test_url_without_trailing_slash(self):
        assert url_to_public_id("https://www.linkedin.com/in/johndoe") == "johndoe"

    def test_url_with_query_params(self):
        assert url_to_public_id("https://www.linkedin.com/in/johndoe?foo=bar") == "johndoe"

    def test_url_with_extra_path_segments(self):
        assert url_to_public_id("https://www.linkedin.com/in/johndoe/detail/contact-info/") == "johndoe"

    def test_percent_encoded_id(self):
        assert url_to_public_id("https://www.linkedin.com/in/john%20doe/") == "john doe"

    def test_empty_url_returns_none(self):
        assert url_to_public_id("") is None

    def test_non_profile_url_returns_none(self):
        assert url_to_public_id("https://www.linkedin.com/feed/") is None

    def test_only_domain_returns_none(self):
        assert url_to_public_id("https://www.linkedin.com/") is None


# ── public_id_to_url (pure function) ──

class TestPublicIdToUrl:
    def test_standard_id(self):
        assert public_id_to_url("johndoe") == "https://www.linkedin.com/in/johndoe/"

    def test_empty_id(self):
        assert public_id_to_url("") == ""

    def test_id_with_slashes_stripped(self):
        assert public_id_to_url("/johndoe/") == "https://www.linkedin.com/in/johndoe/"


# ── live lead import/enrichment (db.leads) ──

SAMPLE_PROFILE = {
    "first_name": "Alice",
    "last_name": "Smith",
    "headline": "Engineer",
    "positions": [{"company_name": "Acme"}],
    "urn": "urn:li:fsd_profile:ABC123",
}


@pytest.mark.django_db
class TestLeadExists:
    def test_exists_after_create(self, fake_session):
        create_enriched_lead(fake_session, "https://www.linkedin.com/in/alice/", SAMPLE_PROFILE)
        assert lead_exists("https://www.linkedin.com/in/alice/") is True

    def test_not_exists(self, fake_session):
        assert lead_exists("https://www.linkedin.com/in/nobody/") is False

    def test_invalid_url(self, fake_session):
        assert lead_exists("https://linkedin.com/feed/") is False


@pytest.mark.django_db
class TestCreateEnrichedLead:
    def test_creates_lead_and_caches_urn(self, fake_session):
        from crm.models import Lead
        pk = create_enriched_lead(fake_session, "https://www.linkedin.com/in/alice/", SAMPLE_PROFILE)
        assert pk is not None
        lead = Lead.objects.get(linkedin_url="https://www.linkedin.com/in/alice/")
        assert lead.public_identifier == "alice"
        assert lead.urn == "urn:li:fsd_profile:ABC123"

    def test_persists_embedding(self, fake_session):
        from crm.models import Lead
        create_enriched_lead(fake_session, "https://www.linkedin.com/in/alice/", SAMPLE_PROFILE)
        lead = Lead.objects.get(linkedin_url="https://www.linkedin.com/in/alice/")
        assert lead.embedding is not None

    def test_returns_none_for_duplicate(self, fake_session):
        create_enriched_lead(fake_session, "https://www.linkedin.com/in/alice/", SAMPLE_PROFILE)
        pk2 = create_enriched_lead(fake_session, "https://www.linkedin.com/in/alice/", SAMPLE_PROFILE)
        assert pk2 is None
