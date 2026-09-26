"""Builds the §6 documents (`latest.json` body, `all.json`), the headline and
key stats from computed data. Pure Python: no LLM, no I/O."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from agents_core.schema import KeyStat

from agents.grants.config import BusinessProfile, GrantsConfig
from agents.grants.models import Opportunity, Score, Summary
from agents.grants.prompting import NOTICE_TYPE_LABELS, days_left, place_text
from agents.grants.schema import (
    SAM_META_KEY,
    AllRow,
    DeadlineEntry,
    FetchedCounts,
    GrantsAll,
    LargestValue,
    ProfileSummary,
    RejectedCounts,
    SourceLink,
    Stats,
    SummaryBlock,
    Thresholds,
    TopMatch,
    ValueBlock,
    cap_all_rows,
)
from agents.grants.setasides import SET_ASIDES, is_eligible_for_set_aside
from agents.grants.templates import headline

DISCLAIMER = "Automated screening. Always read the official notice and attachments before acting."
SOURCES = [
    SourceLink(name="SAM.gov Contract Opportunities", url="https://sam.gov/"),
    SourceLink(name="Grants.gov", url="https://www.grants.gov/"),
]
MATCH_RECOMMENDATIONS = ("Pursue", "Consider")
CLOSING_SOON_DAYS = 14
DEADLINES_WINDOW_DAYS = 30


@dataclass
class RunFacts:
    """Everything analyze() computed that the documents are built from."""

    now: datetime
    today: date
    profile: BusinessProfile
    profile_hash: str
    config: GrantsConfig
    active: dict[str, Opportunity]  # passed hard filters, still active
    relevance: dict[str, int]
    scores: dict[str, Score]  # valid scores (cached or new)
    summaries: dict[str, Summary]
    top_ids: list[str]
    new_ids: set[str]
    changed_ids: set[str]
    fetched: FetchedCounts
    rejected: dict[str, int]
    below_relevance: int
    llm_scored_this_run: int
    llm_scored_cached: int
    sam_budget_exhausted: bool
    sam_requests_used: int


def set_asides_eligible(profile: BusinessProfile) -> list[str]:
    """Set-asides the profile positively qualifies for (small-business status or
    a held certification), e.g. "Total Small Business (SBA)"."""
    out = []
    for code, info in SET_ASIDES.items():
        positive = code in ("SBA", "SBP") or info.required_certification is not None
        if positive and is_eligible_for_set_aside(code, profile):
            out.append(f"{info.label.removesuffix(' Set-Aside')} ({code})")
    return out


def is_match(score: Score | None) -> bool:
    return score is not None and score.recommendation in MATCH_RECOMMENDATIONS


def _far_future(opp: Opportunity) -> float:
    return opp.deadline.timestamp() if opp.deadline else float("inf")


def rank_top(
    active: dict[str, Opportunity], scores: dict[str, Score], n: int
) -> list[str]:
    """§5.5 #1: top N by fit, ties broken by the nearest deadline."""
    scored = [id_ for id_ in active if id_ in scores]
    scored.sort(key=lambda id_: (-scores[id_].fit, _far_future(active[id_]), id_))
    return scored[:n]


def _top_match(f: RunFacts, id_: str) -> TopMatch:
    opp, score = f.active[id_], f.scores[id_]
    summary = f.summaries.get(id_)
    return TopMatch(
        id=opp.id,
        source=opp.source,
        kind=opp.kind,
        notice_type=opp.notice_type,
        notice_type_label=NOTICE_TYPE_LABELS[opp.notice_type],
        title=opp.title,
        solicitation_number=opp.solicitation_number,
        agency=opp.agency,
        office=opp.office,
        naics=opp.naics,
        psc=opp.psc,
        set_aside_label=opp.set_aside_label,
        posted_date=opp.posted_date,
        deadline=opp.deadline,
        days_left=days_left(opp, f.today),
        place=place_text(opp),
        value=ValueBlock(kind=opp.value_kind, amount=opp.value_amount, floor=opp.value_floor),
        url=opp.url,
        fit=score.fit,
        sub_scores=score.sub_scores,
        recommendation=score.recommendation,
        confidence=score.confidence,
        reasons=score.reasons,
        red_flags=score.red_flags,
        is_new=id_ in f.new_ids,
        changed=id_ in f.changed_ids,
        summary=None
        if summary is None
        else SummaryBlock(
            what_they_want=summary.what_they_want,
            why_fit=summary.why_fit,
            risks=summary.risks,
            next_steps=summary.next_steps,
            narrative_source=summary.narrative_source,
            model=summary.model,
            generated_at=summary.generated_at,
        ),
    )


