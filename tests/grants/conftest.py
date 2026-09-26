from datetime import UTC, datetime

import pytest

from agents.grants.config import BusinessProfile
from agents.grants.models import Opportunity

NOW = datetime(2026, 9, 24, tzinfo=UTC)


def make_opportunity(**overrides) -> Opportunity:
    base = dict(
        id="sam:abc123",
        source="sam",
        source_id="abc123",
        kind="contract",
        notice_type="solicitation",
        title="Cloud Modernization Support Services",
        posted_date="2026-09-20",
        deadline="2026-10-15T16:00:00-04:00",
        naics=["541512"],
        psc="DA01",
        url="https://sam.gov/opp/abc123/view",
        content_hash="deadbeef",
        first_seen_at=NOW,
        last_seen_at=NOW,
    )
    base.update(overrides)
    return Opportunity.model_validate(base)


def make_profile(**overrides) -> BusinessProfile:
    base = dict(
        id="test",
        name="Test consultancy",
        summary="A test business.",
        entity_type="small_business",
        certifications=[],
        naics_primary=["541511", "541512"],
        naics_secondary=["541519"],
        psc_prefixes=["DA", "DB"],
        keywords=["cloud", "software development", "modernization"],
        negative_keywords=["construction", "janitorial"],
        min_value=25000,
        max_value=5000000,
        remote_ok=True,
        states=[],
        target_agencies=["GENERAL SERVICES ADMINISTRATION"],
        excluded_agencies=[],
        notice_types=[
            "solicitation",
            "combined_synopsis_solicitation",
            "presolicitation",
            "sources_sought",
        ],
        include_grants=True,
        grant_keywords=["software", "artificial intelligence"],
        min_days_to_respond=3,
    )
    base.update(overrides)
    return BusinessProfile.model_validate(base)


@pytest.fixture
def opportunity_factory():
    return make_opportunity


@pytest.fixture
def profile_factory():
    return make_profile


@pytest.fixture
def default_profile() -> BusinessProfile:
    return make_profile()


@pytest.fixture(autouse=True)
def _isolated_agents_core_dirs(tmp_path, monkeypatch):
    """Keep agents-core's cost/guard-failure logs and caches out of the repo's data/."""
    monkeypatch.setenv("AGENTS_CORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AGENTS_CORE_PUBLISH_DIR", str(tmp_path / "public-data"))
    monkeypatch.setenv("AGENTS_CORE_HTTP_CACHE_DIR", str(tmp_path / "http-cache"))
