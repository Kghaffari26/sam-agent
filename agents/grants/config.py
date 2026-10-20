"""Business profile and run settings for the grants/contracts finder agent.

See docs/specs/SPEC_GRANTS.md §2 and §8. Both TOML files are validated with
pydantic so a bad profile or config fails fast, before any network call.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

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


class ProfileFilters(BaseModel):
    """`[filters]`: what to screen out before any LLM call (SPEC_GRANTS.md §5.2a).

    The first group mirrors SAM.gov's own Contract Opportunities search filters;
    the second goes further than the website can. Every filter is applied locally
    after the (unfiltered) SAM window fetch, so changing one never leaves a gap in
    the fetched window. Empty / None / False means "don't filter".
    """

    # -- SAM.gov search filters --------------------------------------------------
    # NAICS / PSC codes or prefixes ("5415" = the whole industry group). Contracts
    # only: grants carry neither; a contract with none listed passes.
    naics: list[str] = Field(default_factory=list)
    psc: list[str] = Field(default_factory=list)
    # Federal organization: case-insensitive substring of agency, sub-agency or office.
    agencies: list[str] = Field(default_factory=list)
    # Only these set-aside codes (see setasides.py); "NONE" keeps unrestricted
    # (full and open) notices.
    set_asides: list[str] = Field(default_factory=list)
    # Keyword search over title + description: any one of / every one of.
    keywords_any: list[str] = Field(default_factory=list)
    keywords_all: list[str] = Field(default_factory=list)
    # Posted date ("last N days") and response date ("due within N days").
    posted_within_days: int | None = None
    due_within_days: int | None = None
    # -- enhancements beyond the SAM.gov website ---------------------------------
    # Drop sole-source set-asides and "notice of intent to sole source/award"
    # notices: the awardee is already chosen.
    exclude_sole_source: bool = False
    # Check `negative_keywords` against the description too, not just the title.
    negative_keywords_in_description: bool = False
    # Drop notices without a response deadline / without a known value.
    require_deadline: bool = False
    require_known_value: bool = False


SubScoreName = Literal["capability", "eligibility", "size", "timeline", "strategic"]

# The rubric's own point ranges (scoring.RANGES): the default weights, so a profile
# without `[scoring]` scores exactly as before.
DEFAULT_WEIGHTS: dict[str, int] = {
    "capability": 40,
    "eligibility": 20,
    "size": 15,
    "timeline": 15,
    "strategic": 10,
}


class ScoringPreferences(BaseModel):
    """`[scoring]`: how this contractor turns rubric sub-scores into a fit and a
    recommendation (SPEC_GRANTS.md §5.4a). All applied in code after the LLM call,
    so changing them never needs a rescore."""

    # Points each rubric dimension contributes to the 0-100 fit; must sum to 100.
    weights: dict[SubScoreName, int] = Field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    # Per-profile recommendation bands; None falls back to grants.toml [recommendation].
    pursue: int | None = None
    consider: int | None = None
    # Fit points added when a notice is set aside for a status the profile holds
    # (less competition); the total is still capped at 100.
    set_aside_bonus: int = 0

    @model_validator(mode="after")
    def _check(self) -> ScoringPreferences:
        missing = set(DEFAULT_WEIGHTS) - set(self.weights)
        if missing:
            raise ValueError(f"[scoring] weights missing: {sorted(missing)}")
        if any(w < 0 for w in self.weights.values()) or sum(self.weights.values()) != 100:
            raise ValueError("[scoring] weights must be >= 0 and sum to 100")
        if self.pursue is not None and self.consider is not None and self.consider > self.pursue:
            raise ValueError("[scoring] consider must be <= pursue")
        if not 0 <= self.set_aside_bonus <= 20:
            raise ValueError("[scoring] set_aside_bonus must be 0-20")
        return self


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
    filters: ProfileFilters = Field(default_factory=ProfileFilters)
    scoring: ScoringPreferences = Field(default_factory=ScoringPreferences)


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


class ResearchSettings(BaseModel):
    """Bid research (§6.3): an agent loop per Pursue match, cached by content hash."""

    enabled: bool = True
    max_per_run: int = 3
    max_steps: int = 10
    max_usd_per_opportunity: float = 0.12
    max_seconds_per_opportunity: float = 240.0
    max_attachments_per_opportunity: int = 3
    # SAM.gov requests (attachment downloads) the research step may spend per run,
    # on top of being counted in the day's SAM ledger/budget.
    max_sam_requests_per_run: int = 4
    max_usaspending_calls_per_opportunity: int = 3
    attachment_chars: int = 6000


class SamKeySettings(BaseModel):
    """SAM.gov API keys expire (every 90 days for a personal key). Set the date the
    current key expires to get an ops alert `alert_days_before` it does."""

    expires_on: date | None = None
    alert_days_before: int = 14


class GrantsConfig(BaseModel):
    """`config/grants.toml`, validated (SPEC_GRANTS.md §8)."""

    settings: RunSettings = Field(default_factory=RunSettings)
    recommendation: RecommendationThresholds = Field(default_factory=RecommendationThresholds)
    sam: SamSettings = Field(default_factory=SamSettings)
    grants_gov: GrantsGovSettings = Field(default_factory=GrantsGovSettings)
    research: ResearchSettings = Field(default_factory=ResearchSettings)
    sam_key: SamKeySettings = Field(default_factory=SamKeySettings)


def effective_thresholds(profile: BusinessProfile, config: GrantsConfig) -> tuple[int, int]:
    """(pursue, consider): the profile's `[scoring]` bands, else grants.toml's."""
    pursue = profile.scoring.pursue
    consider = profile.scoring.consider
    return (
        config.recommendation.pursue if pursue is None else pursue,
        config.recommendation.consider if consider is None else consider,
    )


# Profile sections applied only in code after/before the LLM: not in the prompts,
# so not part of `profile_hash` (changing them must not invalidate cached scores).
CODE_ONLY_SECTIONS = ("filters", "scoring")


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
    or summaries does, which correctly invalidates cached scores. `[filters]` and
    `[scoring]` are left out: they never reach a prompt (see CODE_ONLY_SECTIONS).
    """
    data = profile.model_dump(mode="json", exclude=set(CODE_ONLY_SECTIONS))
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
