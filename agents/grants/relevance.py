"""Deterministic relevance pre-score, 0-100, run before any LLM call (SPEC_GRANTS.md §5.3).

Candidates are opportunities scoring at or above `relevance_threshold`; everything
else is still stored but never scored by the LLM.
"""

from __future__ import annotations

import re

from agents.grants.config import BusinessProfile
from agents.grants.models import Opportunity

NAICS_PRIMARY_POINTS = 50
NAICS_SECONDARY_POINTS = 35
NAICS_PREFIX_POINTS = 20
PSC_PREFIX_POINTS = 15
TITLE_KEYWORD_POINTS = 10
TITLE_KEYWORD_CAP = 30
DESCRIPTION_KEYWORD_POINTS = 5
DESCRIPTION_KEYWORD_CAP = 20
TARGET_AGENCY_POINTS = 5
NEGATIVE_KEYWORD_PENALTY = -30

NAICS_PREFIX_LEN = 4


def _keyword_in_text(keyword: str, text: str) -> bool:
    pattern = r"\b" + re.escape(keyword.lower()) + r"\b"
    return re.search(pattern, text.lower()) is not None


def _naics_score(opp_naics: list[str], profile: BusinessProfile) -> int:
    naics_set = set(opp_naics)
    if naics_set & set(profile.naics_primary):
        return NAICS_PRIMARY_POINTS
    if naics_set & set(profile.naics_secondary):
        return NAICS_SECONDARY_POINTS
    primary_prefixes = {n[:NAICS_PREFIX_LEN] for n in profile.naics_primary}
    if any(n[:NAICS_PREFIX_LEN] in primary_prefixes for n in naics_set):
        return NAICS_PREFIX_POINTS
    return 0


def compute_relevance(opp: Opportunity, profile: BusinessProfile) -> int:
    score = _naics_score(opp.naics, profile)

    if opp.psc and any(opp.psc.startswith(prefix) for prefix in profile.psc_prefixes):
        score += PSC_PREFIX_POINTS

    title_hits = sum(1 for kw in profile.keywords if _keyword_in_text(kw, opp.title))
    score += min(title_hits * TITLE_KEYWORD_POINTS, TITLE_KEYWORD_CAP)

    if opp.description_text:
        description_hits = sum(
            1 for kw in profile.keywords if _keyword_in_text(kw, opp.description_text)
        )
        score += min(description_hits * DESCRIPTION_KEYWORD_POINTS, DESCRIPTION_KEYWORD_CAP)

        if any(_keyword_in_text(neg, opp.description_text) for neg in profile.negative_keywords):
            score += NEGATIVE_KEYWORD_PENALTY

    if opp.agency and opp.agency in profile.target_agencies:
        score += TARGET_AGENCY_POINTS

    return max(0, min(100, score))


def select_candidates(
    opportunities: list[Opportunity],
    profile: BusinessProfile,
    *,
    relevance_threshold: int,
    max_candidates: int,
) -> tuple[list[tuple[Opportunity, int]], list[tuple[Opportunity, int]]]:
    """Score every opportunity; split into (candidates, below_threshold).

    `candidates` is sorted by relevance descending and capped at `max_candidates`;
    overflow beyond the cap is folded back into `below_threshold` (still stored,
    still shown, just outside this run's LLM-scoring budget).
    """
    scored = [(opp, compute_relevance(opp, profile)) for opp in opportunities]
    above = sorted(
        (pair for pair in scored if pair[1] >= relevance_threshold),
        key=lambda pair: pair[1],
        reverse=True,
    )
    below = [pair for pair in scored if pair[1] < relevance_threshold]

    candidates = above[:max_candidates]
    overflow = above[max_candidates:]
    below_threshold = overflow + below
    return candidates, below_threshold
