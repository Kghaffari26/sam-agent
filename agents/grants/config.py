"""Business profile and run settings for the grants/contracts finder agent.

See docs/specs/SPEC_GRANTS.md §2 and §8. Both TOML files are validated with
pydantic so a bad profile or config fails fast, before any network call.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

EntityType = Literal["small_business", "nonprofit", "university", "individual", "large_business"]
Clearance = Literal["none", "public_trust", "secret", "top_secret"]

# Order in which a profile becomes eligible for a clearance requirement: an
# opportunity requiring a clearance above the profile's is a hard incompatibility.
CLEARANCE_RANK: dict[Clearance, int] = {
    "none": 0,
    "public_trust": 1,
    "secret": 2,
    "top_secret": 3,
}


class BusinessProfile(BaseModel):
    """`config/business_profile.toml`, validated (SPEC_GRANTS.md §2)."""

    id: str
    name: str
    summary: str
    entity_type: EntityType
    certifications: list[str] = Field(default_factory=list)
    sam_registered: bool = False
    naics_primary: list[str] = Field(default_factory=list)
    naics_secondary: list[str] = Field(default_factory=list)
    psc_prefixes: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    negative_keywords: list[str] = Field(default_factory=list)
    clearance: Clearance = "none"
    min_value: float | None = None
    max_value: float | None = None
    remote_ok: bool = True
    states: list[str] = Field(default_factory=list)
    target_agencies: list[str] = Field(default_factory=list)
    excluded_agencies: list[str] = Field(default_factory=list)
    notice_types: list[str] = Field(default_factory=list)
    include_grants: bool = True
    grant_keywords: list[str] = Field(default_factory=list)
    min_days_to_respond: int = 0
    team_capacity_note: str = ""
    past_performance: list[str] = Field(default_factory=list)


class RunSettings(BaseModel):
    profile: str = "config/business_profile.toml"
    sam_daily_request_budget: int = 8
    max_sam_description_fetches: int = 3
    max_grants_detail_fetches: int = 60
    relevance_threshold: int = 25
    max_llm_scoring_per_run: int = 150
    batch_poll_timeout_min: int = 30
    top_n_summaries: int = 20
    store_max_items: int = 5000
    all_json_max_rows: int = 2000
    forecast_max_age_days: int = 180


class RecommendationThresholds(BaseModel):
    pursue: int = 75
    consider: int = 55


class SamSettings(BaseModel):
    ptypes: list[str] = Field(default_factory=lambda: ["o", "k", "p", "r"])
    window_overlap_days: int = 1
    first_run_lookback_days: int = 7


class GrantsGovSettings(BaseModel):
    opp_statuses: str = "forecasted|posted"
    rows_per_query: int = 100


class GrantsConfig(BaseModel):
    """`config/grants.toml`, validated (SPEC_GRANTS.md §8)."""

    settings: RunSettings = Field(default_factory=RunSettings)
    recommendation: RecommendationThresholds = Field(default_factory=RecommendationThresholds)
    sam: SamSettings = Field(default_factory=SamSettings)
    grants_gov: GrantsGovSettings = Field(default_factory=GrantsGovSettings)


def load_business_profile(path: str | Path) -> BusinessProfile:
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return BusinessProfile.model_validate(data)


def load_grants_config(path: str | Path) -> GrantsConfig:
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return GrantsConfig.model_validate(data)


def profile_hash(profile: BusinessProfile) -> str:
    """sha256 of the profile's normalized (canonical JSON) content.

    Used as part of every cache key (SPEC_GRANTS.md §2, §5.4): reformatting the
    TOML doesn't change this hash, but any field that affects filtering, scoring
    or summaries does, which correctly invalidates cached scores.
    """
    canonical = json.dumps(profile.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
