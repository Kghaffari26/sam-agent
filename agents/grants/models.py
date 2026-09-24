"""Normalized data models for the grants/contracts finder agent.

See docs/specs/SPEC_GRANTS.md §4 and §5.4/§7.3.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, HttpUrl

NoticeType = Literal[
    "solicitation",
    "combined_synopsis_solicitation",
    "presolicitation",
    "sources_sought",
    "special_notice",
    "grant_posted",
    "grant_forecasted",
]

Recommendation = Literal["Pursue", "Consider", "Pass"]
Confidence = Literal["low", "medium", "high"]


class Opportunity(BaseModel):
    """A SAM.gov contract notice or Grants.gov opportunity, normalized to one shape."""

    model_config = ConfigDict(frozen=False)

    id: str  # "sam:<noticeId>" | "gg:<id>"
    source: Literal["sam", "grants_gov"]
    source_id: str
    kind: Literal["contract", "grant"]
    notice_type: NoticeType
    title: str
    solicitation_number: str | None = None
    agency: str | None = None
    sub_agency: str | None = None
    office: str | None = None
    naics: list[str] = []
    psc: str | None = None
    aln: list[str] = []  # grants (Assistance Listing numbers)
    set_aside_code: str | None = None
    set_aside_label: str | None = None
    eligibility_codes: list[str] = []  # grants
    posted_date: date
    deadline: datetime | None = None  # timezone-aware; None for many forecasts
    place_state: str | None = None
    place_city: str | None = None
    value_kind: Literal["award_ceiling", "estimated_total", "award_amount", "none"] = "none"
    value_amount: float | None = None
    value_floor: float | None = None
    url: HttpUrl
    description_text: str | None = None  # truncated to 8,000 chars; HTML stripped
    description_fetched: bool = False
    content_hash: str  # sha256 of the fields above that matter for scoring
    first_seen_at: datetime
    last_seen_at: datetime


class SubScores(BaseModel):
    """LLM rubric sub-scores (§5.4). Code clamps each to its range and sums them."""

    capability: int  # 0-40
    eligibility: int  # 0-20
    size: int  # 0-15
    timeline: int  # 0-15
    strategic: int  # 0-10


class Score(BaseModel):
    """Result of scoring one opportunity against one business profile."""

    opportunity_id: str
    sub_scores: SubScores
    fit: int
    recommendation: Recommendation
    reasons: list[str]  # at most 3, each <= 12 words
    red_flags: list[str]
    confidence: Confidence
    capped: bool = False
    cap_reason: str | None = None
    model_id: str
    profile_hash: str
    prompt_version: str
    content_hash: str  # opportunity content hash this score was computed from
    scored_at: datetime


class Summary(BaseModel):
    """Bid/no-bid style narrative for a top-ranked opportunity (§7.3)."""

    opportunity_id: str
    what_they_want: str  # <= 2 sentences
    why_fit: list[str]  # <= 3 bullets
    risks: list[str]  # <= 3 bullets
    next_steps: list[str]  # <= 4 bullets, concrete and in order
    narrative_source: Literal["llm", "template"]
    model: str
    profile_hash: str
    prompt_version: str
    content_hash: str
    generated_at: datetime
