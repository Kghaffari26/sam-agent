"""Published output schema (SPEC_GRANTS.md §6) -- the website's JSON contract.

`GrantsLatest` is this agent's `agents_core` `output_model`: agents-core's
runner adds the shared `meta` block (`agents_core.schema.RunMeta`), validates
the whole document, publishes it as `public-data/latest.json` and exports its
JSON Schema as `public-data/schema.json`. `GrantsAll` is published alongside it
as `all.json`.

§6.1 puts two SAM fields inside `meta` (`sam_budget_exhausted`,
`sam_requests_used`). agents-core's runner builds `meta` itself and has no hook
for agent-specific meta fields (see STATUS.md "Needed from agents-core"), so
`GrantsMeta` extends `RunMeta` with them and `GrantsLatest` moves them from a
`sam_meta` key in the analyze() body into `meta` before validation. The
published shape is exactly §6.1's.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from agents_core.schema import AgentOutput, KeyStat, RunMeta, Timestamp
from agents_core.schema import Model as CoreModel
from pydantic import HttpUrl, model_validator

from agents.grants.models import Confidence, Recommendation, SubScores

__all__ = ["KeyStat"]

ALL_JSON_ROW_CAP_DEFAULT = 2000
SAM_META_KEY = "sam_meta"


class Model(CoreModel):
    """Base for published models: no undocumented fields reach the site."""


class GrantsMeta(RunMeta):
    sam_budget_exhausted: bool = False
    sam_requests_used: int = 0


class ProfileSummary(Model):
    id: str
    name: str
    naics: list[str]
    set_asides_eligible: list[str]
    keywords_preview: list[str]
    profile_hash: str


class Thresholds(Model):
    relevance: int
    pursue: int
    consider: int


class LargestValue(Model):
    amount: float
    id: str
    title: str


class FetchedCounts(Model):
    sam: int = 0
    grants_gov: int = 0


class RejectedCounts(Model):
    deadline: int = 0
    type: int = 0
    agency_excluded: int = 0
    set_aside: int = 0
    eligibility: int = 0
    value: int = 0
    place: int = 0
    negative_keyword: int = 0


class Stats(Model):
    new_since_last_run: int
    closing_within_14d: int
    active_matches: int
    largest_value: LargestValue | None = None
    fetched: FetchedCounts
    rejected: RejectedCounts
    below_relevance: int
    llm_scored_this_run: int
    llm_scored_cached: int


class ValueBlock(Model):
    kind: Literal["award_ceiling", "estimated_total", "award_amount", "none"]
    amount: float | None = None
    floor: float | None = None


class SummaryBlock(Model):
    what_they_want: str
    why_fit: list[str]
    risks: list[str]
    next_steps: list[str]
    narrative_source: Literal["llm", "template"]
    model: str
    generated_at: Timestamp


class TopMatch(Model):
    id: str
    source: Literal["sam", "grants_gov"]
    kind: Literal["contract", "grant"]
    notice_type: str
    notice_type_label: str
    title: str
    solicitation_number: str | None = None
    agency: str | None = None
    office: str | None = None
    naics: list[str]
    psc: str | None = None
    set_aside_label: str | None = None
    posted_date: date
    deadline: datetime | None = None
    days_left: int | None = None
    place: str | None = None
    value: ValueBlock
    url: HttpUrl
    fit: int
    sub_scores: SubScores
    recommendation: Recommendation
    confidence: Confidence
    reasons: list[str]
    red_flags: list[str]
    is_new: bool
    changed: bool
    summary: SummaryBlock | None = None


class DeadlineEntry(Model):
    id: str
    title: str
    deadline: datetime
    recommendation: Recommendation
    fit: int


class SourceLink(Model):
    name: str
    url: HttpUrl


class GrantsLatest(AgentOutput):
    meta: GrantsMeta
    headline: str
    key_stats: list[KeyStat]
    profile: ProfileSummary
    thresholds: Thresholds
    stats: Stats
    top_matches: list[TopMatch]
    deadlines_30d: list[DeadlineEntry]
    sources: list[SourceLink]
    disclaimer: str

    @model_validator(mode="before")
    @classmethod
    def _merge_sam_meta(cls, data: Any) -> Any:
        if isinstance(data, dict) and SAM_META_KEY in data:
            data = dict(data)
            extra = data.pop(SAM_META_KEY) or {}
            data["meta"] = {**(data.get("meta") or {}), **extra}
        return data


class AllRow(Model):
    id: str
    source: Literal["sam", "grants_gov"]
    kind: Literal["contract", "grant"]
    type: str
    title: str
    agency: str | None = None
    naics: list[str]
    set_aside: str | None = None
    posted: date
    deadline: datetime | None = None
    value: float | None = None
    fit: int | None = None
    relevance: int
    recommendation: Recommendation | None = None
    reasons: list[str]
    url: HttpUrl
    is_new: bool
    in_top: bool


class GrantsAll(Model):
    generated_at: Timestamp
    rows: list[AllRow]


def _row_sort_key(row: AllRow) -> tuple[int, int]:
    # Sort by fit desc (unscored rows last), then relevance desc.
    fit_key = row.fit if row.fit is not None else -1
    return (fit_key, row.relevance)


def cap_all_rows(rows: list[AllRow], *, max_rows: int = ALL_JSON_ROW_CAP_DEFAULT) -> list[AllRow]:
    """Sort by fit then relevance (descending) and cap at `max_rows` (§6.2)."""
    ordered = sorted(rows, key=_row_sort_key, reverse=True)
    return ordered[:max_rows]
