# tests/api/test_search_scrape.py
"""Search-URL lead import (M1) — scrape mocked, Voyager mocked."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from tests.factories import UserFactory

SEARCH_URL = "https://www.linkedin.com/search/results/people/?keywords=founder"

PROFILES = {
    "https://www.linkedin.com/in/alice/": {"public_identifier": "alice", "urn": "urn:li:fsd_profile:A"},
    "https://www.linkedin.com/in/bob/": {"public_identifier": "bob", "urn": "urn:li:fsd_profile:B"},
    "https://www.linkedin.com/in/carol/": {"public_identifier": "carol", "urn": "urn:li:fsd_profile:C"},
    "https://www.linkedin.com/in/dave/": {"public_identifier": "dave", "urn": "urn:li:fsd_profile:D"},
    "https://www.linkedin.com/in/erin/": {"public_identifier": "erin", "urn": "urn:li:fsd_profile:E"},
}


def _new_list():
    from linkedin.models import LeadList

    return LeadList.objects.create(
        name="S", owner=UserFactory(), source_type=LeadList.SourceType.SEARCH_URL, source_url=SEARCH_URL,
    )


def _mock_voyager(MockAPI):
    MockAPI.return_value.get_profile.side_effect = lambda profile_url: (PROFILES[profile_url], {})


@pytest.mark.django_db
class TestSearchImport:
    def test_creates_enriched_leads_linked_to_list(self, fake_session):
        from crm.models import Lead
        from linkedin.leads import importer

        ll = _new_list()
        urls = ["https://www.linkedin.com/in/alice/", "https://www.linkedin.com/in/bob/"]
        with patch.object(importer, "scrape_search_url", return_value=urls), \
                patch("linkedin_cli.api.client.PlaywrightLinkedinAPI") as MockAPI:
            _mock_voyager(MockAPI)
            result = importer.import_search_url(fake_session, ll, SEARCH_URL)

        assert result == {"created": 2, "scraped": 2}
        assert Lead.objects.filter(lead_list=ll).count() == 2
        assert set(Lead.objects.values_list("public_identifier", flat=True)) == {"alice", "bob"}

    def test_idempotent_rerun_creates_no_duplicates(self, fake_session):
        from crm.models import Lead
        from linkedin.leads import importer

        ll = _new_list()
        urls = ["https://www.linkedin.com/in/alice/", "https://www.linkedin.com/in/bob/"]
        with patch.object(importer, "scrape_search_url", return_value=urls), \
                patch("linkedin_cli.api.client.PlaywrightLinkedinAPI") as MockAPI:
            _mock_voyager(MockAPI)
            importer.import_search_url(fake_session, ll, SEARCH_URL)
            second = importer.import_search_url(fake_session, ll, SEARCH_URL)

        assert second["created"] == 0
        assert Lead.objects.filter(public_identifier__in=["alice", "bob"]).count() == 2

    def test_cap_and_abort_flow_into_scrape_and_all_scraped_saved(self, fake_session):
        # The cap bounds COLLECTION (inside scrape_search_url); everything scraped
        # is saved as a skeleton lead — there is no per-profile enrichment at
        # import time (backfill does that later, paced).
        from crm.models import Lead
        from linkedin.leads import importer

        ll = _new_list()
        sentinel = object()
        with patch.object(importer, "scrape_search_url",
                          return_value=list(PROFILES)[:2]) as mock_scrape:
            result = importer.import_search_url(
                fake_session, ll, SEARCH_URL, cap=2, abort_check=sentinel)

        assert mock_scrape.call_args.kwargs["cap"] == 2
        assert mock_scrape.call_args.kwargs["abort_check"] is sentinel
        assert result["created"] == 2
        assert Lead.objects.filter(lead_list=ll).count() == 2