@dataclass
class Documents:
    body: dict
    all_json: GrantsAll
    headline: str
    key_stats: list[KeyStat]
    active_matches: int


def build_documents(f: RunFacts) -> Documents:
    matches = [id_ for id_ in f.active if is_match(f.scores.get(id_))]
    new_matches = [id_ for id_ in matches if id_ in f.new_ids]

    def closing(id_: str, within: int) -> bool:
        d = days_left(f.active[id_], f.today)
        return d is not None and 0 <= d <= within

    closing_14 = [id_ for id_ in matches if closing(id_, CLOSING_SOON_DAYS)]
    valued = [id_ for id_ in matches if f.active[id_].value_amount is not None]
    largest = None
    if valued:
        best = max(valued, key=lambda id_: f.active[id_].value_amount or 0)
        largest = LargestValue(
            amount=f.active[best].value_amount or 0, id=best, title=f.active[best].title
        )

    top = f.top_ids[0] if f.top_ids else None
    text = headline(
        new_matches=len(new_matches),
        closing_14d=len(closing_14),
        top=(f.active[top], f.scores[top]) if top else None,
    )
    key_stats = [
        KeyStat(label="New matches today", value=len(new_matches), format="count"),
        KeyStat(
            label="Closing ≤ 14 days",
            value=len(closing_14),
            format="count",
            good_direction="neutral",
        ),
        KeyStat(label="Active matches", value=len(matches), format="count", good_direction="up"),
        KeyStat(
            label="Top fit",
            value=f.scores[top].fit if top else None,
            format="count",
            good_direction="up",
        ),
    ]

    deadlines = sorted(
        (id_ for id_ in matches if closing(id_, DEADLINES_WINDOW_DAYS)),
        key=lambda id_: _far_future(f.active[id_]),
    )
    thresholds = Thresholds(
        relevance=f.config.settings.relevance_threshold,
        pursue=f.config.recommendation.pursue,
        consider=f.config.recommendation.consider,
    )
    body = {
        SAM_META_KEY: {
            "sam_budget_exhausted": f.sam_budget_exhausted,
            "sam_requests_used": f.sam_requests_used,
        },
        "headline": text,
        "key_stats": [k.model_dump(mode="json") for k in key_stats],
        "profile": ProfileSummary(
            id=f.profile.id,
            name=f.profile.name,
            naics=f.profile.naics_primary,
            set_asides_eligible=set_asides_eligible(f.profile),
            keywords_preview=f.profile.keywords[:4],
            profile_hash=f.profile_hash,
        ).model_dump(mode="json"),
        "thresholds": thresholds.model_dump(mode="json"),
        "stats": Stats(
            new_since_last_run=len(new_matches),
            closing_within_14d=len(closing_14),
            active_matches=len(matches),
            largest_value=largest,
            fetched=f.fetched,
            rejected=RejectedCounts(**f.rejected),
            below_relevance=f.below_relevance,
            llm_scored_this_run=f.llm_scored_this_run,
            llm_scored_cached=f.llm_scored_cached,
        ).model_dump(mode="json"),
        "top_matches": [_top_match(f, id_).model_dump(mode="json") for id_ in f.top_ids],
        "deadlines_30d": [
            DeadlineEntry(
                id=id_,
                title=f.active[id_].title,
                deadline=f.active[id_].deadline,
                recommendation=f.scores[id_].recommendation,
                fit=f.scores[id_].fit,
            ).model_dump(mode="json")
            for id_ in deadlines
        ],
        "sources": [s.model_dump(mode="json") for s in SOURCES],
        "disclaimer": DISCLAIMER,
    }

    top_set = set(f.top_ids)
    rows = []
    for id_, opp in f.active.items():
        score = f.scores.get(id_)
        rows.append(
            AllRow(
                id=opp.id,
                source=opp.source,
                kind=opp.kind,
                type=NOTICE_TYPE_LABELS[opp.notice_type],
                title=opp.title,
                agency=opp.agency,
                naics=opp.naics,
                set_aside=opp.set_aside_label,
                posted=opp.posted_date,
                deadline=opp.deadline,
                value=opp.value_amount,
                fit=score.fit if score else None,
                relevance=f.relevance.get(id_, 0),
                recommendation=score.recommendation if score else None,
                reasons=score.reasons if score else [],
                url=opp.url,
                is_new=id_ in f.new_ids,
                in_top=id_ in top_set,
            )
        )
    all_json = GrantsAll(
        generated_at=f.now,
        rows=cap_all_rows(rows, max_rows=f.config.settings.all_json_max_rows),
    )
    return Documents(body, all_json, text, key_stats, len(matches))
